"""Конфигурация: читается из переменных окружения.

В Airflow переменные окружения подставляются из Connections (см. ДАГ):
  - api_git    -> AZURE_DEVOPS_URL, AZURE_DEVOPS_PAT
  - llm_server -> LLM_URL, LLM_API_KEY
"""

import os
from dataclasses import dataclass

import urllib3

VERIFY_SSL = os.environ.get("VERIFY_SSL", "false").lower() in ("1", "true", "yes", "on")

if not VERIFY_SSL:
    # Самоподписанные сертификаты на корпоративных хостах — осознанно выключаем проверку.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Корпоративные константы (НЕ секреты) — дефолты, чтобы задавать только секреты.
AZURE_URL_DEFAULT = "https://alm-itsk.gazprom-neft.local:8080/TFS/GPN/U200001871_mkhdbrd"
AZURE_REPO_DEFAULT = "U200001871_mkhdbrd_greenplum"
LLM_URL_DEFAULT = "https://spb99akl-dgx02.gazprom-neft.local/v1/chat/completions"


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
        azure_url=os.environ.get("AZURE_DEVOPS_URL", AZURE_URL_DEFAULT).rstrip("/"),
        azure_pat=os.environ.get("AZURE_DEVOPS_PAT", "").strip(),
        azure_repo=os.environ.get("AZURE_DEVOPS_REPO", AZURE_REPO_DEFAULT),
        api_version=os.environ.get("AZURE_DEVOPS_API_VERSION", "6.1-preview"),
        llm_url=os.environ.get("LLM_URL", LLM_URL_DEFAULT).rstrip("/"),
        llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
        llm_model=os.environ.get("LLM_MODEL", "qwen3:latest"),
        verify_ssl=VERIFY_SSL,
    )
