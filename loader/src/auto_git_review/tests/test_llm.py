"""Тесты LlmClient: формирование запроса, разбор ответа и запасная модель (без сети)."""

from unittest.mock import MagicMock

import pytest
import requests

from auto_git_review.config import Settings
from auto_git_review.llm import LlmClient


def _settings(**overrides):
    base = dict(
        azure_url="https://alm.example/TFS/GPN",
        azure_project="Proj1",
        azure_pat="pat",
        azure_repo="repo1",
        api_version="6.1-preview",
        llm_url="https://llm.example/v1/chat/completions",
        llm_api_key="sk-key",
        llm_model="qwen3.8-27b-fp8",
        llm_model_fallback="qwen3.8-think",
        post_comments=False,
    )
    base.update(overrides)
    return Settings(**base)


def _ok_response():
    fake = MagicMock()
    fake.json.return_value = {
        "choices": [{"message": {"content": "ok", "reasoning_content": "think"}}]
    }
    fake.raise_for_status.return_value = None
    return fake


def test_chat_returns_content_and_reasoning(monkeypatch):
    fake = _ok_response()
    monkeypatch.setattr("auto_git_review.llm.requests.post", lambda *a, **k: fake)

    result = LlmClient(_settings()).chat([{"role": "user", "content": "hi"}])
    assert result["content"] == "ok"
    assert result["reasoning"] == "think"
    assert result["model"] == "qwen3.8-27b-fp8"
    assert result["fallback_used"] is False


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
    fake = _ok_response()
    calls = {}

    def fake_post(*args, **kwargs):
        calls["kwargs"] = kwargs
        return fake

    monkeypatch.setattr("auto_git_review.llm.requests.post", fake_post)

    LlmClient(_settings()).chat([{"role": "user", "content": "hi"}])
    kwargs = calls["kwargs"]
    assert kwargs["json"]["model"] == "qwen3.8-27b-fp8"
    assert kwargs["headers"]["x-litellm-api-key"] == "sk-key"


def test_chat_falls_back_on_transport_error(monkeypatch):
    fake = _ok_response()
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"]["model"])
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("gateway down")
        return fake

    monkeypatch.setattr("auto_git_review.llm.requests.post", fake_post)

    result = LlmClient(_settings()).chat([{"role": "user", "content": "hi"}])
    assert calls == ["qwen3.8-27b-fp8", "qwen3.8-think"]
    assert result["model"] == "qwen3.8-think"
    assert result["fallback_used"] is True
    assert result["fallback_from"] == "qwen3.8-27b-fp8"


def test_chat_falls_back_on_server_error(monkeypatch):
    fake_fail = MagicMock()
    fake_fail.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "503", response=MagicMock(status_code=503, text="unavailable")
    )
    fake_ok = _ok_response()
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"]["model"])
        return fake_fail if len(calls) == 1 else fake_ok

    monkeypatch.setattr("auto_git_review.llm.requests.post", fake_post)

    result = LlmClient(_settings()).chat([{"role": "user", "content": "hi"}])
    assert calls == ["qwen3.8-27b-fp8", "qwen3.8-think"]
    assert result["fallback_used"] is True


def test_chat_does_not_fall_back_on_context_error(monkeypatch):
    response = MagicMock(
        status_code=400, text="maximum context length is 262144 tokens"
    )
    fake = MagicMock()
    fake.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "400", response=response
    )
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"]["model"])
        return fake

    monkeypatch.setattr("auto_git_review.llm.requests.post", fake_post)

    with pytest.raises(requests.exceptions.HTTPError):
        LlmClient(_settings()).chat([{"role": "user", "content": "hi"}])
    assert calls == ["qwen3.8-27b-fp8"]


def test_chat_does_not_fall_back_when_disabled(monkeypatch):
    fake = MagicMock()
    fake.raise_for_status.side_effect = requests.exceptions.ConnectionError("down")
    monkeypatch.setattr("auto_git_review.llm.requests.post", lambda *a, **k: fake)

    with pytest.raises(requests.exceptions.ConnectionError):
        LlmClient(_settings(llm_model_fallback="")).chat(
            [{"role": "user", "content": "hi"}]
        )


def test_should_fallback_classification():
    assert LlmClient._should_fallback(requests.exceptions.Timeout())
    assert LlmClient._should_fallback(
        requests.exceptions.HTTPError(
            "429", response=MagicMock(status_code=429, text="")
        )
    )
    assert LlmClient._should_fallback(
        requests.exceptions.HTTPError(
            "404", response=MagicMock(status_code=404, text="")
        )
    )
    assert LlmClient._should_fallback(
        requests.exceptions.HTTPError(
            "400", response=MagicMock(status_code=400, text="Invalid model name")
        )
    )
    assert not LlmClient._should_fallback(
        requests.exceptions.HTTPError(
            "401", response=MagicMock(status_code=401, text="unauthorized")
        )
    )
    assert not LlmClient._should_fallback(
        requests.exceptions.HTTPError(
            "400",
            response=MagicMock(
                status_code=400, text="maximum context length is 262144 tokens"
            ),
        )
    )
