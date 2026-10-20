"""Prompt templates (D14): `{{name}}` placeholders, required and default variables."""

from __future__ import annotations

import pytest

from claude_code_wrapper.config import Config, PromptsConfig
from claude_code_wrapper.prompts import PromptError, list_templates, render_template, resolve_prompt

TEMPLATES = {
    "review": {
        "description": "Review a change",
        "text": "Review {{ target }} for {{focus}}. Again: {{target}}.",
        "variables": {
            "target": {"required": True},
            "focus": {"default": "bugs"},
        },
    },
    "plain": {"text": "Say hi to {{who}}", "variables": {"who": {}}},
}


def cfg() -> Config:
    return Config.model_validate({"prompts": {"templates": TEMPLATES}})


def test_render_variables_and_defaults() -> None:
    c = cfg()
    assert (
        render_template(c.prompts.templates, "review", {"target": "main.py"})
        == "Review main.py for bugs. Again: main.py."
    )
    # A supplied value wins over the default; the whole config and PromptsConfig work too.
    assert (
        render_template(c, "review", {"target": "a", "focus": "style"})
        == "Review a for style. Again: a."
    )
    assert render_template(c.prompts, "review", {"target": "b"}).startswith("Review b for bugs")
    # An optional variable with no default renders empty.
    assert render_template(c, "plain", None) == "Say hi to "
    # Values are inserted once, never re-expanded.
    assert render_template(c, "plain", {"who": "{{who}}"}) == "Say hi to {{who}}"
    assert list_templates(c) == [
        {
            "name": "plain",
            "description": "",
            "variables": {"who": {"required": False, "default": None}},
        },
        {
            "name": "review",
            "description": "Review a change",
            "variables": {
                "target": {"required": True, "default": None},
                "focus": {"required": False, "default": "bugs"},
            },
        },
    ]
    assert list_templates(PromptsConfig()) == []


def test_missing_and_unknown_variables() -> None:
    c = cfg()
    with pytest.raises(PromptError) as exc:
        render_template(c, "review", {})
    assert exc.value.code == "missing_variable"
    assert exc.value.name == "target"
    assert "target" in str(exc.value)

    with pytest.raises(PromptError) as exc:
        render_template(c, "review", {"target": "x", "colour": "red"})
    assert exc.value.code == "unknown_variable"
    assert exc.value.name == "colour"

    with pytest.raises(PromptError) as exc:
        render_template(c, "nope", {})
    assert exc.value.code == "unknown_template"
    assert exc.value.name == "nope"


def test_text_or_template() -> None:
    c = cfg()
    assert resolve_prompt(c, text="hello") == "hello"
    assert resolve_prompt(c, template="review", variables={"target": "t"}).startswith("Review t")
    for kwargs in ({}, {"text": "a", "template": "review"}):
        with pytest.raises(PromptError) as exc:
            resolve_prompt(c, **kwargs)
        assert exc.value.code == "text_or_template"
    # Neither is allowed where a prompt is optional (session create): no prompt.
    assert resolve_prompt(c, required=False) is None
    with pytest.raises(PromptError) as exc:
        resolve_prompt(c, text="a", template="review", required=False)
    assert exc.value.code == "text_or_template"
    # Variables belong to a template.
    with pytest.raises(PromptError) as exc:
        resolve_prompt(c, text="a", variables={"x": "1"})
    assert exc.value.code == "unknown_variable"
    with pytest.raises(PromptError) as exc:
        resolve_prompt(c, template="review")
    assert exc.value.code == "missing_variable"
