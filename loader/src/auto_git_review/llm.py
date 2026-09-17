"""Клиент локального LLM через LiteLLM-шлюз (OpenAI-совместимый)."""

import logging
import time

import requests

from .config import get_settings

logger = logging.getLogger("auto_git_review.llm")


class LlmClient:
    """Клиент локального LLM через LiteLLM-шлюз (OpenAI-совместимый endpoint)."""

    def __init__(self, settings=None):
        self.settings = settings or get_settings()

    def chat(self, messages, temperature=None, timeout=600) -> dict:
        """Один вызов chat/completions.

        Возвращает dict с ключами:
          - content / reasoning — текст ответа модели;
          - usage — сырое поле usage ответа (prompt/completion/total tokens) или None;
          - latency_ms — время выполнения запроса в миллисекундах.
        """
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        # Логируем промпт только на DEBUG: он может содержать код файлов и
        # чувствительные фрагменты корпоративных данных.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("LLM запрос: %s", payload)

        started = time.monotonic()
        resp = requests.post(
            self.settings.llm_url,
            verify=self.settings.verify_ssl,
            headers={
                "x-litellm-api-key": self.settings.llm_api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        resp.raise_for_status()
        data = resp.json()
        message = data["choices"][0]["message"]
        usage = data.get("usage")

        logger.info(
            "LLM ответ: latency=%dms, tokens=%s",
            latency_ms,
            usage.get("total_tokens") if usage else "нет данных",
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("LLM ответ: %s", message)

        return {
            "content": message.get("content") or "",
            "reasoning": message.get("reasoning_content") or "",
            "usage": usage,
            "latency_ms": latency_ms,
        }
