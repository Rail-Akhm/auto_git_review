"""Сбор и вывод сводных метрик прогона ревью (вердикты, latency, токены, стоимость).

Метрики пишутся одной структурированной JSON-строкой, пригодной для разбора
системами мониторинга (ELK, Prometheus exporter и т.п.).
"""

import json

# Цена за 1000 токенов локальной модели (заполняется по фактическому тарифу).
# Ноль по умолчанию — cost не считается, пока тариф не задан.
COST_PER_1K_TOKENS = 0.0


def summarize(results: list) -> dict:
    """Агрегирует список результатов PR в сводные метрики прогона.

    Каждый элемент ``results``: {pr_id, verdict, latency_ms?, tokens?, ...}.
    """
    by_verdict = {}
    total_latency_ms = 0
    total_tokens = 0
    for r in results:
        verdict = r.get("verdict", "unknown")
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
        total_latency_ms += r.get("latency_ms", 0)
        total_tokens += r.get("tokens", 0)

    return {
        "total_pr": len(results),
        "by_verdict": by_verdict,
        "total_latency_ms": total_latency_ms,
        "total_tokens": total_tokens,
        "estimated_cost": total_tokens / 1000 * COST_PER_1K_TOKENS,
    }


def emit_summary(log, summary: dict) -> None:
    """Логирует сводку прогона одной структурированной JSON-строкой."""
    log.info("REVIEW_RUN_METRICS %s", json.dumps(summary, ensure_ascii=False))
