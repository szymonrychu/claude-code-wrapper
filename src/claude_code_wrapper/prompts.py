"""Prompt templates (D14): `{{name}}` placeholders with required and default variables.

`{{name}}` never clashes with the `${VAR}` interpolation applied to config.yaml, so a template
reaches this module verbatim. Values are inserted in one pass and never re-expanded.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from claude_code_wrapper.config import _PLACEHOLDER, Config, PromptsConfig, PromptTemplate

TemplatesSource = Config | PromptsConfig | Mapping[str, PromptTemplate]

_MESSAGES = {
    "unknown_template": "unknown template {name!r}",
    "unknown_variable": "unknown variable {name!r}",
    "missing_variable": "missing required variable {name!r}",
    "text_or_template": "exactly one of text or template is required",
}


class PromptError(ValueError):
    """A prompt cannot be built. `code`: unknown_template, unknown_variable, missing_variable
    or text_or_template; `name` is the template or variable concerned, when there is one."""

    def __init__(self, code: str, name: str | None = None) -> None:
        self.code = code
        self.name = name
        super().__init__(_MESSAGES.get(code, code).format(name=name))


def _templates(source: TemplatesSource) -> Mapping[str, PromptTemplate]:
    if isinstance(source, Config):
        return source.prompts.templates
    if isinstance(source, PromptsConfig):
        return source.templates
    return source


def render_template(
    templates_cfg: TemplatesSource, name: str, variables: Mapping[str, Any] | None = None
) -> str:
    """Render template `name`; supplied values win over defaults, optional ones default to ''."""
    template = _templates(templates_cfg).get(name)
    if template is None:
        raise PromptError("unknown_template", name)
    supplied = dict(variables or {})
    for var in supplied:
        if var not in template.variables:
            raise PromptError("unknown_variable", var)
    values: dict[str, str] = {}
    for var, spec in template.variables.items():
        if var in supplied and supplied[var] is not None:
            values[var] = str(supplied[var])
        elif spec.default is not None:
            values[var] = spec.default
        elif spec.required:
            raise PromptError("missing_variable", var)
        else:
            values[var] = ""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template.text)


def list_templates(cfg: TemplatesSource) -> list[dict[str, Any]]:
    """`[{name, description, variables: {name: {required, default}}}]`, sorted by name."""
    return [
        {
            "name": name,
            "description": template.description,
            "variables": {
                var: {"required": spec.required, "default": spec.default}
                for var, spec in template.variables.items()
            },
        }
        for name, template in sorted(_templates(cfg).items())
    ]


def resolve_prompt(
    cfg: TemplatesSource,
    *,
    text: str | None = None,
    template: str | None = None,
    variables: Mapping[str, Any] | None = None,
    required: bool = True,
) -> str | None:
    """The prompt text from exactly one of `text` or `template`.

    Neither is a `text_or_template` error unless `required=False`, where it means no prompt
    (None). Variables only go with a template.
    """
    if text is not None and template is not None:
        raise PromptError("text_or_template")
    if template is not None:
        return render_template(cfg, template, variables)
    if variables:
        raise PromptError("unknown_variable", next(iter(variables)))
    if text is None and required:
        raise PromptError("text_or_template")
    return text
