"""magic-trainer example: seat-pair completion, review, narration and exit as ccw hooks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from conftest import World, wait_turn

from claude_code_wrapper.config import Config
from claude_code_wrapper.interpolation import interpolate
from claude_code_wrapper.manager import CreateSessionRequest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "src/claude_code_wrapper/data/examples/magic-trainer.config.yaml"
SCRIPT = ROOT / "src/claude_code_wrapper/data/examples/magic-trainer/mtg_hooks.py"
SECTIONS = {"init", "claude", "sessions", "hooks"}


def example(env: dict[str, str] | None = None) -> dict[str, Any]:
    base = {"MTG_PYTHON": sys.executable, "MTG_HOOKS": str(SCRIPT)}
    return interpolate(yaml.safe_load(EXAMPLE.read_text()), {**base, **(env or {})})


def run_script(sub: str, stdin: dict | None = None, *args: str, **env: str):
    return subprocess.run(
        [sys.executable, str(SCRIPT), sub, *args],
        input=json.dumps(stdin or {}),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", **env},
    )


def hooks_of(*events: str) -> dict[str, Any]:
    ex = example()["hooks"]
    return {e: ex[e] for e in events}


# ---------------------------------------------------------------- config


def test_magic_config_loads_and_ships():
    cfg = Config.model_validate({k: v for k, v in example().items() if k in SECTIONS})
    assert cfg.sessions.defer_stop_on_background_tasks is True
    assert EXAMPLE.is_file() and SCRIPT.is_file()
    assert EXAMPLE.is_relative_to(ROOT / "src/claude_code_wrapper/data")
    assert SCRIPT.is_relative_to(ROOT / "src/claude_code_wrapper/data")
    link = ROOT / "examples/magic-trainer.config.yaml"
    assert link.is_symlink() and link.resolve() == EXAMPLE.resolve()


def test_example_config_options():
    cfg = Config.model_validate({k: v for k, v in example().items() if k in SECTIONS})
    s = cfg.sessions
    assert s.defer_stop_on_background_tasks is True
    assert s.exit_when_last_destroyed is True
    assert s.max_restarts == 0
    assert s.ask_user_question == "deny"
    assert cfg.claude.settings["teammateMode"] == "in-process"
    assert cfg.claude.env.claude_code_experimental_agent_teams is None
    assert cfg.claude.env.mcp_timeout == 60000
    assert cfg.claude.env.mcp_tool_timeout == 600000
    assert cfg.claude.env.max_mcp_output_tokens == 50000
    runs = [c.run for c in cfg.init.setup_commands]
    assert "credential.helper" in runs[0] and "/etc/mtg/git-token" in runs[0]
    assert "safe.directory /guides" in runs[1]
    assert runs[2] == "true"
    (review,) = cfg.hooks.onDestroy
    assert review.blocking is True and review.timeout_seconds == 4800
    assert review.run.endswith("review")


def test_example_env_overrides():
    cfg = Config.model_validate(
        {
            k: v
            for k, v in example(
                {
                    "AGENT_TEAMS": "1",
                    "REVIEW_TIMEOUT_SECONDS": "60",
                    "MTG_AGENTS_RENDER": "render-agents --all",
                    "MTG_GIT_TOKEN_FILE": "/run/tok",
                }
            ).items()
            if k in SECTIONS
        }
    )
    assert cfg.claude.env.claude_code_experimental_agent_teams is True
    assert cfg.hooks.onDestroy[0].timeout_seconds == 60
    assert cfg.init.setup_commands[2].run == "render-agents --all"
    assert "/run/tok" in cfg.init.setup_commands[0].run


def test_hook_events_and_blocking():
    hooks = example()["hooks"]
    (stop,) = hooks["onStop"]
    assert stop["blocking"] is True and stop["run"].endswith("pending-tasks")
    for event in ("onAgentToolCall", "onAgentResponse"):
        (narrate,) = hooks[event]
        assert narrate["blocking"] is False and narrate["run"].endswith("narrate")


def test_no_mtg_names_in_core():
    for path in (ROOT / "src/claude_code_wrapper").glob("*.py"):
        text = path.read_text().lower()
        assert "magic-trainer" not in text and "mtg_hooks" not in text, path.name


# ---------------------------------------------------------------- pending-tasks


def write_transcript(path: Path, lines: list[dict]) -> str:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return str(path)


def prompt(turn: str = "t1") -> dict:
    text = f"go\n\n<!-- ccw turn: {turn} -->"
    return {"type": "user", "message": {"role": "user", "content": text}}


def use(tid: str, tool: str, **tinput: Any) -> dict:
    block = {"type": "tool_use", "id": tid, "name": tool, "input": tinput}
    return {"type": "assistant", "message": {"role": "assistant", "content": [block]}}


def result(tid: str, text: str) -> dict:
    block = {"type": "tool_result", "tool_use_id": tid, "content": text}
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def notice(task: str, status: str = "completed") -> dict:
    text = (
        f"<task-notification><task-id>{task}</task-id><status>{status}</status></task-notification>"
    )
    return {"type": "user", "message": {"role": "user", "content": text}}


def gate(tmp_path: Path, lines: list[dict] | None, path: str | None = None) -> dict:
    tpath = path if lines is None else write_transcript(tmp_path / "t.jsonl", lines)
    run = run_script("pending-tasks", {"event": "onStop", "data": {"transcript_path": tpath}})
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_pending_tasks_fallback_defers_from_transcript(tmp_path):
    out = gate(
        tmp_path,
        [
            prompt(),
            use("a1", "Agent", run_in_background=True, description="dig"),
            result("a1", "started agentId: ag-1"),
            use("b1", "Bash", run_in_background=True, command="sleep 99"),
            result("b1", "Command running in background with ID: sh-2"),
            use("m1", "Task", team_name="seat", name="blue"),
            use("fg", "Bash", command="ls"),
        ],
    )
    assert out["decision"] == "defer"
    pending = out["annotations"]["pending_tasks"]
    assert [(p["kind"], p["id"]) for p in pending] == [
        ("agent", "ag-1"),
        ("bash", "sh-2"),
        ("teammate", "blue"),
    ]


def test_pending_tasks_subtracts_completions_and_stops(tmp_path):
    out = gate(
        tmp_path,
        [
            prompt(),
            use("a1", "Agent", run_in_background=True),
            result("a1", "agentId: ag-1"),
            use("b1", "Bash", run_in_background=True),
            result("b1", "ID: sh-2"),
            use("m1", "Task", team_name="seat", name="blue"),
            use("s1", "TaskStop", task_id="sh-2"),
            notice("ag-1"),
            notice("blue", "completed"),
        ],
    )
    assert out == {"decision": "allow", "annotations": {"pending_tasks": []}}


def test_pending_tasks_running_notification_is_not_completion(tmp_path):
    out = gate(
        tmp_path,
        [
            prompt(),
            use("a1", "Agent", run_in_background=True),
            result("a1", "agentId: ag-1"),
            notice("ag-1", "running"),
        ],
    )
    assert out["decision"] == "defer"


def test_pending_tasks_only_counts_since_the_last_marker(tmp_path):
    out = gate(
        tmp_path,
        [
            prompt("t1"),
            use("a1", "Agent", run_in_background=True),
            result("a1", "agentId: old"),
            prompt("t2"),
            use("fg", "Bash", command="ls"),
        ],
    )
    assert out["decision"] == "allow" and out["annotations"] == {"pending_tasks": []}


def test_pending_tasks_without_transcript_allows(tmp_path):
    for path in (None, str(tmp_path / "missing.jsonl")):
        assert gate(tmp_path, None, path)["decision"] == "allow"


async def test_pair_turn_completes_after_background_tasks(world: World):
    done = (
        "<task-notification><task-id>ag-1</task-id><status>completed</status></task-notification>"
    )
    scenario = {
        "turns": [
            {
                "match": "go",
                "steps": [
                    {
                        "tool": "Agent",
                        "input": {"run_in_background": True, "description": "seat"},
                        "result": "agentId: ag-1",
                    },
                    {"text": "waiting"},
                    {"background": {"after": 0.6, "text": done}},
                ],
            }
        ]
    }
    cfg = world.cfg(
        scenario,
        sessions={"defer_stop_on_background_tasks": True},
        hooks=hooks_of("onStop"),
    )
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    router = m.router(sid)
    record = await router.submit_turn("go")
    await world.until(
        lambda: any(e["type"] == "turn_deferred" for e in world.events(sid)), what="deferred"
    )
    assert m.open_turns(sid).get(record.turn_id).state == "running"
    done_turn = await wait_turn(m, sid)
    assert done_turn.turn_id == record.turn_id
    assert done_turn.annotations["pending_tasks"] == []


# ---------------------------------------------------------------- review


async def test_review_output_in_delete_response(world: World, monkeypatch):
    scenario = {"turns": [{"match": "audit the diff", "steps": [{"text": "verdict: ship it"}]}]}
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", str(world.scenario_file(scenario)))
    monkeypatch.setenv("CLAUDE_BIN", str(world.bin))
    m = world.manager(world.cfg(hooks=hooks_of("onDestroy")))
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    out = await m.destroy(
        sid, hook_input={"prompt": "audit the diff", "model": "opus", "effort": "high"}
    )
    (entry,) = out["hook_outputs"]
    assert entry["ok"] is True
    assert entry["output"] == {"text": "verdict: ship it\n"}


async def test_review_skipped_without_input(world: World):
    m = world.manager(world.cfg(hooks=hooks_of("onDestroy")))
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    out = await m.destroy(sid)
    (entry,) = out["hook_outputs"]
    assert entry["ok"] is True and entry["output"] is None


def test_review_command_line(tmp_path):
    log = tmp_path / "argv.json"
    fake = tmp_path / "fake-claude"
    body = (
        "import json, os, sys\n"
        "json.dump({'argv': sys.argv[1:], 'stdin': sys.stdin.read(),\n"
        "           'cfg': os.environ.get('CLAUDE_CONFIG_DIR'), 'cwd': os.getcwd()},\n"
        "          open(os.environ['ARGV_LOG'], 'w'))\n"
        "print('ok')\n"
    )
    fake.write_text(f"#!{sys.executable}\n{body}")
    fake.chmod(0o755)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    run = run_script(
        "review",
        {"event": "onDestroy", "data": {"input": {"prompt": "P", "model": "m1", "effort": "low"}}},
        CLAUDE_BIN=str(fake),
        ARGV_LOG=str(log),
        CCW_CLAUDE_CONFIG_DIR="/cfg/dir",
        CCW_WORKDIR=str(workdir),
    )
    assert run.returncode == 0, run.stderr
    seen = json.loads(log.read_text())
    assert seen["argv"] == ["-p", "--setting-sources", "", "--strict-mcp-config", "--model", "m1"]
    assert seen["stdin"] == "P" and seen["cfg"] == "/cfg/dir"
    assert Path(seen["cwd"]).resolve() == workdir.resolve()
    assert json.loads(run.stdout) == {"output": {"text": "ok\n"}}


# ---------------------------------------------------------------- narrate


def test_narration_lines_written(tmp_path):
    log = tmp_path / "n.log"
    cases = [
        (
            {"event": "onAgentToolCall", "data": {"tool_name": "Bash", "agent_type": None}},
            "step tool=Bash agent=main",
        ),
        (
            {"event": "onAgentToolCall", "data": {"tool_name": "Read", "agent_type": "Explore"}},
            "step tool=Read agent=Explore",
        ),
        ({"event": "onAgentResponse", "data": {"text": "x" * 200}}, "say " + "x" * 80),
    ]
    for doc, _ in cases:
        assert run_script("narrate", doc, NARRATION_LOG=str(log)).returncode == 0
    assert log.read_text().splitlines() == [line for _, line in cases]


async def test_narration_from_a_session(world: World, monkeypatch, tmp_path):
    log = tmp_path / "narration.log"
    monkeypatch.setenv("NARRATION_LOG", str(log))
    scenario = {
        "turns": [
            {
                "match": "go",
                "steps": [
                    {"tool": "Bash", "input": {"command": "ls"}, "result": "a"},
                    {"text": "all done here"},
                ],
            }
        ]
    }
    cfg = world.cfg(scenario, hooks=hooks_of("onAgentToolCall", "onAgentResponse"))
    m = world.manager(cfg)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    await m.router(sid).submit_turn("go")
    await wait_turn(m, sid)
    await world.until(
        lambda: log.exists() and "say all done here" in log.read_text(), what="narration"
    )
    lines = log.read_text().splitlines()
    assert "step tool=Bash agent=main" in lines


# ---------------------------------------------------------------- exit policy, credential helper


async def test_last_destroy_exits_and_no_restart(world: World):
    codes: list[int] = []
    ex = example()
    cfg = world.cfg(
        sessions={k: ex["sessions"][k] for k in ("exit_when_last_destroyed", "max_restarts")}
    )
    m = world.manager(cfg, on_exit_request=codes.append)
    sid = (await m.create(CreateSessionRequest()))["session_id"]
    assert codes == []
    await m.destroy(sid)
    assert codes == [0]


def test_git_credential_helper(tmp_path):
    token = tmp_path / "tok"
    token.write_text("s3cret\n")
    run = run_script("git-credential", None, str(token), "get")
    assert run.stdout == "username=x-access-token\npassword=s3cret\n"
    assert run_script("git-credential", None, str(token), "store").stdout == ""
    assert run_script("git-credential", None, str(tmp_path / "none"), "get").returncode == 1
