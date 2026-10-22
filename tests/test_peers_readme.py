import re
from pathlib import Path

README = (Path(__file__).resolve().parent.parent / "README.md").read_text()


def h2(title: str) -> str:
    parts = re.split(r"^## ", README, flags=re.M)
    for p in parts:
        if p.startswith(title + "\n"):
            return p
    raise AssertionError(title)


def h3(title: str) -> str:
    parts = re.split(r"^#{2,3} ", README, flags=re.M)
    for p in parts:
        if p.startswith(title + "\n"):
            return p
    raise AssertionError(title)


def test_peer_messaging_section_terms():
    body = h2("Peer messaging")
    for term in (
        "<instance>/<session id>",
        "TLS",
        "at most once",
        "unique",
        "X-CCW-Peer-Signature",
        "peers.static",
        "--peer",
        "preJoin",
        "postLeave",
        "max_missed_heartbeats",
        "CLAUDE_CODE_HARBOR_KITE",
        "SendMessage",
        "notify_when_idle",
        "max_clock_skew_seconds",
        "(#prejoin)",
    ):
        assert term in body, term


def test_trust_model_phrases():
    body = h2("Peer messaging")
    for term in ("trust domain", "/internal/mcp", "process env"):
        assert term in body, term
    assert "Peer messaging" in h2("Trust model")


def test_mcp_tools_documented():
    body = h3("`POST /internal/mcp`")
    for term in ("send_message", "peers", "peer unreachable", "unknown peer"):
        assert term in body, term


def test_command_tool_reason_reworded():
    assert "peers.enabled" in h3("The ccw command tool")


def test_peer_origin_listed():
    assert "`peer`" in h2("Overview")
    assert "`peer`" in h3("`turn_started`")
    assert "`peer`" in h3("`onTurn`")


def test_compose_and_kubernetes():
    assert "docker-compose.peers.yml" in h2("Docker and compose")
    assert "headless" in h2("Running in Kubernetes")
