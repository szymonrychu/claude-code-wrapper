"""`${VAR}`, `${VAR:-default}` and `$$` interpolation for config string scalars (D8)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_PATTERN = re.compile(r"\$\$|\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")


class InterpolationError(ValueError):
    """An unset variable without default; carries the dotted YAML path and variable name."""

    def __init__(self, path: str, var: str) -> None:
        self.path = path
        self.var = var
        super().__init__(f"{path or '<root>'}: environment variable {var} is not set")


def _interpolate_str(text: str, env: Mapping[str, str], path: str) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group("name")
        if name is None:
            return "$"
        value = env.get(name)
        default = match.group("default")
        if default is not None:
            return value if value else default.replace("$$", "$")
        if value is None:
            raise InterpolationError(path, name)
        return value

    return _PATTERN.sub(repl, text)


def interpolate(obj: Any, env: Mapping[str, str], path: str = "") -> Any:
    """Rewrite string scalars in dicts and lists; keys and non-strings are untouched."""
    if isinstance(obj, str):
        return _interpolate_str(obj, env, path)
    if isinstance(obj, dict):
        return {k: interpolate(v, env, f"{path}.{k}" if path else str(k)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [interpolate(v, env, f"{path}.{i}" if path else str(i)) for i, v in enumerate(obj)]
    return obj
