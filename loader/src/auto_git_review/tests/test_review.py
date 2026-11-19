"""Тесты чистой логики ревью (без сети): парсинг, обрезка, батчи, агрегация, формат."""

import logging
from unittest.mock import MagicMock

import pytest

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


def test_format_review_comment_records_reviewed_commit():
    parsed = {"verdict": "approve", "summary": "ok", "comments": []}
    sha = "a" * 40
    target = "c" * 40
    text = review._format_review_comment(parsed, sha, target)
    assert "Проверенный коммит" in text
    assert f"reviewed_commit={sha}" in text
    assert f"reviewed_target={target}" in text


class _ThreadsAlm:
    """ALM-заглушка: отдаёт заданные комментарии PR и дату коммита."""

    def __init__(self, contents, published=None, commit_date=None):
        self._contents = contents
        self._published = published
        self._commit_date = commit_date

    def get_threads(self, pr_id):
        return {
            "value": [
                {
                    "comments": [
                        {
                            "content": content,
                            "isDeleted": False,
                            "publishedDate": self._published,
                        }
                        for content in self._contents
                    ]
                }
            ]
        }

    def get_commit(self, commit_id):
        if not self._commit_date:
            raise ConnectionError("commit metadata unavailable")
        return {"committer": {"date": self._commit_date}}


class _BrokenAlm:
    def get_threads(self, pr_id):
        raise ConnectionError("alm down")


def test_already_reviewed_without_marker():
    assert review._already_reviewed(_ThreadsAlm(["обычный комментарий"]), 1, "a" * 40) is False


def test_already_reviewed_same_commit():
    sha = "a" * 40
    content = f"**Автоматическое ревью ({review.COMMENT_MARKER})**\n<!-- {review.COMMENT_MARKER}:reviewed_commit={sha} -->"
    assert review._already_reviewed(_ThreadsAlm([content]), 1, sha) is True


def test_already_reviewed_short_sha_matches():
    content = f"<!-- {review.COMMENT_MARKER}:reviewed_commit={'b' * 8} -->"
    assert review._already_reviewed(_ThreadsAlm([content]), 1, "b" * 40) is True


def test_already_reviewed_new_commit_triggers_review():
    content = f"<!-- {review.COMMENT_MARKER}:reviewed_commit={'a' * 40} -->"
    alm = _ThreadsAlm(
        [content],
        published="2026-11-19T07:00:00Z",
        commit_date="2026-11-19T08:00:00Z",
    )
    assert review._already_reviewed(alm, 1, "b" * 40) is False


def test_already_reviewed_ignores_target_in_comment():
    # target хранится справочно и сам по себе повторное ревью не запускает.
    content = (
        f"<!-- {review.COMMENT_MARKER}:reviewed_commit={'a' * 40}"
        f" reviewed_target={'c' * 40} -->"
    )
    assert review._already_reviewed(_ThreadsAlm([content]), 1, "a" * 40) is True


def test_already_reviewed_visible_line_fallback():
    # HTML-комментарий вырезан рендерером — SHA берём из видимой строки.
    content = (
        f"**Автоматическое ревью ({review.COMMENT_MARKER})**\n\n"
        "**Проверенный коммит:** `aaaaaaaa` (target `cccccccc`)"
    )
    assert review._already_reviewed(_ThreadsAlm([content]), 1, "a" * 40) is True
    assert review._already_reviewed(_ThreadsAlm([content]), 1, "b" * 40) is False


def test_already_reviewed_legacy_comment_newer_than_commit():
    # Старый комментарий без SHA: комментарий опубликован после коммита —
    # новых коммитов нет, повторное ревью не нужно.
    content = f"**Автоматическое ревью ({review.COMMENT_MARKER})**"
    alm = _ThreadsAlm(
        [content],
        published="2026-11-19T08:00:00Z",
        commit_date="2026-11-19T07:00:00Z",
    )
    assert review._already_reviewed(alm, 1, "a" * 40) is True


def test_already_reviewed_legacy_commit_after_comment():
    content = f"**Автоматическое ревью ({review.COMMENT_MARKER})**"
    alm = _ThreadsAlm(
        [content],
        published="2026-11-19T07:00:00Z",
        commit_date="2026-11-19T08:00:00Z",
    )
    assert review._already_reviewed(alm, 1, "a" * 40) is False


def test_already_reviewed_legacy_without_dates():
    # Без SHA и без дат — перепроверяем (лучше лишнее ревью, чем пропуск).
    content = f"**Автоматическое ревью ({review.COMMENT_MARKER})**"
    assert review._already_reviewed(_ThreadsAlm([content]), 1, "a" * 40) is False


def test_parse_iso_datetime_handles_seven_fraction_digits():
    parsed = review._parse_iso_datetime("2026-11-19T08:00:00.1234567Z")
    assert parsed is not None
    assert parsed.year == 2026 and parsed.hour == 8
    assert review._parse_iso_datetime("not-a-date") is None


def test_already_reviewed_alm_error_skips_to_avoid_duplicate():
    assert review._already_reviewed(_BrokenAlm(), 1, "a" * 40) is True


