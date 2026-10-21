"""Tatara example, lifecycle: bootstrap, TTL gate, safety and metrics pushes, pod policy."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import World, wait_turn

from claude_code_wrapper.config import Config
from claude_code_wrapper.interpolation import interpolate
from claude_code_wrapper.manager import CreateSessionRequest
from claude_code_wrapper.router import TurnVetoed

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "src/claude_code_wrapper/data/examples/tatara.config.yaml"
SCRIPT = ROOT / "src/claude_code_wrapper/data/examples/tatara/tatara_hooks.py"
SLEEP = {"turns": [{"match": "go", "steps": [{"sleep": 30}]}]}


def sh(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def example(env: dict[str, str]) -> dict[str, Any]:
    base = {"TATARA_PYTHON": sys.executable, "TATARA_HOOKS": str(SCRIPT)}
    return interpolate(yaml.safe_load(EXAMPLE.read_text()), {**base, **env})


def make_remote(root: Path, owner: str, name: str, branches: tuple[str, ...] = ()) -> Path:
    """A bare remote at root/remotes/<owner>/<name>.git holding main plus `branches`."""
    bare = root / "remotes" / owner / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    sh("init", "--bare", "-b", "main", str(bare))
    seed = root / "seed" / owner / name
    seed.mkdir(parents=True)
    sh("init", "-b", "main", cwd=seed)
    sh("remote", "add", "origin", str(bare), cwd=seed)
    (seed / "a.txt").write_text("base\n")
    sh("add", "-A", cwd=seed)
    sh("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "base", cwd=seed)
    sh("push", "origin", "main", cwd=seed)
    for branch in branches:
        sh("checkout", "-b", branch, cwd=seed)
        (seed / "b.txt").write_text(f"{branch}\n")
        sh("add", "-A", cwd=seed)
        sh("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", branch, cwd=seed)
        sh("push", "origin", branch, cwd=seed)
        sh("checkout", "main", cwd=seed)
    return bare


def run_script(sub: str, stdin: dict | None = None, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), sub],
        input=json.dumps(stdin or {}),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", **env},
    )


# ---------------------------------------------------------------- config


def test_pod_policy_config(monkeypatch):
    for name in ("MODEL", "EFFORT", "PERMISSION_MODE", "BRANCH_PUSH_INTERVAL_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    ex = example({})
    cfg = Config.model_validate(
        {k: v for k, v in ex.items() if k in {"init", "claude", "sessions", "hooks"}}
    )
    s = cfg.sessions
    assert s.exit_when_dead is True
    assert s.probe_alive_token == "TATARA-ALIVE"
    assert s.ask_user_question == "deny"
    assert "continue" in s.restart_continue_prompt.lower()
    c = cfg.claude
    assert "*" not in c.passthrough_env
    assert {"TATARA_*", "CLI_OIDC_*", "OIDC_ISSUER", "RUN_ID", "GIT_TOKEN"} <= set(
        c.passthrough_env
    )
    assert "MISE_ALWAYS_KEEP_DOWNLOAD" in c.passthrough_env
    assert {"CALLBACK_HMAC_SECRET", "OPERATOR_PUSH_*"} <= set(c.passthrough_env_exclude)
    assert c.model == "" and c.effort == ""
    assert "EnterPlanMode" in c.permissions.deny
    assert len(c.permissions.deny) > 2
    assert "bootstrap" in cfg.init.setup_commands[0].run


def test_lifecycle_hooks_shape(monkeypatch):
    monkeypatch.delenv("BRANCH_PUSH_INTERVAL_SECONDS", raising=False)
    hooks = example({})["hooks"]
    (gate,) = hooks["onTurn"]
    assert gate["blocking"] is True and gate["run"].endswith("ttl-gate")
    ticks = {t["run"].rsplit(" ", 1)[1]: t for t in hooks["onTick"]}
    safety, metrics = ticks["safety-push"], ticks["metrics-push"]
    assert (float(safety["interval_seconds"]), safety["when"], safety["blocking"]) == (
        120,
        "busy",
        False,
    )
    assert (metrics["interval_seconds"], metrics["when"], metrics["blocking"]) == (
        30,
        "always",
        False,
    )
    custom = example({"BRANCH_PUSH_INTERVAL_SECONDS": "45"})["hooks"]["onTick"]
    assert [float(t["interval_seconds"]) for t in custom if t["run"].endswith("safety-push")] == [
        45.0
    ]


def test_passthrough_reaches_setup_but_not_hook_secrets():
    from claude_code_wrapper.session import passthrough_names

    ex = example({})["claude"]
    service = {
        "TATARA_REPOS": "1",
        "RUN_ID": "r",
        "GIT_TOKEN": "g",
        "CALLBACK_HMAC_SECRET": "s",
        "OPERATOR_PUSH_URL": "u",
        "OPERATOR_PUSH_TOKEN": "t",
        "HOME": "/h",
    }
    got = passthrough_names(service, ex["passthrough_env"], ex["passthrough_env_exclude"])
    assert sorted(got) == ["GIT_TOKEN", "RUN_ID", "TATARA_REPOS"]


# ---------------------------------------------------------------- ttl gate


def gate(state: Path, metadata: dict | None = None, **env: str) -> dict | None:
    state.mkdir(exist_ok=True)
    run = run_script(
        "ttl-gate",
        {"event": "onTurn", "data": {"metadata": metadata or {}}},
        CCW_STATE_DIR=str(state),
        **env,
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout) if run.stdout.strip() else None


def age_t0(state: Path, seconds: float) -> None:
    path = state / "ttl.json"
    doc = json.loads(path.read_text())
    doc["t0"] -= seconds
    path.write_text(json.dumps(doc))


def test_ttl_gate_records_t0_and_allows_until_ttl(tmp_path):
    state = tmp_path / "st"
    assert gate(state, AGENT_POD_TTL_SECONDS="100") in (None, {"decision": "allow"})
    assert json.loads((state / "ttl.json").read_text())["t0"] > 0
    t0 = json.loads((state / "ttl.json").read_text())["t0"]
    assert gate(state, AGENT_POD_TTL_SECONDS="100") in (None, {"decision": "allow"})
    assert json.loads((state / "ttl.json").read_text())["t0"] == t0  # recorded once


def test_ttl_gate_denies_after_ttl_and_admits_one_handoff(tmp_path):
    state = tmp_path / "st"
    env = {"AGENT_POD_TTL_SECONDS": "100"}
    gate(state, **env)
    age_t0(state, 200)
    out = gate(state, **env)
    assert out == {"decision": "deny", "reason": "pod ttl exceeded"}
    assert gate(state, {"handoff": False}, **env)["reason"] == "pod ttl exceeded"
    assert gate(state, {"handoff": True}, **env) in (None, {"decision": "allow"})
    assert gate(state, {"handoff": True}, **env)["reason"] == "pod ttl exceeded"
    assert gate(state, **env)["reason"] == "pod ttl exceeded"


def test_ttl_gate_handoff_before_ttl_does_not_consume_the_slot(tmp_path):
    state = tmp_path / "st"
    env = {"AGENT_POD_TTL_SECONDS": "100"}
    assert gate(state, {"handoff": True}, **env) in (None, {"decision": "allow"})
    age_t0(state, 200)
    assert gate(state, {"handoff": True}, **env) in (None, {"decision": "allow"})
    assert gate(state, {"handoff": True}, **env)["decision"] == "deny"


def test_ttl_gate_without_ttl_always_allows(tmp_path):
    state = tmp_path / "st"
    assert gate(state) in (None, {"decision": "allow"})
    age_t0(state, 10**6) if (state / "ttl.json").exists() else None
    assert gate(state) in (None, {"decision": "allow"})


async def test_ttl_gate_vetoes_turns_through_ccw(world: World, monkeypatch):
    monkeypatch.setenv("AGENT_POD_TTL_SECONDS", "1")
    ex = example({})
    scenario = {"turns": [{"match": "go", "steps": [{"text": "ok"}]}]}
    m = world.manager(world.cfg(scenario, hooks={"onTurn": ex["hooks"]["onTurn"]}))
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    router = m.router(sid)
    await router.submit_turn("go")
    await wait_turn(m, sid)
    await asyncio.sleep(1.3)
    with pytest.raises(TurnVetoed, match="pod ttl exceeded"):
        await router.submit_turn("go")
    handoff = await router.submit_turn("go", metadata={"handoff": True})
    await wait_turn(m, sid)
    await router.drain(5)
    await world.until(lambda: router.session.current_turn is None, what="turn finished")
    assert handoff.metadata == {"handoff": True}
    with pytest.raises(TurnVetoed, match="pod ttl exceeded"):
        await router.submit_turn("go", metadata={"handoff": True})


# ---------------------------------------------------------------- safety push


async def test_safety_push_pushes_task_branch_never_commits(world: World, monkeypatch, tmp_path):
    monkeypatch.setenv("TASK_BRANCH", "task/7")
    bare = make_remote(tmp_path, "acme", "app")
    ex = example({"BRANCH_PUSH_INTERVAL_SECONDS": "0.3"})
    safety = [t for t in ex["hooks"]["onTick"] if t["run"].endswith("safety-push")]
    m = world.manager(world.cfg(SLEEP, hooks={"onTick": safety}))
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    workdir = Path(m.get(sid)["workdir"])
    repo = workdir / "acme" / "app"
    repo.parent.mkdir(parents=True)
    sh("clone", str(bare), str(repo))
    sh("checkout", "-b", "task/7", cwd=repo)
    (repo / "c.txt").write_text("committed\n")
    sh("add", "-A", cwd=repo)
    sh("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "work", cwd=repo)
    (repo / "dirty.txt").write_text("uncommitted\n")
    head = sh("rev-parse", "HEAD", cwd=repo)

    def remote_has() -> bool:
        out = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "task/7"], cwd=bare, capture_output=True
        )
        return out.returncode == 0

    assert not remote_has()  # idle: the busy-only tick has not pushed
    await m.router(sid).submit_turn("go")
    await world.until(remote_has, what="task branch pushed")
    assert sh("rev-parse", "task/7", cwd=bare) == head
    assert sh("rev-parse", "HEAD", cwd=repo) == head  # no commit made
    assert "dirty.txt" in sh("status", "--porcelain", cwd=repo)


def test_safety_push_failure_in_one_repo_does_not_stop_others(tmp_path):
    good = make_remote(tmp_path, "acme", "good")
    work = tmp_path / "wd"
    for name, remote in (("a-bad", str(tmp_path / "missing.git")), ("good", str(good))):
        repo = work / "acme" / name
        repo.mkdir(parents=True)
        sh("init", "-b", "main", cwd=repo)
        sh("remote", "add", "origin", remote, cwd=repo)
        (repo / "x").write_text("x")
        sh("add", "-A", cwd=repo)
        sh("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "x", cwd=repo)
        sh("checkout", "-b", "task/9", cwd=repo)
    run = run_script("safety-push", {}, CCW_WORKDIR=str(work), TASK_BRANCH="task/9")
    assert run.returncode == 1 and "a-bad" in run.stderr  # failures are reported
    assert sh("rev-parse", "task/9", cwd=good)


# ---------------------------------------------------------------- metrics push


class Operator:
    def __init__(self) -> None:
        self.posts: list[tuple[dict[str, str], bytes]] = []
        self.gets = 0
        posts, op = self.posts, self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                op.gets += 1
                body = b"ccw_sessions 1\n"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                posts.append(({k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def operator():
    op = Operator()
    yield op
    op.server.shutdown()
    op.server.server_close()


def test_metrics_push_forwards_metrics_with_run_headers(operator):
    run = run_script(
        "metrics-push",
        {},
        CCW_API_URL=operator.url,
        OPERATOR_PUSH_URL=operator.url + "/push",
        RUN_ID="run-1",
        POD_NAME="pod-a",
    )
    assert run.returncode == 0, run.stderr
    ((headers, body),) = operator.posts
    assert body == b"ccw_sessions 1\n"
    assert headers["run_id"] == "run-1" and headers["pod_name"] == "pod-a"


def test_metrics_push_is_a_noop_without_operator_url(operator):
    run = run_script("metrics-push", {}, CCW_API_URL=operator.url)
    assert run.returncode == 0 and operator.gets == 0 and not operator.posts


def test_metrics_push_unreachable_operator_exits_nonzero(operator):
    run = run_script(
        "metrics-push", {}, CCW_API_URL=operator.url, OPERATOR_PUSH_URL="http://127.0.0.1:9/x"
    )
    assert run.returncode == 1 and run.stderr.strip()


async def test_metrics_push_ticks_through_ccw(world: World, operator, monkeypatch):
    ex = example({})
    tick = [dict(t, interval_seconds=0.3) for t in ex["hooks"]["onTick"] if "metrics" in t["run"]]
    monkeypatch.setenv("OPERATOR_PUSH_URL", operator.url + "/push")
    monkeypatch.setenv("RUN_ID", "run-2")
    monkeypatch.setenv("POD_NAME", "pod-b")
    m = world.manager(world.cfg(SLEEP, hooks={"onTick": tick}), api_url=operator.url)
    await m.create(CreateSessionRequest())  # idle session: `when: always` still fires
    await world.until(lambda: operator.posts, what="metrics pushed")
    assert operator.posts[0][0]["run_id"] == "run-2"


# ---------------------------------------------------------------- bootstrap


def url(bare: Path) -> str:
    """file:// so --depth 1 is honoured for a local remote."""
    return f"file://{bare}"


