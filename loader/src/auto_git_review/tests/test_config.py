"""Тесты конфигурации (чтение переменных окружения)."""

from auto_git_review.config import _env_bool, get_settings


def test_env_bool_true_variants():
    for v in ("1", "true", "yes", "on", "True"):
        assert _env_bool("_X", v) is True


def test_env_bool_false_variants():
    for v in ("0", "false", "no", "off", "anything"):
        assert _env_bool("_X", v) is False


def test_get_settings_from_env(monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_URL", "https://alm.example/TFS/GPN")
    monkeypatch.setenv("AZURE_DEVOPS_PROJECT", "Proj1")
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "secret-pat")
    monkeypatch.setenv("AZURE_DEVOPS_REPO", "repo1")
    monkeypatch.setenv("LLM_URL", "https://llm.example/v1/chat/completions")
    monkeypatch.setenv("LLM_API_KEY", "sk-key")
    monkeypatch.setenv("LLM_MODEL", "qwen3:latest")
    monkeypatch.setenv("POST_COMMENTS", "true")

    s = get_settings()
    assert s.azure_url == "https://alm.example/TFS/GPN"
    assert s.azure_project == "Proj1"
    assert s.azure_pat == "secret-pat"
    assert s.azure_repo == "repo1"
    assert s.llm_model == "qwen3:latest"
    assert s.post_comments is True