def test_collect_pr_changes_prefers_iteration_source_commit():
    alm = MagicMock()
    alm.get_pull_request.return_value = {
        "lastMergeSourceCommit": {"commitId": "old"},
        "lastMergeTargetCommit": {"commitId": "target"},
    }
    alm.get_pr_iterations.return_value = {
        "value": [
            {"id": 1, "sourceRefCommit": {"commitId": "old"}},
            {"id": 2, "sourceRefCommit": {"commitId": "new"}},
        ]
    }
    alm.get_pr_iteration_changes.return_value = {
        "changeEntries": [{"item": {"path": "/a.sql"}}]
    }

    src, tgt, changes = review._collect_pr_changes(
        alm, 1, logging.getLogger("test")
    )
    assert src == "new"
    assert tgt == "target"
    assert changes == [{"item": {"path": "/a.sql"}}]
    alm.get_pr_iteration_changes.assert_called_once_with(1, 2)


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


class _UnavailableLlm:
    """LLM-шлюз лежит: любой вызов падает."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        raise ConnectionError("llm server is down")


class _FlakyLlm:
    """Первый батч проходит, второй — сбой транспорта."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        if self.calls == 1:
            return {
                "content": '{"verdict": "approve", "summary": "ok", "comments": []}',
                "usage": {"total_tokens": 10},
                "latency_ms": 5,
                "model": "qwen3.8-27b-fp8",
                "fallback_used": False,
            }
        raise ConnectionError("llm server is down")


def test_review_batches_raises_when_llm_unavailable():
    llm = _UnavailableLlm()
    with pytest.raises(review.LlmUnavailableError):
        review._review_pr_batches(
            llm,
            "[[WORK_ITEM]]\n[[FILES]]",
            [["file1"], ["file2"]],
            "work item",
            logging.getLogger("test"),
        )
    # Не перебираем все батчи и таймауты: падаем на первом же сбое.
    assert llm.calls == 1


def test_review_batches_raises_on_failure_after_success():
    llm = _FlakyLlm()
    with pytest.raises(review.LlmUnavailableError):
        review._review_pr_batches(
            llm,
            "[[WORK_ITEM]]\n[[FILES]]",
            [["file1"], ["file2"]],
            "work item",
            logging.getLogger("test"),
        )
    # Первый батч прошёл, второй упал — ревью не считается успешным.
    assert llm.calls == 2


_VALID_REVIEW = '{"verdict": "approve", "summary": "ok", "comments": []}'


class _ReasoningLlm:
    """content пустой, итоговый JSON лежит в reasoning."""

    def chat(self, messages):
        return {
            "content": "",
            "reasoning": _VALID_REVIEW,
            "usage": {"total_tokens": 5},
            "latency_ms": 3,
            "model": "qwen3.8-think",
            "fallback_used": False,
            "finish_reason": "stop",
        }


class _RetryLlm:
    """Первый ответ пустой, второй — валидный JSON."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "",
                "reasoning": "",
                "usage": {"total_tokens": 5},
                "latency_ms": 3,
                "model": "qwen3.8-27b-fp8",
                "fallback_used": False,
                "finish_reason": "length",
            }
        return {
            "content": _VALID_REVIEW,
            "reasoning": "",
            "usage": {"total_tokens": 5},
            "latency_ms": 3,
            "model": "qwen3.8-27b-fp8",
            "fallback_used": False,
            "finish_reason": "stop",
        }


class _EmptyLlm:
    """Модель всегда отвечает пустым content и reasoning."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        return {
            "content": "",
            "reasoning": "",
            "usage": {"total_tokens": 5, "completion_tokens": 0},
            "latency_ms": 3,
            "model": "qwen3.8-27b-fp8",
            "fallback_used": False,
            "finish_reason": "stop",
        }


def test_review_batches_uses_json_from_reasoning():
    parsed, _, _ = review._review_pr_batches(
        _ReasoningLlm(),
        "[[WORK_ITEM]]\n[[FILES]]",
        [["file1"]],
        "work item",
        logging.getLogger("test"),
    )
    assert parsed["verdict"] == "approve"


def test_review_batches_retries_empty_response():
    llm = _RetryLlm()
    parsed, latency_ms, tokens = review._review_pr_batches(
        llm,
        "[[WORK_ITEM]]\n[[FILES]]",
        [["file1"]],
        "work item",
        logging.getLogger("test"),
    )
    assert parsed["verdict"] == "approve"
    assert llm.calls == review.LLM_RESPONSE_RETRIES + 1
    assert latency_ms == 6
    assert tokens == 10


def test_review_batches_raises_on_empty_response_after_retries():
    llm = _EmptyLlm()
    with pytest.raises(review.LlmResponseError) as exc_info:
        review._review_pr_batches(
            llm,
            "[[WORK_ITEM]]\n[[FILES]]",
            [["file1"]],
            "work item",
            logging.getLogger("test"),
        )
    assert llm.calls == review.LLM_RESPONSE_RETRIES + 1
    assert "content=0" in str(exc_info.value)
