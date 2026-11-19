"""Запись метрик ревью в GreenPlum (одна строка на ревью PR).

Таблица: ``prj_brddwh.serv_git_auto_review`` (по умолчанию), подключение —
Airflow Connection ``edw_klad-pbrddw`` (Postgres-совместимый драйвер psycopg2).

Запись best-effort: любая ошибка БД/драйвера логируется, но не валит ревью.
Идемпотентность: перед INSERT удаляется строка с тем же
(project, repo, pr_id, source_commit) — повторный прогон не плодит дубли,
а перезаписывает результат для того же коммита.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger("auto_git_review.metrics")

DEFAULT_CONN_ID = "edw_klad-pbrddw"
DEFAULT_TABLE = "prj_brddwh.serv_git_auto_review"

_COLUMNS = (
    "project",
    "repo",
    "pr_id",
    "source_commit",
    "review_thread_id",
    "review_date",
    "review_verdict",
    "content",
    "context_chars",
    "tokens",
    "latency_ms",
    "llm_latency_ms",
)

_TABLE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$"
)


def _enabled() -> bool:
    return os.environ.get("METRICS_ENABLED", "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _table() -> str:
    return os.environ.get("METRICS_TABLE", DEFAULT_TABLE).strip()


def _conn_id() -> str:
    return os.environ.get("METRICS_DB_CONN_ID", DEFAULT_CONN_ID).strip()


def _row_values(metrics: dict) -> tuple:
    """Готовит значения строки в порядке _COLUMNS (review_date — UTC naive)."""
    review_date = metrics.get("review_date")
    if review_date is None:
        review_date = datetime.now(timezone.utc).replace(tzinfo=None)
    elif isinstance(review_date, datetime) and review_date.tzinfo is not None:
        review_date = review_date.astimezone(timezone.utc).replace(tzinfo=None)

    thread_id = metrics.get("review_thread_id")
    if thread_id is not None:
        thread_id = int(thread_id)

    return (
        metrics.get("project", ""),
        metrics.get("repo", ""),
        int(metrics.get("pr_id")),
        str(metrics.get("source_commit") or ""),
        thread_id,
        review_date,
        metrics.get("review_verdict", ""),
        int(metrics.get("content") or 0),
        int(metrics.get("context_chars") or 0),
        int(metrics.get("tokens") or 0),
        int(metrics.get("latency_ms") or 0),
        int(metrics.get("llm_latency_ms") or 0),
    )


def _connect():
    """Подключение к GreenPlum из Airflow Connection (psycopg2, ленивый импорт)."""
    try:
        import psycopg2
    except ImportError as exc:  # в окружении воркера должен быть psycopg2
        raise RuntimeError(
            "psycopg2 не установлен в окружении воркера — нужен для записи метрик"
        ) from exc

    from airflow.hooks.base import BaseHook

    conn = BaseHook.get_connection(_conn_id())
    extra = json.loads(conn.extra or "{}")
    dbname = extra.get("dbname") or extra.get("database") or conn.schema
    if not dbname:
        raise RuntimeError(
            f"В Connection «{_conn_id()}» не задано имя БД "
            f"(поле schema или extra.dbname)"
        )

    return psycopg2.connect(
        host=conn.host,
        port=conn.port or 5432,
        dbname=dbname,
        user=conn.login,
        password=conn.get_password(),
        connect_timeout=10,
        application_name="auto_git_review",
    )


def record_review(metrics: dict) -> bool:
    """Записать одну строку метрик ревью. Возвращает True при успехе.

    Никогда не поднимает исключение наружу: ошибка метрик не должна ломать ревью.
    """
    if not _enabled() or not metrics:
        return False

    source_commit = str(metrics.get("source_commit") or "").strip()
    if not source_commit:
        logger.warning(
            "Метрики PR #%s не записаны: пустой source_commit",
            metrics.get("pr_id"),
        )
        return False

    table = _table()
    if not _TABLE_RE.match(table):
        logger.warning("Некорректное имя таблицы метрик: %r", table)
        return False

    placeholders = ", ".join(["%s"] * len(_COLUMNS))
    insert_sql = (
        f"INSERT INTO {table} ({', '.join(_COLUMNS)}) VALUES ({placeholders})"
    )
    delete_sql = (
        f"DELETE FROM {table} "
        "WHERE project = %s AND repo = %s AND pr_id = %s AND source_commit = %s"
    )

    try:
        values = _row_values(metrics)
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(delete_sql, values[:4])
                cursor.execute(insert_sql, values)
            conn.commit()
    except Exception as exc:
        logger.warning(
            "Не удалось записать метрики ревью PR #%s в %s: %s",
            metrics.get("pr_id"),
            table,
            exc,
        )
        return False

    logger.info(
        "Метрики ревью PR #%s сохранены в %s (verdict=%s, tokens=%s)",
        metrics.get("pr_id"),
        table,
        metrics.get("review_verdict"),
        metrics.get("tokens"),
    )
    return True
