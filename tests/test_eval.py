"""Тесты расчёта метрик оценки качества."""

import pytest

from auto_git_review.eval import compute_metrics, evaluate, load_dataset


def test_compute_metrics_perfect_accuracy():
    metrics = compute_metrics(["approve", "request_changes"], ["approve", "request_changes"])
    assert metrics["accuracy"] == 1.0
    assert metrics["correct"] == 2


def test_compute_metrics_accuracy_and_confusion():
    expected = ["approve", "approve", "request_changes"]
    predicted = ["approve", "request_changes", "request_changes"]
    metrics = compute_metrics(expected, predicted)
    # 2 из 3 верны
    assert metrics["accuracy"] == pytest.approx(2 / 3)
    # request_changes: tp=1, fp=1, fn=0 -> precision 0.5, recall 1.0
    assert metrics["precision"]["request_changes"] == pytest.approx(0.5)
    assert metrics["recall"]["request_changes"] == pytest.approx(1.0)


def test_compute_metrics_unknown_class_not_counted():
    metrics = compute_metrics(["approve", "weird"], ["approve", "comment"])
    assert metrics["accuracy"] == pytest.approx(0.5)


def test_compute_metrics_empty():
    metrics = compute_metrics([], [])
    assert metrics["accuracy"] == 0.0
    assert metrics["total"] == 0


def test_compute_metrics_length_mismatch_raises():
    with pytest.raises(ValueError):
        compute_metrics(["approve"], ["approve", "comment"])


def test_evaluate_uses_reviewer_callable():
    dataset = [
        {"id": "a", "verdict_expected": "approve"},
        {"id": "b", "verdict_expected": "comment"},
    ]
    metrics, per_case = evaluate(lambda case: case["verdict_expected"], dataset)
    assert metrics["accuracy"] == 1.0
    assert per_case[0]["correct"] is True


def test_load_dataset_wrapped_in_cases(tmp_path):
    p = tmp_path / "ds.json"
    p.write_text('{"cases": [{"id": "x", "verdict_expected": "approve"}]}', encoding="utf-8")
    assert load_dataset(p)[0]["id"] == "x"
