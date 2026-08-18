"""Основная функция ревью: единый проход по всем открытым PR с подробным логированием."""

import difflib
import json
import logging
from dataclasses import replace

from .alm import AlmClient
from .config import get_settings
from .llm import LlmClient
from .prompt import load_prompt, render_prompt

logger = logging.getLogger("auto_git_review")

# Максимальный размер ПОЛНОГО содержимого одного файла, подаваемого в промпт
# (защита контекста ~30k токенов). Раньше для изменённых файлов подавался только
# unified diff с 3 строками контекста — модель видела фрагменты и считала код неполным.
MAX_CHANGE_CHARS = 12000

# Отдельный, меньший лимит на блок «изменённые строки» (diff) — вторичен по объёму.
MAX_DIFF_CHARS = 2000

# Максимальный суммарный размер одного «батча» файлов, подаваемого в ОДИН вызов LLM
# (~8–10k токенов). Большие PR ревьюятся по частям, результаты потом агрегируются.
MAX_BATCH_CHARS = 30000

CHANGE_TYPE_LABELS = {
    "add": "новый файл",
    "edit": "изменён",
    "delete": "удалён",
    "none": "без изменений",
}

# Маркер, по которому узнаём собственные комментарии бота (для dedup при повторных запусках).
COMMENT_MARKER = "auto_git_review"


