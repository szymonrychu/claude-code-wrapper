"""README coverage of config keys and Claude env vars (D2, D16)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, get_args, get_origin

import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import to_jsonable_python

from claude_code_wrapper.claude_env import ClaudeEnv
from claude_code_wrapper.config import Config

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
CONFIG_HEADER = ["Key", "Type", "Default", "Env var", "Description"]
ENV_HEADER = ["Field", "Env var", "Type", "Secret", "Group", "Description"]


def section(title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\s*$", README, re.MULTILINE)
    assert match, f"README has no '## {title}' section"
    rest = README[match.end() :]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def table_rows(body: str, header: list[str]) -> list[list[str]]:
    """Rows of every table in `body` whose header cells equal `header`."""
    rows: list[list[str]] = []
    active = False
    for line in body.splitlines():
        if not line.startswith("|"):
            active = False
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if cells == header:
            active = True
        elif active and not set("".join(cells)) <= set("-: "):
            rows.append(cells)
    return rows


def unwrap(annotation: object) -> object:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def leaves(model: type[BaseModel], prefix: str = "") -> Iterator[tuple[str, FieldInfo]]:
    """Dotted key of every field; nested models are expanded, list and map fields are leaves."""
    for name, field in model.model_fields.items():
        inner = unwrap(field.annotation)
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            yield from leaves(inner, f"{prefix}{name}.")
        else:
            yield f"{prefix}{name}", field


def render_default(field: FieldInfo) -> str:
    """The one rendering of a default: compact JSON of its JSON form, in backticks."""
    return f"`{json.dumps(to_jsonable_python(field.get_default(call_default_factory=True)))}`"


@pytest.fixture(scope="module")
def config_rows() -> dict[str, list[str]]:
    rows = table_rows(section("Configuration reference"), CONFIG_HEADER)
    return {r[0].strip("`"): r for r in rows}


def test_sections_present() -> None:
    for title in (
        "Quick start",
        "Configuration reference",
        "Claude Code environment",
        "Docker and compose",
        "Running in Kubernetes",
        "Workbench usage",
        "Development",
    ):
        assert section(title).strip(), title
    body = section("Configuration reference")
    for text in ("${VAR}", "${VAR:-default}", "$$", "78", "ccw check-config"):
        assert text in body, text
    for top in Config.model_fields:
        assert f"### `{top}`" in body, top
    assert "purge=true" in section("Docker and compose")
    assert "resending" in section("Claude Code environment")
    k8s = section("Running in Kubernetes")
    for text in ("/healthz", "/readyz", "shutdown_timeout_seconds", "CCW_CONFIG", "Trust model"):
        assert text in k8s, text


def test_every_config_key_documented(config_rows: dict[str, list[str]]) -> None:
    keys = [k for k, _ in leaves(Config)]
    assert len(keys) > 60
    missing = [k for k in keys if k not in config_rows]
    assert not missing, f"config keys without a README table row: {missing}"
    stale = sorted(set(config_rows) - set(keys))
    assert not stale, f"README rows for keys not in the Config model: {stale}"


def test_defaults_match_model(config_rows: dict[str, list[str]]) -> None:
    wrong = {
        key: (config_rows[key][2], render_default(field))
        for key, field in leaves(Config)
        if key in config_rows and config_rows[key][2] != render_default(field)
    }
    assert not wrong, f"README default differs from the model (README, model): {wrong}"


def test_env_var_column_only_for_claude_env(config_rows: dict[str, list[str]]) -> None:
    catalogue = {s.field: s.env for s in ClaudeEnv.catalogue()}
    for key, row in config_rows.items():
        if key.startswith("claude.env.") and key != "claude.env.extra":
            assert row[3] == f"`{catalogue[key.rsplit('.', 1)[1]]}`", key
        else:
            assert row[3] == "-", key


def test_every_claude_env_var_documented_with_field() -> None:
    rows = {r[0].strip("`"): r for r in table_rows(section("Claude Code environment"), ENV_HEADER)}
    catalogue = ClaudeEnv.catalogue()
    assert len(catalogue) > 100
    for spec in catalogue:
        assert spec.field in rows, f"{spec.env} has no row"
        assert rows[spec.field][1] == f"`{spec.env}`", spec.field
        assert rows[spec.field][2] == spec.type, spec.field
        assert rows[spec.field][4] == spec.group, spec.field
    assert set(rows) == {s.field for s in catalogue}


def test_secret_env_vars_marked() -> None:
    rows = {r[0].strip("`"): r for r in table_rows(section("Claude Code environment"), ENV_HEADER)}
    assert any(s.secret for s in ClaudeEnv.catalogue())
    for spec in ClaudeEnv.catalogue():
        assert rows[spec.field][3] == ("yes" if spec.secret else "no"), spec.env
