"""Тесты рендеринга промптов."""

from auto_git_review.prompt import render_prompt


def test_render_prompt_substitutes_lowercase_key():
    assert render_prompt("Hello [[NAME]]", name="World") == "Hello World"


def test_render_prompt_multiple_placeholders():
    template = "WI: [[WORK_ITEM]]\nFILES:\n[[FILES]]"
    out = render_prompt(template, work_item="task 1", files="f.sql")
    assert "task 1" in out
    assert "f.sql" in out


def test_render_prompt_no_placeholders_unchanged():
    assert render_prompt("static text") == "static text"
