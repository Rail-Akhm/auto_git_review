"""Прототип ДАГа: запуск ревью открытых PR по одному или нескольким репозиториям.

Каждая таска — отдельный репозиторий со своим промптом. Параметры передаются
напрямую в таску через op_kwargs (без Airflow Variables):

  - repo         — имя репозитория в Azure DevOps Server;
  - post_comment — отправлять ли резюме комментарием в PR (True/False);
  - prompt_name  — файл промпта в loader/src/auto_git_review/prompts/.

Адреса и секреты берутся из Airflow Connections:
  - api_git    : Azure DevOps Server (host, port, password=PAT)
  - llm_server : локальный LLM через LiteLLM (host, port, password=LLM API key)
"""

import os
import airflow
from datetime import timedelta

from airflow.models import DAG
from airflow.operators.python import PythonOperator
from airflow.utils.log.logging_mixin import LoggingMixin
from airflow.hooks.base import BaseHook


class DAGConfiguration:
    DAG_NAME = "SERV__auto_git_review"
    DAG_DESCRIPTION = "Автоматическое ревью открытых PR в Azure DevOps Server"
    DAG_MAX_ACTIVE_TASKS = 1

    # Airflow Connections
    CONN_ALM = "api_git"
    CONN_LLM = "llm_server"

    # Пути, которые не помещаются в стандартные поля Connection (host/port/login/password)
    ALM_BASE_PATH = "/TFS/GPN/U200001871_mkhdbrd"
    LLM_PATH = "/chat/completions"  # префикс /v1 задан в поле host коннектора llm_server

    # Список репозиториев для ревью. Каждый элемент — отдельная таска.
    # В будущем сюда добавляются новые репозитории (ClickHouse, ДАГи Airflow и т.д.)
    # со своими промптами.
    REPOSITORIES = [
        {
            "task_id": "review_greenplum",
            "repo": "U200001871_mkhdbrd_greenplum",
            "prompt_name": "review_prompt.md",
            "post_comment": True,
        },
        # Пример для будущих репозиториев:
        # {"task_id": "review_clickhouse", "repo": "<repo_clickhouse>",
        #  "prompt_name": "review_prompt_clickhouse.md", "post_comment": True},
    ]

    DAG_DEFAULT_ARGS = {
        "owner": "airflow",
        "depends_on_past": False,
        "start_date": airflow.utils.dates.days_ago(1),
        "email_on_failure": False,
        "email_on_retry": False,
        "retries": 0,
        "retry_delay": timedelta(minutes=5),
        "catchup": False,
        "max_active_runs": 1,
        "execution_timeout": timedelta(hours=2),
    }

    RESOURCE_CONFIG = {
        "KubernetesExecutor": {
            "request_memory": "1G",
            "limit_memory": "2G",
            "request_cpu": "1000m",
            "limit_cpu": "2000m",
        }
    }


DC = DAGConfiguration
log = LoggingMixin().log


def _build_url(scheme, host, port, path):
    """Собирает URL из host/port/path. host может быть уже полным URL."""
    if not host:
        return ""
    if host.startswith("http://") or host.startswith("https://"):
        base = host.rstrip("/")
    else:
        base = f"{scheme}://{host}"
        if port and str(port) not in ("80", "443"):
            base += f":{port}"
    return base + path


def _apply_connections():
    """Переносим настройки из Connections в os.environ (их читает config.py)."""
    conn_alm = BaseHook.get_connection(DC.CONN_ALM)
    conn_llm = BaseHook.get_connection(DC.CONN_LLM)

    log.info("Connection «%s»: host=%s port=%s", DC.CONN_ALM, conn_alm.host, conn_alm.port)
    log.info("Connection «%s»: host=%s port=%s", DC.CONN_LLM, conn_llm.host, conn_llm.port)

    # ALM
    if not os.environ.get("AZURE_DEVOPS_PAT"):
        os.environ["AZURE_DEVOPS_PAT"] = (conn_alm.get_password() or conn_alm.login or "").strip()
    if not os.environ.get("AZURE_DEVOPS_URL") and conn_alm.host:
        os.environ["AZURE_DEVOPS_URL"] = _build_url("https", conn_alm.host, conn_alm.port, DC.ALM_BASE_PATH)

    # LLM
    if not os.environ.get("LLM_API_KEY"):
        os.environ["LLM_API_KEY"] = (conn_llm.get_password() or conn_llm.login or "").strip()
    if not os.environ.get("LLM_URL") and conn_llm.host:
        os.environ["LLM_URL"] = _build_url("https", conn_llm.host, conn_llm.port, DC.LLM_PATH)

    log.info("AZURE_DEVOPS_URL = %s", os.environ.get("AZURE_DEVOPS_URL"))
    log.info("LLM_URL          = %s", os.environ.get("LLM_URL"))


def run_wrapper(repo=None, post_comment=False, prompt_name=None, **kwargs):
    # 1. Ищем и добавляем loader/src в sys.path (паттерн проекта).
    import sys

    root = os.path.dirname(os.path.abspath(__file__))
    loader_src = None
    for _ in range(5):
        candidate = os.path.join(root, "loader", "src")
        if os.path.isdir(candidate):
            loader_src = candidate
            break
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent

    if not loader_src:
        raise RuntimeError("Не найдена папка loader/src относительно файла ДАГа")
    sys.path.insert(1, loader_src)
    log.info("loader/src добавлен в sys.path: %s", loader_src)

    # 2. Настройки из Airflow Connections.
    _apply_connections()

    # 3. Запускаем основную функцию ревью с параметрами таски.
    from auto_git_review import review

    return review.run_review(repo=repo, post_comment=post_comment, prompt_name=prompt_name)


with DAG(
    dag_id=DC.DAG_NAME,
    description=DC.DAG_DESCRIPTION,
    schedule_interval=None,
    tags=["review", "git", "llm", "auto_git_review"],
    max_active_runs=1,
    max_active_tasks=DC.DAG_MAX_ACTIVE_TASKS,
    default_args=DC.DAG_DEFAULT_ARGS,
) as dag:
    for _cfg in DC.REPOSITORIES:
        PythonOperator(
            task_id=_cfg["task_id"],
            python_callable=run_wrapper,
            op_kwargs={
                "repo": _cfg["repo"],
                "post_comment": _cfg.get("post_comment", False),
                "prompt_name": _cfg.get("prompt_name"),
            },
            executor_config=DC.RESOURCE_CONFIG,
        )
