"""Загрузка конфигурации из переменных окружения / .env."""

import os
from dataclasses import dataclass

import urllib3
from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


VERIFY_SSL = _env("VERIFY_SSL", "false").lower() in ("1", "true", "yes", "on")

if not VERIFY_SSL:
    # Самоподписанные сертификаты на корпоративных хостах — осознанно выключаем проверку.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@dataclass(frozen=True)
class Settings:
    azure_url: str
    azure_pat: str
    azure_repo: str
    api_version: str
    llm_url: str
    llm_api_key: str
    llm_model: str
    verify_ssl: bool = VERIFY_SSL


def get_settings() -> Settings:
    return Settings(
        azure_url=_env("AZURE_DEVOPS_URL").rstrip("/"),
        azure_pat=_env("AZURE_DEVOPS_PAT"),
        azure_repo=_env("AZURE_DEVOPS_REPO"),
        api_version=_env("AZURE_DEVOPS_API_VERSION", "6.1-preview"),
        llm_url=_env("LLM_URL").rstrip("/"),
        llm_api_key=_env("LLM_API_KEY"),
        llm_model=_env("LLM_MODEL", "qwen3:latest"),
        verify_ssl=VERIFY_SSL,
    )
