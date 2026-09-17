"""Тесты чистой логики ревью (без сети): парсинг, обрезка, батчи, агрегация, формат."""

from auto_git_review import review


def test_parse_json_plain():
    assert review._parse_json_response('{"verdict": "approve"}') == {"verdict": "approve"}


def test_parse_json_markdown_fenced():
    text = '```json\n{"verdict": "comment"}\n```'
    assert review._parse_json_response(text) == {"verdict": "comment"}


def test_parse_json_plain_fence():
    text = '```\n{"verdict": "approve"}\n```'
    assert review._parse_json_response(text) == {"verdict": "approve"}


def test_parse_json_surrounded_by_text():
    text = 'Вот результат: {"verdict": "request_changes"}'
    assert review._parse_json_response(text) == {"verdict": "request_changes"}


def test_parse_json_invalid_returns_none():
    assert review._parse_json_response("не json вообще") is None


def test_parse_json_empty_returns_none():
    assert review._parse_json_response("") is None
    assert review._parse_json_response(None) is None


def test_truncate_short_unchanged():
    assert review._truncate("abc", limit=10) == "abc"


def test_truncate_long_adds_marker():
    out = review._truncate("a" * 20, limit=10)
    assert out.startswith("a" * 10)
    assert "обрезано" in out


def test_chunk_sections_single_batch():
    assert review._chunk_sections(["aa", "bb"], max_chars=10) == [["aa", "bb"]]


def test_chunk_sections_splits_on_overflow():
    sections = ["aaaaaa", "bbbbbb", "cccccc"]
    assert review._chunk_sections(sections, max_chars=8) == [
        ["aaaaaa"],
        ["bbbbbb"],
        ["cccccc"],
    ]


def test_merge_results_verdict_priority():
    parsed = [{"verdict": "approve"}, {"verdict": "request_changes"}, {"verdict": "comment"}]
    assert review._merge_results(parsed)["verdict"] == "request_changes"


def test_merge_results_summary_multiple_parts():
    parsed = [
        {"verdict": "approve", "summary": "A"},
        {"verdict": "comment", "summary": "B"},
    ]
    merged = review._merge_results(parsed)
    assert merged["summary"] == "Часть 1: A\n\nЧасть 2: B"


def test_merge_results_summary_single_part_no_prefix():
    parsed = [{"verdict": "approve", "summary": "A"}]
    assert review._merge_results(parsed)["summary"] == "A"


def test_merge_results_comments_concatenated():
    parsed = [
        {"verdict": "approve", "comments": [{"file": "a.sql"}]},
        {"verdict": "comment", "comments": [{"file": "b.sql"}]},
    ]
    assert len(review._merge_results(parsed)["comments"]) == 2


def test_format_review_comment_contains_marker_and_verdict():
    parsed = {
        "verdict": "request_changes",
        "summary": "Найдены проблемы",
        "comments": [{"file": "a.sql", "line": 1, "severity": "major", "text": "ошибка"}],
    }
    text = review._format_review_comment(parsed)
    assert review.COMMENT_MARKER in text
    assert "request_changes" in text
    assert "Найдены проблемы" in text
    assert "a.sql:1" in text


def test_format_history_empty():
    assert review._format_history([]) == "(истории нет — файл создаётся впервые)"


def test_format_history_lists_commits():
    commits = [
        {"commitId": "abc12345678", "comment": "Fix bug", "author": {"name": "Ivan"}},
    ]
    out = review._format_history(commits)
    assert "abc12345" in out
    assert "Fix bug" in out
    assert "Ivan" in out


def test_format_change_add():
    change = {"changeType": "add", "item": {"path": "/a/b.sql"}}
    out = review._format_change(change, "SELECT 1", "")
    assert "[НОВЫЙ ФАЙЛ] /a/b.sql" in out
    assert "SELECT 1" in out


def test_format_change_delete():
    change = {"changeType": "delete", "item": {"path": "/a/b.sql"}}
    out = review._format_change(change, "", "DROP TABLE t")
    assert "[УДАЛЁН ФАЙЛ] /a/b.sql" in out
    assert "DROP TABLE t" in out


def test_format_change_edit_contains_diff_and_full():
    change = {"changeType": "edit", "item": {"path": "/a/b.sql"}}
    out = review._format_change(change, "SELECT 2", "SELECT 1")
    assert "[ИЗМЕНЁН] /a/b.sql" in out
    assert "Изменённые строки (diff)" in out
    assert "Полное содержимое файла" in out