def _parse_json_response(text: str):
    """Толерантный разбор JSON из ответа модели (модель может оборачивать в markdown)."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def _truncate(text: str, limit: int = MAX_CHANGE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (обрезано из-за ограничения контекста)"


def _describe_work_items(alm: AlmClient, pr_id: int) -> str:
    """Читаемое описание связанных work items PR (для подстановки в промпт)."""
    try:
        data = alm.get_pr_work_items(pr_id)
    except Exception as exc:
        logger.warning("Не удалось получить work items PR #%s: %s", pr_id, exc)
        return "(work items не найдены)"

    items = data.get("value", [])
    if not items:
        return "(work items не найдены)"

    descriptions = []
    for wi in items:
        wi_id = wi.get("id")
        try:
            detail = alm.get_work_item(wi_id)
            fields = detail.get("fields", {})
            wi_type = fields.get("System.WorkItemType", "?")
            wi_title = fields.get("System.Title", "?")
            descriptions.append(f"[{wi_type} #{wi_id}] {wi_title}")
        except Exception as exc:
            logger.warning("Не удалось получить work item #%s: %s", wi_id, exc)
            descriptions.append(f"[#{wi_id}]")
    return "\n".join(descriptions)


def _format_review_comment(parsed: dict) -> str:
    """Формирует markdown-текст общего комментария к PR из результата ревью."""
    verdict = parsed.get("verdict", "?")
    summary = (parsed.get("summary") or "").strip()
    comments = parsed.get("comments") or []

    lines = [f"**Автоматическое ревью ({COMMENT_MARKER})**", ""]
    lines.append(f"**Вердикт:** {verdict}")
    if summary:
        lines.append("")
        lines.append("**Резюме:**")
        lines.append(summary)
    if comments:
        lines.append("")
        lines.append("**Замечания:**")
        for c in comments:
            file_path = c.get("file", "")
            line = c.get("line")
            severity = c.get("severity", "")
            text = (c.get("text") or "").strip()
            locator = f"{file_path}:{line}" if line else file_path
            lines.append(f"- `{locator}` ({severity}): {text}")
    return "\n".join(lines)


def _already_reviewed(alm: AlmClient, pr_id: int) -> bool:
    """True, если у PR уже есть наш не-удалённый комментарий (по маркеру COMMENT_MARKER).

    Проверяем по маркеру в тексте, а не по автору: владелец PAT — реальный
    пользователь, у которого могут быть и собственные комментарии. Маркер
    однозначно отделяет комментарий бота.
    """
    try:
        threads = alm.get_threads(pr_id).get("value", [])
    except Exception as exc:
        # При ошибке проверки не можем гарантировать отсутствие дубля — пропускаем
        # постинг (лучше не отправить, чем отправить второй раз).
        logger.warning("Не удалось проверить комментарии PR #%s: %s — пропускаю отправку", pr_id, exc)
        return True

    for thread in threads:
        for comment in thread.get("comments", []):
            if comment.get("isDeleted"):
                continue
            if COMMENT_MARKER in (comment.get("content") or ""):
                return True
    return False


def _format_change(change: dict, new_content: str, old_content: str) -> str:
    """Читаемое представление одного изменения файла (unified diff или полное содержимое).

    new_content / old_content передаются сюда уже вытянутыми через alm.get_file_content()
    (endpoint iterations/{id}/changes контента не содержит).
    """
    change_type = change.get("changeType", "edit")
    path = (change.get("item") or {}).get("path", "?")

    if change_type == "add":
        return _truncate(f"[НОВЫЙ ФАЙЛ] {path}\n{new_content}")
    if change_type == "delete":
        return _truncate(f"[УДАЛЁН ФАЙЛ] {path}\n{old_content}")

    clean_path = path.lstrip("/")
    diff = difflib.unified_diff(
        old_content.splitlines(),
        new_content.splitlines(),
        fromfile=f"a/{clean_path}",
        tofile=f"b/{clean_path}",
        lineterm="",
    )
    diff_text = _truncate("\n".join(diff), MAX_DIFF_CHARS)
    # Показываем и изменённые строки, и ПОЛНОЕ новое содержимое файла: иначе модель
    # видит только фрагменты diff и считает, что «кода не хватает».
    return (
        f"[ИЗМЕНЁН] {path}\n\n"
        f"Изменённые строки (diff):\n{diff_text}\n\n"
        f"Полное содержимое файла (новая версия):\n{_truncate(new_content)}"
    )


def _format_history(commits: list) -> str:
    """Краткая история изменений файла: commit-сообщения."""
    if not commits:
        return "(истории нет — файл создаётся впервые)"
    lines = []
    for c in commits:
        sha = (c.get("commitId") or "")[:8]
        comment = (c.get("comment") or "").strip().splitlines()
        first = comment[0] if comment else "(без сообщения)"
        author = (c.get("author") or {}).get("name", "?")
        lines.append(f"- {sha} {first} (автор: {author})")
    return "\n".join(lines)


def _build_file_section(alm: AlmClient, change: dict, source_commit: str, target_commit: str) -> str:
    """Контекст одного изменённого файла: история + полное содержимое/изменения."""
    change_type = change.get("changeType", "edit")
    item = change.get("item") or {}
    path = item.get("path", "?")

    if change_type == "add":
        history_text = "(истории нет — файл создаётся впервые)"
    else:
        try:
            data = alm.get_file_commits(path, target_commit)
            history_text = _format_history(data.get("value", []))
        except Exception as exc:
            logger.warning("Не удалось получить историю файла %s: %s", path, exc)
            history_text = "(историю получить не удалось)"

    # Контент: для add — только новый, для delete — только старый, для edit — оба.
    new_content = ""
    old_content = ""
    try:
        if change_type in ("add", "edit"):
            new_content = alm.get_file_content(path, source_commit)
    except Exception as exc:
        logger.warning("Не удалось получить новый контент %s: %s", path, exc)
    try:
        if change_type in ("delete", "edit"):
            old_content = alm.get_file_content(path, target_commit)
    except Exception as exc:
        logger.warning("Не удалось получить старый контент %s: %s", path, exc)

    diff_text = _format_change(change, new_content, old_content)
    return (
        f"### Файл: {path} ({CHANGE_TYPE_LABELS.get(change_type, change_type)})\n"
        f"История предыдущих изменений:\n{history_text}\n\n"
        f"Текущее изменение:\n{diff_text}\n"
    )


def _build_file_sections(alm: AlmClient, changes: list, source_commit: str, target_commit: str) -> list:
    """Список секций контекста по изменённым файлам (tree-объекты пропускаются)."""
    sections = []
    for change in changes:
        item = change.get("item") or {}
        if item.get("gitObjectType") and item.get("gitObjectType") != "blob":
            continue
        sections.append(_build_file_section(alm, change, source_commit, target_commit))
    return sections


def _chunk_sections(sections: list, max_chars: int = MAX_BATCH_CHARS) -> list:
    """Жадно делит секции файлов на батчи, чтобы суммарный размер каждого ≤ max_chars."""
    batches = []
    current = []
    current_size = 0
    for section in sections:
        size = len(section)
        if current and current_size + size > max_chars:
            batches.append(current)
            current = []
            current_size = 0
        current.append(section)
        current_size += size
    if current:
        batches.append(current)
    return batches


VERDICT_PRIORITY = {"request_changes": 3, "comment": 2, "approve": 1}


def _merge_results(parsed_list: list) -> dict:
    """Агрегирует результаты ревью батчей в один итог (verdict + summary + comments)."""
    verdicts = [p.get("verdict", "approve") for p in parsed_list]
    verdict = max(verdicts, key=lambda v: VERDICT_PRIORITY.get(v, 0))

    summaries = []
    for i, p in enumerate(parsed_list, start=1):
        s = (p.get("summary") or "").strip()
        if s:
            summaries.append(s if len(parsed_list) == 1 else f"Часть {i}: {s}")
    summary = "\n\n".join(summaries)

    comments = []
    for p in parsed_list:
        comments.extend(p.get("comments") or [])
    return {"verdict": verdict, "summary": summary, "comments": comments}


def run_review(log=None, repo: str = None, project: str = None, post_comment: bool = False, prompt_name: str = None, max_batches: int = 20):
    """Главная функция: ревью всех открытых PR. Подробно логирует каждый шаг.

    Параметры (передаются из таски Airflow):
      - repo         — имя репозитория в ALM (если None — из настроек/config.py);
      - project      — имя проекта ALM (если None — из настроек/config.py);
      - post_comment — отправлять ли резюме комментарием в PR;
      - prompt_name  — файл промпта в prompts/ (для разных типов репозиториев);
      - max_batches  — максимум батчей на один PR; при превышении полный анализ
                       не выполняется, возвращается общее резюме.
    """
    log = log or logger

    log.info("=" * 60)
    log.info("СТАРТ: автоматическое ревью открытых PR")

    settings = get_settings()
    if repo:
        settings = replace(settings, azure_repo=repo)
    if project:
        settings = replace(settings, azure_project=project)
    if post_comment:
        settings = replace(settings, post_comments=True)
    prompt_name = prompt_name or "review_prompt_greenplum.md"

    log.info("Настройки загружены:")
    log.info("  ALM URL      : %s", settings.azure_url)
    log.info("  Проект       : %s", settings.azure_project)
    log.info("  Репозиторий  : %s", settings.azure_repo)
    log.info("  LLM модель   : %s", settings.llm_model)
    log.info("  LLM endpoint : %s", settings.llm_url)
    log.info("  Промпт       : %s", prompt_name)
    log.info("  Пост-коммент : %s", settings.post_comments)

    if not settings.azure_pat:
        raise RuntimeError("Не задан AZURE_DEVOPS_PAT (пустой токен).")
    if not settings.llm_api_key:
        raise RuntimeError("Не задан LLM_API_KEY (пустой ключ).")

    alm = AlmClient(settings)
    llm = LlmClient(settings)
    prompt_template = load_prompt(prompt_name)
    log.info("Промпт загружен из prompts/%s (%d символов)", prompt_name, len(prompt_template))

    log.info("Шаг 1: получаю список открытых PR из ALM...")
    prs = alm.list_open_pull_requests().get("value", [])
    log.info("Найдено открытых PR: %d", len(prs))

    if not prs:
        log.info("Нет открытых PR — ревью не требуется.")
        log.info("=" * 60)
        return []

    results = []
    for idx, pr in enumerate(prs, start=1):
        pr_id = pr["pullRequestId"]
        title = pr.get("title", "")
        author = pr.get("createdBy", {}).get("displayName", "?")
        log.info("-" * 60)
        log.info("PR %d/%d: #%s «%s» (автор: %s)", idx, len(prs), pr_id, title, author)
        log.info("  Ветки: %s -> %s", pr.get("sourceRefName"), pr.get("targetRefName"))

        # Dedup: проверяем наличие нашего комментария ДО запуска LLM — не тратим
        # вызовы модели на уже отревьюенные PR (только в режиме постинга).
        if settings.post_comments and _already_reviewed(alm, pr_id):
            log.info("    Комментарий от бота уже есть — пропускаю PR.")
            continue

        log.info("  Шаг 2: получаю детали PR #%s...", pr_id)
        detail = alm.get_pull_request(pr_id)
        src_commit = detail.get("lastMergeSourceCommit", {}).get("commitId")
        tgt_commit = detail.get("lastMergeTargetCommit", {}).get("commitId")
        log.info("    lastMergeSourceCommit: %s", src_commit)
        log.info("    lastMergeTargetCommit: %s", tgt_commit)

        log.info("  Шаг 3: получаю связанные work items...")
        work_items_text = _describe_work_items(alm, pr_id)
        log.info("    %s", work_items_text.replace("\n", "\n    "))

        log.info("  Шаг 4: собираю список изменённых файлов PR...")
        changes = []
        if not src_commit or not tgt_commit:
            log.warning("    Нет коммитов для diff, изменения не собраны.")
        else:
            try:
                iterations = alm.get_pr_iterations(pr_id).get("value", [])
                if iterations:
                    last_iteration = max(it["id"] for it in iterations)
                    changes = alm.get_pr_iteration_changes(pr_id, last_iteration).get(
                        "changeEntries", []
                    )
            except Exception as exc:
                log.warning("    Не удалось получить список файлов PR #%s: %s", pr_id, exc)
        log.info("    Изменённых файлов: %d", len(changes))

        log.info("  Шаг 5: собираю контекст файлов и делю на батчи...")
        sections = _build_file_sections(alm, changes, src_commit, tgt_commit)
        batches = _chunk_sections(sections)
        log.info("    Файлов: %d, батчей для LLM: %d", len(sections), len(batches))

        if not batches:
            log.warning("    Изменения не получены — ревью пропущено.")
            continue

        if len(batches) > max_batches:
            # Тяжёлый случай (напр. тысячи файлов): не гоняем сотни батчей через LLM,
            # формируем общее резюме с числом файлов и пометкой «нужно ручное ревью».
            log.warning(
                "    Слишком большой PR: %d файлов -> %d батчей (лимит %d) — полный анализ невозможен.",
                len(sections), len(batches), max_batches,
            )
            parsed = {
                "verdict": "comment",
                "summary": (
                    f"В PR изменено {len(sections)} файлов — объём слишком велик для "
                    f"полного автоматического анализа (превышен лимит батчей: "
                    f"{len(batches)} > {max_batches}). Требуется ручное ревью."
                ),
                "comments": [],
            }
        else:
            log.info("  Шаг 6: вызываю LLM (%s) по каждому батчу...", settings.llm_model)
            parsed_batches = []
            for bi, batch in enumerate(batches, start=1):
                files_text = "\n".join(batch)
                messages = [
                    {
                        "role": "user",
                        "content": render_prompt(
                            prompt_template,
                            work_item=work_items_text,
                            files=files_text,
                        ),
                    }
                ]
                try:
                    response = llm.chat(messages)
                    content = response["content"]
                except Exception as exc:
                    log.warning("    Батч %d/%d: ошибка вызова LLM: %s", bi, len(batches), exc)
                    continue
                log.info("    Батч %d/%d: ответ LLM (%d символов)", bi, len(batches), len(content))
                parsed = _parse_json_response(content)
                if parsed:
                    parsed_batches.append(parsed)
                else:
                    log.warning("    Батч %d/%d: не удалось разобрать JSON-ответ, сырой текст:", bi, len(batches))
                    log.warning("%s", content[:1000])

            log.info("  Шаг 7: агрегирую результаты батчей...")
            if not parsed_batches:
                log.warning("    Ни один батч не дал разобранный результат — ревью не сформировано.")
                results.append({"pr_id": pr_id, "verdict": "parse_error"})
                continue
            parsed = _merge_results(parsed_batches)

        verdict = parsed.get("verdict", "?")
        summary = parsed.get("summary", "")
        comments = parsed.get("comments", [])
        log.info("    Вердикт: %s", verdict)
        log.info("    Резюме: %s", summary)
        log.info("    Комментариев: %d", len(comments))

        if settings.post_comments:
            log.info("    Отправляю резюме в PR как комментарий...")
            try:
                comment_text = _format_review_comment(parsed)
                thread = alm.create_thread_comment(pr_id, comment_text)
                log.info("    Комментарий создан, thread id=%s", thread.get("id"))
            except Exception as exc:
                log.warning("    Не удалось отправить комментарий в PR #%s: %s", pr_id, exc)
        else:
            log.info("    Отправка комментариев отключена (POST_COMMENTS=false).")

        results.append({"pr_id": pr_id, "verdict": verdict, "comments": comments})

    log.info("=" * 60)
    log.info("ГОТОВО: обработано PR — %d", len(results))
    return results
