"""Основная функция ревью: единый проход по всем открытым PR с подробным логированием."""

import difflib
import json
import logging
import re
import time
from dataclasses import replace
from datetime import datetime, timezone

from .alm import AlmClient
from .config import get_settings
from .llm import LlmClient
from .monitoring import emit_summary, summarize
from .prompt import load_prompt, render_prompt

logger = logging.getLogger("auto_git_review")

# Лимиты под рабочее окно 250k токенов (фактический предел модели —
# 262 144). Оценка «символов на токен» ~4.5 верна для латиницы, но код и
# кириллица токенизируются плотнее, поэтому оставляем запас и место на промпт
# и ответ модели.

# Максимальный размер ПОЛНОГО содержимого одного файла, подаваемого в промпт.
# Раньше для изменённых файлов подавался только unified diff с 3 строками
# контекста — модель видела фрагменты и считала код неполным.
MAX_CHANGE_CHARS = 200_000

# Отдельный, меньший лимит на блок «изменённые строки» (diff) — вторичен по объёму.
MAX_DIFF_CHARS = 20_000

# Максимальный суммарный размер одного «батча» файлов, подаваемого в ОДИН вызов LLM.
# 400k символов — это ориентировочно 90–200k токенов в зависимости от плотности
# токенизации; остаётся запас до 250k на промпт и ответ. Большие PR ревьюятся по
# частям, результаты потом агрегируются.
MAX_BATCH_CHARS = 400_000

CHANGE_TYPE_LABELS = {
    "add": "новый файл",
    "edit": "изменён",
    "delete": "удалён",
    "none": "без изменений",
}

# Маркер, по которому узнаём собственные комментарии бота (для dedup при повторных запусках).
COMMENT_MARKER = "auto_git_review"

# SHA проверенных коммитов, записанные в комментарии бота. По ним понимаем,
# появились ли после ревью новые коммиты (source) или уехала целевая ветка
# (target), и нужен ли повторный проход.
REVIEWED_COMMIT_RE = re.compile(r"reviewed_commit=([0-9a-fA-F]{7,40})")
# Резервный разбор видимой строки — если HTML-комментарий вырежет рендерер.
VISIBLE_COMMITS_RE = re.compile(
    r"Проверенный коммит:\*\*\s*`([0-9a-fA-F]{7,40})`"
    r"(?:\s*\(target\s*`([0-9a-fA-F]{7,40})`\))?"
)


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


def _format_review_comment(
    parsed: dict, reviewed_commit: str = None, target_commit: str = None
) -> str:
    """Формирует markdown-текст общего комментария к PR из результата ревью.

    В комментарий записываются SHA проверенных source- и target-коммитов: по
    ним следующий запуск отличит «уже отревьюено» от «человек докатил новый
    коммит» или «уехала целевая ветка».
    """
    verdict = parsed.get("verdict", "?")
    summary = (parsed.get("summary") or "").strip()
    comments = parsed.get("comments") or []

    lines = [f"**Автоматическое ревью ({COMMENT_MARKER})**", ""]
    if reviewed_commit:
        commit_line = f"**Проверенный коммит:** `{reviewed_commit[:8]}`"
        if target_commit:
            commit_line += f" (target `{target_commit[:8]}`)"
        lines.append(commit_line)
        lines.append("")
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
    if reviewed_commit:
        # Скрытый маркер для dedup: человеку не мешает, парсится регуляркой.
        marker = f"{COMMENT_MARKER}:reviewed_commit={reviewed_commit}"
        if target_commit:
            marker += f" reviewed_target={target_commit}"
        lines.append("")
        lines.append(f"<!-- {marker} -->")
    return "\n".join(lines)


