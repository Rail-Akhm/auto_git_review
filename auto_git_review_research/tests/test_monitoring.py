"""Тесты сводных метрик прогона."""

from auto_git_review.monitoring import summarize


def test_summarize_counts_verdicts_and_totals():
    results = [
        {"pr_id": 1, "verdict": "approve", "latency_ms": 100, "tokens": 500},
        {"pr_id": 2, "verdict": "request_changes", "latency_ms": 200, "tokens": 300},
        {"pr_id": 3, "verdict": "approve", "latency_ms": 50, "tokens": 200},
    ]
    s = summarize(results)
    assert s["total_pr"] == 3
    assert s["by_verdict"] == {"approve": 2, "request_changes": 1}
    assert s["total_latency_ms"] == 350
    assert s["total_tokens"] == 1000


def test_summarize_empty():
    s = summarize([])
    assert s["total_pr"] == 0
    assert s["by_verdict"] == {}
    assert s["total_tokens"] == 0
