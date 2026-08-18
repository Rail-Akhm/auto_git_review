"""Клиент локального LLM через LiteLLM-шлюз (OpenAI-совместимый)."""

import requests

from .config import get_settings


class LlmClient:
    def __init__(self, settings=None):
        self.settings = settings or get_settings()

    def chat(self, messages, temperature=None, timeout=600) -> dict:
        """Один вызов chat/completions. Возвращает {content, reasoning}."""
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature

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
        resp.raise_for_status()
        data = resp.json()
        message = data["choices"][0]["message"]
        return {
            "content": message.get("content") or "",
            "reasoning": message.get("reasoning_content") or "",
        }
