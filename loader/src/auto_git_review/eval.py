"""Оценка качества ревьюера на eval-датасете (golden set).

Модуль не требует доступа к ALM/LLM: чистые функции расчёта метрик тестируются
локально. Прогон с живой моделью выполняется только в корпоративном контуре —
через передаваемый извне callable ``reviewer(case) -> verdict``.
"""

import json
from pathlib import Path

# Канонические вердикты (совпадают с форматом ответа модели).
VERDICTS = ["approve", "request_changes", "comment"]


def load_dataset(path) -> list:
    """Загружает eval-датасет из JSON-файла.

    Принимает либо список кейсов напрямую, либо объект вида {"cases": [...]}.
    Кейс: {id, verdict_expected, ...}.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "cases" in data:
        data = data["cases"]
    return data


def compute_metrics(expected: list, predicted: list, labels=None) -> dict:
    """Расчёт accuracy, precision/recall/F1 по классам и confusion matrix.

    ``expected`` и ``predicted`` — списки вердиктов одинаковой длины.
    """
    labels = labels or VERDICTS
    if len(expected) != len(predicted):
        raise ValueError("expected и predicted должны быть одной длины")

    total = len(expected)
    correct = sum(1 for e, p in zip(expected, predicted) if e == p)
    accuracy = correct / total if total else 0.0

    confusion = {label: {other: 0 for other in labels} for label in labels}
    for e, p in zip(expected, predicted):
        if e in confusion and p in confusion[e]:
            confusion[e][p] += 1

    precision, recall, f1 = {}, {}, {}
    for label in labels:
        tp = confusion[label][label]
        fp = sum(confusion[e][label] for e in labels if e != label)
        fn = sum(confusion[label][p] for p in labels if p != label)
        precision[label] = tp / (tp + fp) if (tp + fp) else 0.0
        recall[label] = tp / (tp + fn) if (tp + fn) else 0.0
        denom = precision[label] + recall[label]
        f1[label] = 2 * precision[label] * recall[label] / denom if denom else 0.0

    return {
        "total": total,
        "correct": correct,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "confusion": confusion,
    }


def evaluate(reviewer, dataset: list) -> dict:
    """Прогоняет ``reviewer`` по датасету и возвращает (metrics, per_case_results).

    ``reviewer`` — callable, принимающий кейс и возвращающий предсказанный вердикт.
    """
    expected = []
    predicted = []
    per_case = []
    for case in dataset:
        expected_verdict = case.get("verdict_expected")
        predicted_verdict = reviewer(case)
        expected.append(expected_verdict)
        predicted.append(predicted_verdict)
        per_case.append(
            {
                "id": case.get("id"),
                "expected": expected_verdict,
                "predicted": predicted_verdict,
                "correct": expected_verdict == predicted_verdict,
            }
        )
    return compute_metrics(expected, predicted), per_case


def format_report(metrics: dict, per_case: list) -> str:
    """Формирует markdown-отчёт об оценке качества."""
    lines = [
        "## Отчёт об оценке качества ревьюера",
        "",
        f"- Всего кейсов: {metrics['total']}",
        f"- Верных вердиктов: {metrics['correct']}",
        f"- **Accuracy: {metrics['accuracy']:.2%}**",
        "",
        "| Класс | Precision | Recall | F1 |",
        "|---|---|---|---|",
    ]
    for label in VERDICTS:
        lines.append(
            f"| {label} | {metrics['precision'][label]:.2%} "
            f"| {metrics['recall'][label]:.2%} | {metrics['f1'][label]:.2%} |"
        )
    lines.append("")
    lines.append("### По кейсам")
    lines.append("")
    lines.append("| id | expected | predicted | верно |")
    lines.append("|---|---|---|---|")
    for c in per_case:
        mark = "да" if c["correct"] else "нет"
        lines.append(
            f"| {c['id']} | {c['expected']} | {c['predicted']} | {mark} |"
        )
    return "\n".join(lines)


def write_report(metrics: dict, per_case: list, path) -> None:
    """Сохраняет отчёт об оценке качества в markdown-файл."""
    Path(path).write_text(format_report(metrics, per_case), encoding="utf-8")