def _parse_iso_datetime(value):
    """ISO-дата ALM -> aware datetime (UTC); None, если разобрать не удалось."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    # ADO иногда отдаёт 7 знаков после точки — fromisoformat понимает до 6.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _commit_datetime(alm: AlmClient, commit_id: str):
    """Дата source-коммита (committer.date) или None при ошибке."""
    try:
        data = alm.get_commit(commit_id)
    except Exception as exc:
        logger.warning("Не удалось получить дату коммита %s: %s", commit_id, exc)
        return None
    return _parse_iso_datetime(
        (data.get("committer") or {}).get("date")
        or (data.get("author") or {}).get("date")
    )


def _already_reviewed(
    alm: AlmClient,
    pr_id: int,
    source_commit: str = None,
) -> bool:
    """True, если текущий source-коммит PR уже отревьюен.

    Проверяем по маркеру в тексте, а не по автору: владелец PAT — реальный
    пользователь, у которого могут быть и собственные комментарии. Маркер
    однозначно отделяет комментарий бота.

    Решение принимается по source-коммиту (то, что запушил автор PR):
    совпал SHA — пропускаем. Движение целевой ветки само по себе повторное
    ревью не запускает. Если SHA в комментарии нет (старый формат или маркер
    вырезан), сравниваем время публикации комментария с датой коммита: если
    комментарий не старше коммита, новых изменений после ревью не было.
    """
    try:
        threads = alm.get_threads(pr_id).get("value", [])
    except Exception as exc:
        # При ошибке проверки не можем гарантировать отсутствие дубля — пропускаем
        # постинг (лучше не отправить, чем отправить второй раз).
        logger.warning(
            "Не удалось проверить комментарии PR #%s: %s — пропускаю отправку",
            pr_id,
            exc,
        )
        return True

    found_marker = False
    reviewed_sources = []
    latest_published = None
    for thread in threads:
        for comment in thread.get("comments", []):
            if comment.get("isDeleted"):
                continue
            content = comment.get("content") or ""
            if COMMENT_MARKER not in content:
                continue
            found_marker = True
            published = _parse_iso_datetime(comment.get("publishedDate"))
            if published and (latest_published is None or published > latest_published):
                latest_published = published

            src_match = REVIEWED_COMMIT_RE.search(content)
            if src_match:
                reviewed_sources.append(src_match.group(1).lower())
                continue
            # Резерв: видимая строка «Проверенный коммит: `sha` (target `sha`)».
            visible = VISIBLE_COMMITS_RE.search(content)
            if visible:
                reviewed_sources.append(visible.group(1).lower())

    if not found_marker:
        return False
    if not source_commit:
        # Сравнивать не с чем — не рискуем отправить второй комментарий.
        return True

    def same_commit(left, right):
        return left == right or left.startswith(right) or right.startswith(left)

    source = source_commit.lower()
    if any(same_commit(source, reviewed) for reviewed in reviewed_sources):
        return True

    # SHA не совпал или не записан: решает время. Комментарий, опубликованный
    # не раньше коммита, означает, что новых коммитов после ревью не было.
    commit_dt = _commit_datetime(alm, source_commit)
    if commit_dt and latest_published and latest_published >= commit_dt:
        logger.info(
            "PR #%s: комментарий от %s новее source-коммита %s от %s — "
            "новых коммитов нет, пропускаю",
            pr_id,
            latest_published.isoformat(),
            source_commit[:8],
            commit_dt.isoformat(),
        )
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


def _build_file_section(
    alm: AlmClient, change: dict, source_commit: str, target_commit: str
) -> str:
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


def _build_file_sections(
    alm: AlmClient, changes: list, source_commit: str, target_commit: str
) -> list:
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


def _collect_pr_changes(alm: AlmClient, pr_id: int, log) -> tuple:
    """Возвращает (source_commit, target_commit, changes) для PR.

    changes — список изменённых файлов последней итерации (changeEntries).
    При отсутствии коммитов или ошибке список changes остаётся пустым.
    """
    detail = alm.get_pull_request(pr_id)
    src_commit = detail.get("lastMergeSourceCommit", {}).get("commitId")
    tgt_commit = detail.get("lastMergeTargetCommit", {}).get("commitId")
    log.info("    lastMergeSourceCommit: %s", src_commit)
    log.info("    lastMergeTargetCommit: %s", tgt_commit)

    changes = []
    try:
        iterations = alm.get_pr_iterations(pr_id).get("value", [])
        if iterations:
            last_iteration = max(iterations, key=lambda it: it.get("id", 0))
            # Для активного PR source-коммит последней итерации — это ровно то,
            # что человек запушил последним; он же фиксируется в комментарии бота.
            iteration_src = (last_iteration.get("sourceRefCommit") or {}).get(
                "commitId"
            )
            if iteration_src:
                src_commit = iteration_src
                log.info(
                    "    Итерация %s, sourceRefCommit: %s",
                    last_iteration.get("id"),
                    src_commit,
                )
            changes = alm.get_pr_iteration_changes(
                pr_id, last_iteration["id"]
            ).get("changeEntries", [])
    except Exception as exc:
        log.warning("    Не удалось получить список файлов PR #%s: %s", pr_id, exc)

    if not src_commit or not tgt_commit:
        log.warning("    Нет коммитов для diff, изменения не собираются.")
        changes = []
    return src_commit, tgt_commit, changes


class LlmUnavailableError(RuntimeError):
    """LLM не дала ответа: ревью PR прервано и задача Airflow должна упасть.

    Недоступность шлюза или отвергнутый запрос не должны выглядеть как
    «успешный» прогон без ревью (например, когда сервер LLM лежал).
    """


def _review_pr_batches(
    llm: LlmClient, prompt_template: str, batches: list, work_items_text: str, log
) -> tuple:
    """Ревью одного PR по батчам: вызов LLM на каждый батч и агрегация результата.

    Возвращает кортеж (parsed, total_latency_ms, total_tokens):
      - parsed — итоговый dict (verdict + summary + comments) или None, если ни
        один батч не дал разобранный JSON-ответ;
      - total_latency_ms / total_tokens — суммарные метрики вызовов LLM по PR.

    Поднимает ``LlmUnavailableError`` при любом сбое вызова LLM: недоступность
    шлюза (или отвергнутый запрос) не должна выглядеть как успешный прогон, а
    частичный отчёт без остальных батчей — публиковаться.
    """
    parsed_batches = []
    total_latency_ms = 0
    total_tokens = 0
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
            # Любой сбой вызова — повод прервать ревью PR и завалить задачу:
            # частичный отчёт опаснее ошибки (может пропустить дефекты), а серия
            # таймаутов на остальных батчах только растянет прогон.
            raise LlmUnavailableError(
                f"LLM не дала ответа на батч {bi}/{len(batches)} — ревью PR "
                f"прервано, остальные батчи не запускались. "
                f"Последняя ошибка: {exc}"
            ) from exc
        if response.get("fallback_used"):
            log.warning(
                "    Батч %d/%d: основная модель «%s» недоступна — ответ получен "
                "на запасной «%s»",
                bi,
                len(batches),
                response.get("fallback_from"),
                response.get("model"),
            )
        total_latency_ms += response.get("latency_ms", 0)
        usage = response.get("usage") or {}
        total_tokens += usage.get("total_tokens", 0)
        log.info("    Батч %d/%d: ответ LLM (%d символов)", bi, len(batches), len(content))
        parsed = _parse_json_response(content)
        if parsed:
            parsed_batches.append(parsed)
        else:
            log.warning(
                "    Батч %d/%d: не удалось разобрать JSON-ответ, сырой текст:",
                bi,
                len(batches),
            )
            log.warning("%s", content[:1000])

    log.info(
        "    Итого по PR: %d батч(ей), latency=%dms, tokens=%d",
        len(batches),
        total_latency_ms,
        total_tokens,
    )

    if not parsed_batches:
        return None, total_latency_ms, total_tokens
    return _merge_results(parsed_batches), total_latency_ms, total_tokens


def _review_one_pr(
    alm: AlmClient,
    llm: LlmClient,
    prompt_template: str,
    pr: dict,
    settings,
    log,
    max_batches: int,
) -> dict:
    """Ревью одного PR: сбор контекста, батчевый вызов LLM, постинг комментария.

    Возвращает dict результата либо None, если PR пропущен (dedup / нет изменений).
    """
    pr_id = pr["pullRequestId"]
    title = pr.get("title", "")
    author = pr.get("createdBy", {}).get("displayName", "?")
    started = time.monotonic()
    log.info("-" * 60)
    log.info("PR #%s «%s» (автор: %s)", pr_id, title, author)
    log.info("  Ветки: %s -> %s", pr.get("sourceRefName"), pr.get("targetRefName"))

    log.info("  Шаг 2: получаю детали PR #%s...", pr_id)
    src_commit, tgt_commit, changes = _collect_pr_changes(alm, pr_id, log)
    log.info("    Изменённых файлов: %d", len(changes))

    # Dedup по проверенному коммиту: старый комментарий не блокирует ревью,
    # если после него человек запушил новые коммиты (только в режиме постинга).
    if settings.post_comments and _already_reviewed(alm, pr_id, src_commit):
        log.info(
            "    Комментарий для коммита %s уже есть — пропускаю PR.",
            (src_commit or "?")[:8],
        )
        return None

    log.info("  Шаг 3: получаю связанные work items...")
    work_items_text = _describe_work_items(alm, pr_id)
    log.info("    %s", work_items_text.replace("\n", "\n    "))

    log.info("  Шаг 4: собираю контекст файлов и делю на батчи...")
    sections = _build_file_sections(alm, changes, src_commit, tgt_commit)
    batches = _chunk_sections(sections)
    log.info("    Файлов: %d, батчей для LLM: %d", len(sections), len(batches))

    if not batches:
        log.warning("    Изменения не получены — ревью пропущено.")
        return None

    llm_latency_ms = 0
    llm_tokens = 0
    if len(batches) > max_batches:
        # Тяжёлый случай (напр. тысячи файлов): не гоняем сотни батчей через LLM,
        # формируем общее резюме с числом файлов и пометкой «нужно ручное ревью».
        log.warning(
            "    Слишком большой PR: %d файлов -> %d батчей (лимит %d) — полный анализ невозможен.",
            len(sections),
            len(batches),
            max_batches,
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
        log.info("  Шаг 5: вызываю LLM (%s) по каждому батчу...", settings.llm_model)
        parsed, llm_latency_ms, llm_tokens = _review_pr_batches(
            llm, prompt_template, batches, work_items_text, log
        )
        if parsed is None:
            log.warning(
                "    Ни один батч не дал разобранный результат — ревью не сформировано."
            )
            return {
                "pr_id": pr_id,
                "verdict": "parse_error",
                "latency_ms": llm_latency_ms,
                "tokens": llm_tokens,
            }

    verdict = parsed.get("verdict", "?")
    summary = parsed.get("summary", "")
    comments = parsed.get("comments", [])
    log.info("    Вердикт: %s", verdict)
    log.info("    Резюме: %s", summary)
    log.info("    Комментариев: %d", len(comments))

    if settings.post_comments:
        log.info("    Отправляю резюме в PR как комментарий...")
        try:
            comment_text = _format_review_comment(parsed, src_commit, tgt_commit)
            thread = alm.create_thread_comment(pr_id, comment_text)
            log.info("    Комментарий создан, thread id=%s", thread.get("id"))
        except Exception as exc:
            log.warning("    Не удалось отправить комментарий в PR #%s: %s", pr_id, exc)
    else:
        log.info("    Отправка комментариев отключена (POST_COMMENTS=false).")

    log.info(
        "    PR #%s обработан за %dms (end-to-end)",
        pr_id,
        int((time.monotonic() - started) * 1000),
    )
    return {
        "pr_id": pr_id,
        "verdict": verdict,
        "comments": comments,
        "latency_ms": llm_latency_ms,
        "tokens": llm_tokens,
    }


def run_review(
    log=None,
    repo: str = None,
    project: str = None,
    post_comment: bool = False,
    prompt_name: str = None,
    max_batches: int = 20,
    model: str = None,
    fallback_model: str = None,
):
    """Главная функция: ревью всех открытых PR. Подробно логирует каждый шаг.

    Параметры (передаются из таски Airflow):
      - repo           — имя репозитория в ALM (если None — из настроек/config.py);
      - project        — имя проекта ALM (если None — из настроек/config.py);
      - post_comment   — отправлять ли резюме комментарием в PR;
      - prompt_name    — файл промпта в prompts/ (для разных типов репозиториев);
      - max_batches    — максимум батчей на один PR; при превышении полный анализ
                         не выполняется, возвращается общее резюме;
      - model          — основная LLM-модель (переопределяет LLM_MODEL);
      - fallback_model — запасная модель при недоступности основной
                         (переопределяет LLM_MODEL_FALLBACK).
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
    if model:
        settings = replace(settings, llm_model=model)
    if fallback_model:
        settings = replace(settings, llm_model_fallback=fallback_model)
    prompt_name = prompt_name or "review_prompt_greenplum.md"

    log.info("Настройки загружены:")
    log.info("  ALM URL      : %s", settings.azure_url)
    log.info("  Проект       : %s", settings.azure_project)
    log.info("  Репозиторий  : %s", settings.azure_repo)
    log.info("  LLM модель   : %s", settings.llm_model)
    log.info("  Запасная LLM : %s", settings.llm_model_fallback or "—")
    log.info("  LLM endpoint : %s", settings.llm_url)
    log.info("  Промпт       : %s", prompt_name)
    log.info("  Пост-коммент : %s", settings.post_comments)
    log.info(
        "  Лимиты       : файл=%d, diff=%d, батч=%d симв., батчей≤%d",
        MAX_CHANGE_CHARS,
        MAX_DIFF_CHARS,
        MAX_BATCH_CHARS,
        max_batches,
    )

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
        log.info("PR %d/%d:", idx, len(prs))
        result = _review_one_pr(
            alm, llm, prompt_template, pr, settings, log, max_batches
        )
        if result is not None:
            results.append(result)

    log.info("=" * 60)
    log.info("ГОТОВО: обработано PR — %d", len(results))
    emit_summary(log, summarize(results))
    return results
