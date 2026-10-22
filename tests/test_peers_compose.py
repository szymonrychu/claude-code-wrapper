import subprocess
from pathlib import Path

import pytest
import yaml

from claude_code_wrapper.config import load_config

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "examples" / "docker-compose.peers.yml"
LINK = ROOT / "examples" / "peers.config.yaml"
DATA = ROOT / "src" / "claude_code_wrapper" / "data" / "examples"


@pytest.fixture
def compose():
    return yaml.safe_load(COMPOSE.read_text())


def test_two_services_with_distinct_ports(compose):
    services = compose["services"]
    assert set(services) == {"ccw-a", "ccw-b"}
    ports = [services[n]["ports"][0] for n in ("ccw-a", "ccw-b")]
    assert len(set(ports)) == 2
    assert all(p.startswith("127.0.0.1:") and p.endswith(":8080") for p in ports)


def test_names_seeds_and_token(compose):
    a, b = (compose["services"][n]["environment"] for n in ("ccw-a", "ccw-b"))
    assert (a["CCW_PEER_NAME"], b["CCW_PEER_NAME"]) == ("a", "b")
    assert a["CCW_PEER_URL"] == b["CCW_PEER_SEED"] == "http://ccw-a:8080"
    assert b["CCW_PEER_URL"] == a["CCW_PEER_SEED"] == "http://ccw-b:8080"
    for env in (a, b):
        assert env["CCW_PEER_TOKEN"] == "${CCW_PEER_TOKEN:?set CCW_PEER_TOKEN}"
        assert env["CCW_AUTH_TOKEN"] == "${CCW_AUTH_TOKEN:?set CCW_AUTH_TOKEN}"


def test_one_build_and_shared_image(compose):
    a, b = compose["services"]["ccw-a"], compose["services"]["ccw-b"]
    builds = [s for s in (a, b) if "build" in s]
    assert builds == [a]
    assert a["build"] == ".."
    assert a["image"] == b["image"] == "claude-code-wrapper:local"
    assert b["pull_policy"] == "never"


def test_no_shared_named_volume(compose):
    def named(svc):
        return {v.split(":")[0] for v in svc["volumes"] if not v.startswith((".", "/", "~", "$"))}

    a, b = (named(compose["services"][n]) for n in ("ccw-a", "ccw-b"))
    assert len(a) == 3 and len(b) == 3
    assert not a & b
    assert (a | b) <= set(compose["volumes"])
    for n in ("ccw-a", "ccw-b"):
        assert "./peers.config.yaml:/etc/ccw/config.yaml:ro" in compose["services"][n]["volumes"]


def test_example_is_committed_symlink():
    out = subprocess.run(
        ["git", "ls-files", "-s", "examples/peers.config.yaml"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.startswith("120000 ")
    assert (LINK.parent / LINK.readlink()).resolve().parent == DATA.resolve()


def test_example_loads_with_peers(monkeypatch):
    env = {
        "CCW_AUTH_TOKEN": "auth-token-0123456789",
        "CCW_PEER_TOKEN": "peer-token-abcdefghij",
        "CCW_PEER_NAME": "a",
        "CCW_PEER_URL": "http://ccw-a:8080",
        "CCW_PEER_SEED": "http://ccw-b:8080",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = load_config(LINK)
    assert cfg.peers.enabled
    assert cfg.peers.static == ["http://ccw-b:8080"]
    assert cfg.peers.name == "a"


def test_compose_example_two_instances(compose):
    test_two_services_with_distinct_ports(compose)
    test_names_seeds_and_token(compose)
    test_one_build_and_shared_image(compose)
    test_no_shared_named_volume(compose)


def test_example_config_symlink(monkeypatch):
    test_example_is_committed_symlink()
    test_example_loads_with_peers(monkeypatch)
