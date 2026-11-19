"""Клиент локального LLM через LiteLLM-шлюз (OpenAI-совместимый)."""

import logging
import time

import requests

from .config import get_settings

logger = logging.getLogger("auto_git_review.llm")


class LlmClient:
    """Клиент локального LLM через LiteLLM-шлюз (OpenAI-совместимый endpoint).

    Поддерживает запасную модель: если основная недоступна (сеть, таймаут,
    5xx/429, исчезла из шлюза), запрос повторяется на `llm_model_fallback`.
    Ошибки самого запроса (например, превышение контекста или неверный ключ)
    на запасную модель не переводятся — это не проблема доступности.
    """

    def __init__(self, settings=None):
        self.settings = settings or get_settings()

    def chat(self, messages, temperature=None, timeout=600, model=None) -> dict:
        """Один вызов chat/completions с запасной моделью при сбое основной.

        Возвращает dict с ключами:
          - content / reasoning — текст ответа модели;
          - usage — сырое поле usage ответа (prompt/completion/total tokens);
          - latency_ms — время выполнения запроса в миллисекундах;
          - model — модель, которая фактически ответила;
          - fallback_used / fallback_from — признак и имя основной модели,
            если сработало переключение.
        """
        primary = model or self.settings.llm_model
        try:
            return self._chat_once(messages, primary, temperature, timeout)
        except Exception as exc:
            fallback = self.settings.llm_model_fallback
            if not fallback or fallback == primary or not self._should_fallback(exc):
                raise
            logger.warning(
                "LLM «%s» недоступна (%s) — повторяю на запасной «%s»",
                primary,
                exc,
                fallback,
            )
            result = self._chat_once(messages, fallback, temperature, timeout)
            result["fallback_used"] = True
            result["fallback_from"] = primary
            return result

    @staticmethod
    def _should_fallback(exc) -> bool:
        """Переключаться ли на запасную модель при данном исключении."""
        # Транспортные сбои: сеть, TLS, таймаут — без HTTP-ответа.
        if isinstance(exc, requests.exceptions.RequestException) and getattr(
            exc, "response", None
        ) is None:
            return True

        response = getattr(exc, "response", None)
        if response is None:
            return False

        status = response.status_code
        if status in (404, 408, 429) or status >= 500:
            return True
        if status == 400:
            text = (response.text or "").lower()
            # Превышение контекста — не повод менять модель: окно у запасной
            # такое же, повтор только потратит время.
            if "context" in text and ("exceed" in text or "maximum" in text):
                return False
            return "invalid model" in text or "model not found" in text
        return False

    def _chat_once(self, messages, model, temperature, timeout) -> dict:
        payload = {
            "model": model,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        # Логируем промпт только на DEBUG: он может содержать код файлов и
        # чувствительные фрагменты корпоративных данных.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("LLM запрос (%s): %s", model, payload)

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
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage")
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        finish_reason = choice.get("finish_reason")

        logger.info(
            "LLM ответ (%s): latency=%dms, tokens=%s, finish=%s",
            model,
            latency_ms,
            usage.get("total_tokens") if usage else "нет данных",
            finish_reason,
        )
        if not content and reasoning:
            # У «thinking»-моделей ответ иногда целиком остаётся в reasoning.
            logger.warning(
                "LLM (%s): content пустой, reasoning=%d симв. — использую его",
                model,
                len(reasoning),
            )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("LLM ответ: %s", message)

        return {
            "content": content,
            "reasoning": reasoning,
            "usage": usage,
            "latency_ms": latency_ms,
            "model": model,
            "fallback_used": False,
            "finish_reason": finish_reason,
        }