def bootstrap(workdir: Path, cfgdir: Path, **env: str) -> subprocess.CompletedProcess:
    workdir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "bootstrap"],
        cwd=workdir,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "CLAUDE_CONFIG_DIR": str(cfgdir),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            **env,
        },
    )


def test_bootstrap_single_repo_creates_task_branch_from_base(tmp_path):
    bare = make_remote(tmp_path, "acme", "app", branches=("dev",))
    wd, cfg = tmp_path / "wd", tmp_path / "cfg"
    run = bootstrap(wd, cfg, REPO_URL=url(bare), REPO_BRANCH="dev", TASK_BRANCH="task/1")
    assert run.returncode == 0, run.stderr
    repo = wd / "acme" / "app"
    assert sh("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "task/1"
    assert (repo / "b.txt").read_text().strip() == "dev"  # branched from the base branch
    assert sh("rev-parse", "--is-shallow-repository", cwd=repo) == "true"


def test_bootstrap_checks_out_existing_task_branch(tmp_path):
    bare = make_remote(tmp_path, "acme", "app", branches=("task/2",))
    wd, cfg = tmp_path / "wd", tmp_path / "cfg"
    run = bootstrap(wd, cfg, REPO_URL=str(bare), REPO_BRANCH="main", TASK_BRANCH="task/2")
    assert run.returncode == 0, run.stderr
    repo = wd / "acme" / "app"
    assert sh("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "task/2"
    assert (repo / "b.txt").read_text().strip() == "task/2"


def test_bootstrap_tatara_repos_and_full_clone(tmp_path):
    one = make_remote(tmp_path, "acme", "one")
    two = make_remote(tmp_path, "other", "two", branches=("dev",))
    wd, cfg = tmp_path / "wd", tmp_path / "cfg"
    repos = [{"url": url(one), "branch": "main"}, {"url": url(two), "branch": "dev"}]
    run = bootstrap(
        wd,
        cfg,
        TATARA_REPOS=json.dumps(repos),
        REPO_URL="ignored",
        TASK_BRANCH="task/3",
        TATARA_WORKSPACE_FULL_CLONE="true",
    )
    assert run.returncode == 0, run.stderr
    for path in ("acme/one", "other/two"):
        repo = wd / path
        assert sh("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "task/3"
        assert sh("rev-parse", "--is-shallow-repository", cwd=repo) == "false"
    assert (wd / "other/two/b.txt").exists()


def test_bootstrap_writes_directives_and_mcp_config(tmp_path):
    bare = make_remote(tmp_path, "acme", "app")
    wd, cfg = tmp_path / "wd", tmp_path / "cfg"
    servers = {"tatara": {"type": "http", "url": "http://tatara/mcp"}}
    run = bootstrap(
        wd,
        cfg,
        REPO_URL=str(bare),
        TASK_BRANCH="task/4",
        TATARA_EXTRA_MCP_SERVERS=json.dumps(servers),
    )
    assert run.returncode == 0, run.stderr
    text = (cfg / "CLAUDE.md").read_text().lower()
    assert "headless" in text and "push" in text
    assert json.loads((wd / ".mcp.json").read_text()) == {"mcpServers": servers}
    # rerun is idempotent: directive blocks are replaced, not appended
    again = bootstrap(wd, cfg, REPO_URL=str(bare), TASK_BRANCH="task/4")
    assert again.returncode == 0, again.stderr
    assert (cfg / "CLAUDE.md").read_text().lower().count("headless") == text.count("headless")


def test_bootstrap_without_mcp_servers_writes_no_mcp_json(tmp_path):
    bare = make_remote(tmp_path, "acme", "app")
    wd = tmp_path / "wd"
    run = bootstrap(wd, tmp_path / "cfg", REPO_URL=str(bare), TASK_BRANCH="task/5")
    assert run.returncode == 0, run.stderr
    assert not (wd / ".mcp.json").exists()


def test_bootstrap_rejects_bad_input(tmp_path):
    wd, cfg = tmp_path / "wd", tmp_path / "cfg"
    assert bootstrap(wd, cfg, TATARA_REPOS="not json").returncode != 0
    missing = bootstrap(wd, cfg, REPO_URL=str(tmp_path / "nope.git"), TASK_BRANCH="t")
    assert missing.returncode != 0 and "clone" in missing.stderr


async def test_bootstrap_runs_as_setup_command_with_passthrough_env(
    world: World, monkeypatch, tmp_path
):
    bare = make_remote(tmp_path, "acme", "app")
    for k, v in {
        "REPO_URL": str(bare),
        "REPO_BRANCH": "main",
        "TASK_BRANCH": "task/6",
        "TATARA_EXTRA_MCP_SERVERS": json.dumps({"x": {"url": "http://x"}}),
    }.items():
        monkeypatch.setenv(k, v)
    ex = example({})
    scenario = {"turns": [{"match": "go", "steps": [{"text": "ok"}]}]}
    cfg = world.cfg(scenario, init={"setup_commands": ex["init"]["setup_commands"]})
    cfg = cfg.model_copy(
        update={
            "claude": cfg.claude.model_copy(
                update={"passthrough_env": ex["claude"]["passthrough_env"]}
            )
        }
    )
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    workdir = Path(m.get(sid)["workdir"])
    repo = workdir / "acme" / "app"
    assert sh("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "task/6"
    assert json.loads((workdir / ".mcp.json").read_text())["mcpServers"] == {
        "x": {"url": "http://x"}
    }
    assert "headless" in (world.session_dir(sid) / "config" / "CLAUDE.md").read_text().lower()


def test_script_usage_lists_lifecycle_subcommands():
    run = run_script("nope")
    assert run.returncode == 64
    for sub in ("bootstrap", "ttl-gate", "safety-push", "metrics-push"):
        assert sub in run.stderr
