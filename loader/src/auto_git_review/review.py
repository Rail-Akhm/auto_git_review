"""Основная функция ревью: единый проход по всем открытым PR с подробным логированием."""

import json
import logging

from .alm import AlmClient
from .config import get_settings
from .llm import LlmClient
from .prompt import load_prompt, render_prompt

logger = logging.getLogger("auto_git_review")


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


def run_review(log=None):
    """Главная функция: ревью всех открытых PR. Подробно логирует каждый шаг."""
    log = log or logger

    log.info("=" * 60)
    log.info("СТАРТ: автоматическое ревью открытых PR")

    settings = get_settings()
    log.info("Настройки загружены:")
    log.info("  ALM URL      : %s", settings.azure_url)
    log.info("  Репозиторий  : %s", settings.azure_repo)
    log.info("  LLM модель   : %s", settings.llm_model)
    log.info("  LLM endpoint : %s", settings.llm_url)

    if not settings.azure_pat:
        raise RuntimeError("Не задан AZURE_DEVOPS_PAT (пустой токен).")
    if not settings.llm_api_key:
        raise RuntimeError("Не задан LLM_API_KEY (пустой ключ).")

    alm = AlmClient(settings)
    llm = LlmClient(settings)
    prompt_template = load_prompt()
    log.info("Промпт загружен из prompts/review_prompt.md (%d символов)", len(prompt_template))

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

        log.info("  Шаг 2: получаю детали PR #%s...", pr_id)
        detail = alm.get_pull_request(pr_id)
        src_commit = detail.get("lastMergeSourceCommit", {}).get("commitId")
        tgt_commit = detail.get("lastMergeTargetCommit", {}).get("commitId")
        log.info("    lastMergeSourceCommit: %s", src_commit)
        log.info("    lastMergeTargetCommit: %s", tgt_commit)

        log.info("  Шаг 3: получаю связанные work items...")
        work_items_text = _describe_work_items(alm, pr_id)
        log.info("    %s", work_items_text.replace("\n", "\n    "))

        log.info("  Шаг 4: сборка diff (заглушка — реализуется в следующей фазе)...")
        diff_text = ""      # TODO(фаза 3): unified diff по изменённым файлам
        context_text = ""   # TODO(фаза 4): контекст 5 предыдущих PR файла

        log.info("  Шаг 5: формирую промпт и вызываю LLM (%s)...", settings.llm_model)
        messages = [
            {
                "role": "user",
                "content": render_prompt(
                    prompt_template,
                    work_item=work_items_text,
                    context=context_text or "(контекст не собран)",
                    diff=diff_text or "(diff не собран)",
                ),
            }
        ]
        response = llm.chat(messages)
        content = response["content"]
        log.info("    Получен ответ LLM (%d символов)", len(content))

        log.info("  Шаг 6: разбираю результат...")
        parsed = _parse_json_response(content)
        if parsed:
            verdict = parsed.get("verdict", "?")
            summary = parsed.get("summary", "")
            comments = parsed.get("comments", [])
            log.info("    Вердикт: %s", verdict)
            log.info("    Резюме: %s", summary)
            log.info("    Комментариев: %d", len(comments))
            results.append({"pr_id": pr_id, "verdict": verdict, "comments": comments})
        else:
            log.warning("    Не удалось разобрать JSON-ответ, сырой текст:")
            log.warning("%s", content[:2000])
            results.append({"pr_id": pr_id, "verdict": "parse_error", "raw": content})

    log.info("=" * 60)
    log.info("ГОТОВО: обработано PR — %d", len(results))
    return results
