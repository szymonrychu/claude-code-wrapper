"""README coverage: undocumented routes, events, hooks and error codes fail the suite (D16)."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute, APIWebSocketRoute

from claude_code_wrapper.api.app import create_app
from claude_code_wrapper.config import HOOK_EVENTS, Config
from claude_code_wrapper.events import EVENT_TYPES
from claude_code_wrapper.internal_api import create_internal_app
from claude_code_wrapper.redact import Redactor

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "claude_code_wrapper"
README = (ROOT / "README.md").read_text(encoding="utf-8")


class _StubManager:
    started = True

    def __init__(self) -> None:
        self.redactor = Redactor()


def section(title: str) -> str:
    """The body of the `## title` section (up to the next H2)."""
    match = re.search(rf"^## {re.escape(title)}\s*$", README, re.MULTILINE)
    assert match, f"README has no '## {title}' section"
    rest = README[match.end() :]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def h3_headings(title: str) -> set[str]:
    return set(re.findall(r"^### (.+?)\s*$", section(title), re.MULTILINE))


def _walk(router, prefix: str = "", kind: type = APIRoute):
    """(path, methods) of every `kind` route; FastAPI includes routers lazily, so descend into
    them. A WebSocket route has the single method `WS`."""
    for route in router.routes:
        if isinstance(route, kind):
            yield prefix + route.path, getattr(route, "methods", None) or {"WS"}
        elif hasattr(route, "original_router"):
            yield from _walk(route.original_router, prefix + route.include_context.prefix, kind)


def _doc_path(path: str) -> str:
    """The README spelling of a route path: `{id}` for the session id, no `:path` convertors."""
    return re.sub(r"\{(\w+):\w+\}", r"{\1}", path).replace("{session_id}", "{id}")


def route_headings(app, kind: type = APIRoute) -> set[str]:
    out = set()
    for path, methods in _walk(app, kind=kind):
        for method in sorted(methods - {"HEAD"}):
            out.add(f"`{method} {_doc_path(path)}`")
    return out


def test_required_sections_exist() -> None:
    for title in (
        "Overview",
        "Trust model",
        "Architecture",
        "API reference",
        "Events (SSE)",
        "Hooks",
        "Errors",
    ):
        assert section(title).strip(), title


def test_every_public_route_documented() -> None:
    app = create_app(_StubManager(), Config.model_validate({"server": {"allow_anonymous": True}}))
    expected = route_headings(app)
    assert len(expected) >= 17
    missing = expected - h3_headings("API reference")
    assert not missing, f"undocumented public routes: {sorted(missing)}"


def test_every_websocket_route_documented() -> None:
    app = create_app(_StubManager(), Config.model_validate({"server": {"allow_anonymous": True}}))
    expected = route_headings(app, APIWebSocketRoute)
    assert "`WS /v1/sessions/{id}/terminals/{terminal_id}/attach`" in expected
    headings = h3_headings("API reference")
    missing = expected - headings
    assert not missing, f"undocumented WebSocket routes: {sorted(missing)}"
    stale = {h for h in headings if h.startswith("`WS ")} - expected
    assert not stale, f"README documents WebSocket routes the app lacks: {sorted(stale)}"


def test_every_internal_route_documented() -> None:
    expected = route_headings(create_internal_app(_StubManager()))  # type: ignore[arg-type]
    assert expected
    missing = expected - h3_headings("API reference")
    assert not missing, f"undocumented internal routes: {sorted(missing)}"


def test_every_event_type_documented() -> None:
    missing = {f"`{t}`" for t in EVENT_TYPES} - h3_headings("Events (SSE)")
    assert not missing, f"undocumented event types: {sorted(missing)}"


def test_every_hook_event_documented() -> None:
    missing = {f"`{e}`" for e in HOOK_EVENTS} - h3_headings("Hooks")
    assert not missing, f"undocumented hook events: {sorted(missing)}"


def _error_codes() -> set[str]:
    """Every code the API can answer with, scraped from where each is raised."""

    def read(*parts: str) -> str:
        return SRC.joinpath(*parts).read_text(encoding="utf-8")

    codes: set[str] = set()
    for path in [*(SRC / "api").glob("*.py"), SRC / "internal_api.py"]:
        text = path.read_text(encoding="utf-8")
        codes.update(re.findall(r"ApiError\(\s*\d+,\s*\"([a-z_]+)\"", text))
        codes.update(re.findall(r"mapped\(\w+,\s*\d+,\s*\"([a-z_]+)\"", text))
    codes.update(re.findall(r"\d+: \"([a-z_]+)\"", read("api", "errors.py")))
    codes.update(re.findall(r"respond\(\d+, \"([a-z_]+)\"", read("api", "errors.py")))
    codes.update(re.findall(r"^    code = \"([a-z_]+)\"", read("manager.py"), re.MULTILINE))
    codes.update(re.findall(r"PromptError\(\s*\"([a-z_]+)\"", read("prompts.py")))
    codes.update(re.findall(r"FileWriteError\(\s*\"([a-z_]+)\"", read("workdir.py")))
    codes.update(re.findall(r"FileError\(\s*\"([a-z_]+)\"", read("files.py")))
    codes.update(re.findall(r"\"(busy|booting|dead)\"", read("api", "turns.py")))
    codes.update(_terminal_error_codes())
    codes.add("http_error")  # errors.py fallback for any other HTTP status
    codes.discard("error")  # ManagerError's abstract base code
    return codes


def _terminal_error_codes() -> set[str]:
    """Codes of the registry errors the terminal routes answer with over HTTP (4xx and 5xx);
    the abstract base and the WebSocket-only overflow code are not HTTP answers."""
    from claude_code_wrapper.api.terminals import TerminalError

    pending, codes = list(TerminalError.__subclasses__()), set()
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if 400 <= cls.status < 600:
            codes.add(cls.code)
    return codes


def test_error_codes_documented() -> None:
    codes = _error_codes()
    assert {"unauthorized", "not_found", "too_large", "text_or_template", "vetoed"} <= codes
    assert {"terminal_limit", "terminal_not_found", "session_not_active", "invalid_size"} <= codes
    rows = {
        m.group(1) for m in re.finditer(r"^\|\s*`([a-z_]+)`\s*\|", section("Errors"), re.MULTILINE)
    }
    missing = codes - rows
    assert not missing, f"error codes missing from the Errors table: {sorted(missing)}"


def test_trust_model_and_callback_contract_documented() -> None:
    trust = section("Trust model").lower()
    for phrase in (
        "cluster-internal",
        "public ingress",
        "one wrapper instance per",
        "fully trusted",
    ):
        assert phrase in trust, phrase
    assert "user_message" in trust
    assert "history" in trust
    assert "redactor" in trust
    assert "callbackUrl" in README
    assert "callback_secret" in README
    assert "onTurnComplete" in section("Hooks")
