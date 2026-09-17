"""Загрузка и рендеринг промптов из отдельных файлов (легко править без кода)."""

from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(name: str = "review_prompt_greenplum.md") -> str:
    """Читает текст промпта из prompts/ по имени файла."""
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def render_prompt(template: str, **values) -> str:
    """Подставляет плейсхолдеры [[КЛЮЧ]] значениями из values (ключи — в верхнем регистре)."""
    result = template
    for key, value in values.items():
        result = result.replace(f"[[{key.upper()}]]", str(value))
    return result
