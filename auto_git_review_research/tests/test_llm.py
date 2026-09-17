"""Тесты LlmClient: формирование запроса и разбор ответа (без сети)."""

from unittest.mock import MagicMock

from auto_git_review.config import Settings
from auto_git_review.llm import LlmClient


def _settings():
    return Settings(
        azure_url="https://alm.example/TFS/GPN",
        azure_project="Proj1",
        azure_pat="pat",
        azure_repo="repo1",
        api_version="6.1-preview",
        llm_url="https://llm.example/v1/chat/completions",
        llm_api_key="sk-key",
        llm_model="qwen3:latest",
        post_comments=False,
    )


def test_chat_returns_content_and_reasoning(monkeypatch):
    fake = MagicMock()
    fake.json.return_value = {
        "choices": [{"message": {"content": "ok", "reasoning_content": "think"}}]
    }
    fake.raise_for_status.return_value = None
    monkeypatch.setattr("auto_git_review.llm.requests.post", lambda *a, **k: fake)

    result = LlmClient(_settings()).chat([{"role": "user", "content": "hi"}])
    assert result["content"] == "ok"
    assert result["reasoning"] == "think"


def test_chat_returns_usage_and_latency(monkeypatch):
    fake = MagicMock()
    fake.json.return_value = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    fake.raise_for_status.return_value = None
    monkeypatch.setattr("auto_git_review.llm.requests.post", lambda *a, **k: fake)

    result = LlmClient(_settings()).chat([{"role": "user", "content": "hi"}])
    assert result["usage"]["total_tokens"] == 15
    assert result["latency_ms"] >= 0


def test_chat_sends_model_and_api_key(monkeypatch):
    fake = MagicMock()
    fake.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
    fake.raise_for_status.return_value = None
    calls = {}

    def fake_post(*args, **kwargs):
        calls["kwargs"] = kwargs
        return fake

    monkeypatch.setattr("auto_git_review.llm.requests.post", fake_post)

    LlmClient(_settings()).chat([{"role": "user", "content": "hi"}])
    kwargs = calls["kwargs"]
    assert kwargs["json"]["model"] == "qwen3:latest"
    assert kwargs["headers"]["x-litellm-api-key"] == "sk-key"
