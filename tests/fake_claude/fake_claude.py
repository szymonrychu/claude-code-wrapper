"""Scripted fake `claude` for PTY, session and e2e tests (stdlib only).

CLI
    fake_claude [--model M] [--effort E] [--session-id UUID] [-r|--resume ID]
                [--append-system-prompt T] [other flags accepted and recorded]
    fake_claude -p|--print [prompt] [--output-format json] [flags]
                                                print mode, see below
    fake_claude --version                       prints "0.0.0 (Fake Claude)"
    fake_claude plugin marketplace add <source> [--scope s] [--json]
    fake_claude plugin marketplace list --json
    fake_claude plugin install <name@mkt> [-s scope] [-y] [--json]
    fake_claude plugin list --json
  Plugin state lives in $CLAUDE_CONFIG_DIR/plugins/fake-state.json.

Environment
    CLAUDE_CONFIG_DIR            config root (settings.json hooks, projects/ transcripts)
    CLAUDE_CODE_PROJECT_DIR_NAME transcript project dir name (default: cwd with every
                                 non-alphanumeric char replaced by '-')
    FAKE_CLAUDE_SCENARIO         path to a scenario JSON file
    FAKE_CLAUDE_LOG              when set, one JSON line per invocation:
                                 {argv, cwd, env: {CLAUDE_CONFIG_DIR,
                                 CLAUDE_CODE_PROJECT_DIR_NAME, CCW_SESSION_ID,
                                 CCW_INTERNAL_URL}}
    FAKE_CLAUDE_PLUGIN_FAIL      `plugin install` of exactly this id exits 1
    CLAUDE_CODE_STOP_HOOK_BLOCK_CAP  max consecutive Stop blocks per turn (default 8)

Interactive mode (no subcommand) needs a TTY on stdin, else exits 2. Boot prints a
colored banner, then (boot.bypass_dialog, default true) a "Bypass Permissions mode"
dialog needing Down (ESC [ B) then CR; CR without Down exits 1. Input arriving before
boot.min_seconds (default 0.3) exits 1. Then the idle footer "? for shortcuts".
Input: bracketed paste ESC[200~ ... ESC[201~; a CR in a later read than the paste end
submits; a CR in the same read as the paste end is a newline. A lone ESC during a turn
interrupts it. Byte 0x03 at idle exits 0.

Scenario schema
    {"boot": {"bypass_dialog": true, "min_seconds": 0.3},
     "turns": [{"match": "<regex>", "steps": [...]}]}
  First turn whose regex matches (re.search) the prompt runs; otherwise the default
  steps [{"text": "echo: <prompt>"}]. Steps:
    {"text": s}  {"thinking": s}
    {"tool": name, "input": {...}, "result": s, "fail": false,
     "agent": {"id": A, "type": T}}   with "agent", PreToolUse and PostToolUse(Failure)
                         payloads carry agent_id and agent_type, and the tool_use and
                         tool_result lines go to subagents/agent-<A>.jsonl (isSidechain true)
    {"subagent": {"id", "type", "text"}}  -> <project dir>/<session id>/subagents/
                                             agent-<id>.jsonl, isSidechain true
    {"sleep": seconds}   ESC during it writes "[Request interrupted by user]" and
                         returns to idle without a Stop hook
    {"print": s}         raw PTY output, then idle without Stop
    {"exit": code}
    {"exit_once": code}  exit with code only the first time any fake process reaches this
                         step (flag file under $CLAUDE_CONFIG_DIR); afterwards a no-op
    {"background": {"after": seconds, "text": s, "background_tasks": [...]}}  after the
                         turn, wait, append an assistant line and run Stop again; that Stop
                         carries background_tasks (default [])
  After the last step: Stop hooks run with the last assistant text, then the footer.
  Turn-level keys besides "match" and "steps":
    "stop": {"background_tasks": [...], "session_crons": [...]}  the turn's first Stop
                         carries these lists verbatim (entries {"id","type","status",
                         "description"}); every Stop payload has both keys, default []
    "on_block": [steps]  run after a Stop block (default [{"text": "continued: <reason>"}])
  Stop block: a Stop hook that prints {"decision": "block", "reason": R} on stdout (exit 0)
  or exits 2 (reason = stderr) makes the fake write R as a user line, run the turn's
  on_block steps and fire Stop again with stop_hook_active true. At most 8 consecutive
  blocks (CLAUDE_CODE_STOP_HOOK_BLOCK_CAP overrides); past the cap the turn ends as allowed.

Print mode
  `-p`/`--print` takes the prompt from the argument or stdin, needs no TTY and runs no hooks.
  It picks the first turn whose regex matches, prints the concatenated `text` steps (default
  "echo: <prompt>") and exits 0; with `--output-format json` it prints {"result": <text>}.
  Other flags are accepted and logged via FAKE_CLAUDE_LOG like every invocation.

Hooks come from $CLAUDE_CONFIG_DIR/settings.json hooks.<Event>; commands run via
`sh -c` with the JSON payload on stdin and CLAUDE_PROJECT_DIR=<cwd>. PreToolUse deny
is stdout hookSpecificOutput.permissionDecision == "deny" or exit code 2; additionalContext
from PreToolUse/PostToolUse is written to the transcript as a system line.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import select
import subprocess
import sys
import termios
import time
import tty
import uuid
import zlib

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
DOWN = b"\x1b[B"
FOOTER = "? for shortcuts"
INTERRUPT_TEXT = "[Request interrupted by user]"
VERSION = "0.0.0 (Fake Claude)"
DEFAULT_BLOCK_CAP = 8
VALUED_FLAGS = {
    "--model",
    "--effort",
    "--append-system-prompt",
    "--permission-mode",
    "--settings",
    "--add-dir",
    "--allowedTools",
    "--disallowedTools",
    "--setting-sources",
    "--output-format",
    "--max-turns",
    "--mcp-config",
}


def config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def project_dir_name(cwd: str) -> str:
    return os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME") or re.sub(r"[^A-Za-z0-9]", "-", cwd)


# ---------------------------------------------------------------- plugins


def _state_path() -> str:
    return os.path.join(config_dir(), "plugins", "fake-state.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"marketplaces": [], "plugins": []}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
    with open(_state_path(), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _opt(args: list[str], names: tuple[str, ...]) -> str | None:
    for i, a in enumerate(args):
        if a in names and i + 1 < len(args):
            return args[i + 1]
    return None


def _positionals(args: list[str]) -> list[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in ("--scope", "-s"):
            skip = True
        elif not a.startswith("-"):
            out.append(a)
    return out


def plugin_main(args: list[str]) -> int:
    state = _load_state()
    as_json = "--json" in args
    pos = _positionals(args)
    if pos[:2] == ["marketplace", "add"] and len(pos) >= 3:
        source = pos[2]
        local = source.startswith(("/", ".", "~"))
        if local:
            path = os.path.abspath(os.path.expanduser(source))
            manifest = os.path.join(path, ".claude-plugin", "marketplace.json")
            if not os.path.isfile(manifest):
                print(f"Error: marketplace manifest not found at {manifest}", file=sys.stderr)
                return 1
            with open(manifest, encoding="utf-8") as f:
                name = json.load(f).get("name") or os.path.basename(path)
        else:
            path = source
            name = re.sub(r"\.git$", "", source.rstrip("/").rsplit("/", 1)[-1])
        state["marketplaces"] = [m for m in state["marketplaces"] if m["name"] != name]
        state["marketplaces"].append({"name": name, "source": source, "path": path})
        _save_state(state)
        print(json.dumps({"name": name}) if as_json else f"Added marketplace {name}")
        return 0
    if pos[:2] == ["marketplace", "list"]:
        if as_json:
            print(json.dumps(state["marketplaces"]))
        else:
            for m in state["marketplaces"]:
                print(m["name"])
        return 0
    if pos[:1] == ["install"] and len(pos) >= 2:
        pid = pos[1]
        _, _, mkt = pid.partition("@")
        if pid == os.environ.get("FAKE_CLAUDE_PLUGIN_FAIL"):
            print(f"Error: failed to install {pid}", file=sys.stderr)
            return 1
        if not mkt or mkt not in {m["name"] for m in state["marketplaces"]}:
            print(f"Error: marketplace '{mkt}' not found", file=sys.stderr)
            return 1
        scope = _opt(args, ("--scope", "-s")) or "user"
        state["plugins"] = [p for p in state["plugins"] if p["id"] != pid]
        state["plugins"].append({"id": pid, "version": "1.0.0", "scope": scope, "enabled": True})
        _save_state(state)
        print(json.dumps({"id": pid}) if as_json else f"Installed {pid}")
        return 0
    if pos[:1] == ["list"]:
        if as_json:
            print(json.dumps(state["plugins"]))
        else:
            for p in state["plugins"]:
                print(p["id"])
        return 0
    print("Error: unknown plugin command", file=sys.stderr)
    return 1


# ---------------------------------------------------------------- interactive


class Exit(Exception):  # noqa: N818
    def __init__(self, code: int) -> None:
        self.code = code


class Fake:
    def __init__(self, argv: list[str]) -> None:
        self.cwd = os.getcwd()
        self.fd = sys.stdin.fileno()
        self.session_id, self.resume = self._parse(argv)
        self.scenario = self._load_scenario()
        proj = os.path.join(config_dir(), "projects", project_dir_name(self.cwd))
        self.proj_dir = proj
        self.transcript = os.path.join(proj, f"{self.session_id}.jsonl")
        self.settings = self._load_settings()
        self.parent = None
        self.last_text = ""
        self.background: list[tuple[float, str, list]] = []
        self.pending_input = b""

    @staticmethod
    def _parse(argv: list[str]) -> tuple[str, str | None]:
        sid, resume = None, None
        i = 0
        while i < len(argv):
            a = argv[i]
            if a == "--session-id" and i + 1 < len(argv):
                sid = argv[i + 1]
                i += 1
            elif a in ("-r", "--resume") and i + 1 < len(argv):
                resume = argv[i + 1]
                i += 1
            elif a in VALUED_FLAGS:
                i += 1
            i += 1
        return resume or sid or str(uuid.uuid4()), resume

    @staticmethod
    def _load_scenario() -> dict:
        path = os.environ.get("FAKE_CLAUDE_SCENARIO")
        if not path:
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _load_settings() -> dict:
        try:
            with open(os.path.join(config_dir(), "settings.json"), encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}

    # ---- terminal io
    def out(self, text: str) -> None:
        os.write(1, text.replace("\r\n", "\n").replace("\n", "\r\n").encode())

    def read(self, timeout: float | None) -> bytes | None:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        data = os.read(self.fd, 65536)
        if not data:
            raise Exit(0)
        return data

    def footer(self) -> None:
        self.out(f"\n  {FOOTER}\n")

    # ---- transcript
    def write_line(self, obj: dict, path: str | None = None, sidechain: bool = False) -> None:
        path = path or self.transcript
        os.makedirs(os.path.dirname(path), exist_ok=True)
        base = {
            "uuid": str(uuid.uuid4()),
            "sessionId": self.session_id,
            "isSidechain": sidechain,
            "cwd": self.cwd,
            "timestamp": now(),
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({**obj, **base}) + "\n")

    def user_line(self, content, path=None, sidechain=False) -> None:
        self.write_line(
            {"type": "user", "message": {"role": "user", "content": content}}, path, sidechain
        )

    def assistant_line(self, block: dict, prompt_tokens=0, path=None, sidechain=False) -> None:
        out_tokens = len(str(block.get("text", block.get("thinking", ""))).split())
        self.write_line(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_" + uuid.uuid4().hex[:24],
                    "model": "fake-claude",
                    "role": "assistant",
                    "content": [block],
                    "usage": {
                        "input_tokens": prompt_tokens,
                        "output_tokens": out_tokens,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
            },
            path,
            sidechain,
        )

    # ---- hooks
    def run_hooks(self, event: str, extra: dict, tool: str | None = None) -> list[dict]:
        payload = {
            "session_id": self.session_id,
            "transcript_path": self.transcript,
            "cwd": self.cwd,
            "hook_event_name": event,
            "permission_mode": "default",
            **extra,
        }
        results = []
        for group in self.settings.get("hooks", {}).get(event, []):
            matcher = group.get("matcher") or ""
            if tool is not None and matcher not in ("", "*") and not re.fullmatch(matcher, tool):
                continue
            for h in group.get("hooks", []):
                if h.get("type", "command") != "command":
                    continue
                env = {**os.environ, "CLAUDE_PROJECT_DIR": self.cwd}
                try:
                    p = subprocess.run(
                        ["sh", "-c", h["command"]],
                        input=json.dumps(payload).encode(),
                        capture_output=True,
                        env=env,
                        cwd=self.cwd,
                        timeout=h.get("timeout", 60),
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    continue
                res = {
                    "code": p.returncode,
                    "stderr": p.stderr.decode(errors="replace").strip(),
                    "json": {},
                }
                try:
                    parsed = json.loads(p.stdout.decode(errors="replace"))
                    if isinstance(parsed, dict):
                        res["json"] = parsed
                except ValueError:
                    pass
                results.append(res)
        return results

    def context_from(self, results: list[dict]) -> None:
        for r in results:
            ctx = (r["json"].get("hookSpecificOutput") or {}).get("additionalContext")
            if ctx:
                self.write_line(
                    {"type": "system", "subtype": "hook_additional_context", "content": ctx}
                )

    # ---- boot
    def boot(self) -> None:
        cfg = self.scenario.get("boot", {})
        self.out("\x1b[38;5;208m* Fake Claude\x1b[0m \x1b[2mv0.0.0\x1b[0m\n")
        deadline = time.monotonic() + float(cfg.get("min_seconds", 0.3))
        while (left := deadline - time.monotonic()) > 0:
            if self.read(left) is not None:
                raise Exit(1)
        if cfg.get("bypass_dialog", True):
            self.out(
                "\n\x1b[1;31mWARNING: Bypass Permissions mode\x1b[0m\n"
                "  1. No, exit\n  2. Yes, I accept\n"
            )
            buf = b""
            while True:
                buf += self.read(None) or b""
                idx = buf.find(DOWN)
                if idx >= 0 and b"\r" in buf[idx:]:
                    break
                if idx < 0 and b"\r" in buf:
                    raise Exit(1)
        self.footer()

    # ---- main loop
    def run(self) -> int:
        os.makedirs(self.proj_dir, exist_ok=True)
        self.boot()
        in_paste = False
        paste = b""
        pasted = False
        typed = b""
        while True:
            timeout = None
            if self.background:
                timeout = max(0.0, min(b[0] for b in self.background) - time.monotonic())
            data = self.read(timeout)
            if data is None:
                self.fire_background()
                continue
            if data == b"\x03":
                return 0
            ended_here = False
            while data:
                if in_paste:
                    idx = data.find(PASTE_END)
                    if idx < 0:
                        paste += data
                        break
                    paste += data[:idx]
                    data = data[idx + len(PASTE_END) :]
                    in_paste, pasted, ended_here = False, True, True
                elif data.startswith(PASTE_START):
                    data = data[len(PASTE_START) :]
                    in_paste, paste, pasted = True, b"", False
                else:
                    ch, data = data[:1], data[1:]
                    if ch in (b"\r", b"\n"):
                        if pasted and ended_here:
                            paste += b"\n"
                        elif pasted or typed:
                            text = (paste if pasted else typed).decode(errors="replace")
                            in_paste, paste, pasted, typed = False, b"", False, b""
                            self.run_turn(text.rstrip("\n"))
                            data = b""
                    elif not pasted:
                        typed += ch

    def fire_background(self) -> None:
        due = [b for b in self.background if b[0] <= time.monotonic()]
        for item in due:
            self.background.remove(item)
            self.assistant_line({"type": "text", "text": item[1]})
            self.last_text = item[1]
            self.stop_cycle([], background_tasks=item[2])
            self.footer()

    def interrupted(self, timeout: float = 0.0) -> bool:
        data = self.read(timeout)
        return data == b"\x1b"

    def do_interrupt(self) -> None:
        self.user_line([{"type": "text", "text": INTERRUPT_TEXT}])
        self.out(f"\n{INTERRUPT_TEXT}\n")
        self.footer()

    @staticmethod
    def matching_turn_of(scenario: dict, prompt: str) -> dict:
        for t in scenario.get("turns", []):
            if re.search(t.get("match", ".*"), prompt):
                return t
        return {"steps": [{"text": f"echo: {prompt}"}]}

    def matching_turn(self, prompt: str) -> dict:
        return self.matching_turn_of(self.scenario, prompt)

    def run_turn(self, prompt: str) -> None:
        turn = self.matching_turn(prompt)
        self.user_line(prompt)
        self.run_hooks("UserPromptSubmit", {"prompt": prompt})
        self.out(f"\n> {prompt}\n")
        tokens = len(prompt.split())
        self.last_text = ""
        bg: list[dict] = []
        if not self.run_steps(turn.get("steps", []), tokens, bg, turn):
            return
        self.stop_cycle(turn.get("on_block"), turn.get("stop") or {})
        self.footer()
        for b in bg:
            self.background.append(
                (
                    time.monotonic() + float(b.get("after", 0)),
                    b["text"],
                    b.get("background_tasks", []),
                )
            )

    def run_steps(self, steps: list[dict], tokens: int, bg: list[dict], turn: dict) -> bool:
        """Run steps; False when the turn ended without a Stop (interrupt or raw print)."""
        for index, step in enumerate(steps):
            if self.interrupted():
                self.do_interrupt()
                return False
            if "text" in step:
                self.assistant_line({"type": "text", "text": step["text"]}, tokens)
                self.last_text = step["text"]
                self.out(f"\n{step['text']}\n")
            elif "thinking" in step:
                self.assistant_line(
                    {"type": "thinking", "thinking": step["thinking"], "signature": "fake"}, tokens
                )
            elif "tool" in step:
                self.tool_step(step, tokens)
            elif "subagent" in step:
                self.subagent_step(step["subagent"])
            elif "sleep" in step:
                if self.interrupted(float(step["sleep"])):
                    self.do_interrupt()
                    return False
            elif "print" in step:
                self.out(step["print"])
                return False
            elif "exit" in step:
                raise Exit(int(step["exit"]))
            elif "exit_once" in step:
                self.exit_once(int(step["exit_once"]), f"{turn.get('match', '')}#{index}")
            elif "background" in step:
                bg.append(step["background"])
        return True

    def exit_once(self, code: int, key: str) -> None:
        flag = os.path.join(config_dir(), f"fake-exit-once-{code}-{zlib.crc32(key.encode()):08x}")
        try:
            fd = os.open(flag, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return
        os.close(fd)
        raise Exit(code)

    @staticmethod
    def block_reason(results: list[dict]) -> str | None:
        for r in results:
            if r["json"].get("decision") == "block":
                return str(r["json"].get("reason") or r["stderr"] or "blocked by hook")
            if r["code"] == 2:
                return r["stderr"] or "blocked by hook"
        return None

    def stop_cycle(
        self,
        on_block: list[dict] | None,
        stop: dict | None = None,
        background_tasks: list | None = None,
    ) -> None:
        """Fire Stop; a block continues the turn and re-fires Stop (cap per claude 2.1.143)."""
        cap = int(os.environ.get("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP") or DEFAULT_BLOCK_CAP)
        stop = stop or {}
        tasks = stop.get("background_tasks", background_tasks or [])
        crons = stop.get("session_crons", [])
        blocks = 0
        while True:
            results = self.run_hooks(
                "Stop",
                {
                    "stop_hook_active": blocks > 0,
                    "last_assistant_message": self.last_text,
                    "background_tasks": tasks,
                    "session_crons": crons,
                },
            )
            reason = self.block_reason(results)
            if reason is None or blocks >= cap:
                return
            blocks += 1
            tasks, crons = [], []
            self.user_line(reason)
            steps = on_block if on_block is not None else [{"text": f"continued: {reason}"}]
            if not self.run_steps(steps, len(reason.split()), [], {}):
                return

    def tool_step(self, step: dict, tokens: int) -> None:
        name, tinput = step["tool"], step.get("input", {})
        tid = "toolu_" + uuid.uuid4().hex[:24]
        agent = step.get("agent")
        who: dict = {}
        path, side = None, False
        if agent:
            who = {"agent_id": agent["id"], "agent_type": agent.get("type", "general-purpose")}
            path = os.path.join(
                self.proj_dir, self.session_id, "subagents", f"agent-{agent['id']}.jsonl"
            )
            side = True
        self.assistant_line(
            {"type": "tool_use", "id": tid, "name": name, "input": tinput}, tokens, path, side
        )
        pre = self.run_hooks(
            "PreToolUse",
            {"tool_name": name, "tool_input": tinput, "tool_use_id": tid, **who},
            tool=name,
        )
        self.context_from(pre)
        deny = None
        for r in pre:
            spec = r["json"].get("hookSpecificOutput") or {}
            if spec.get("permissionDecision") == "deny":
                deny = spec.get("permissionDecisionReason") or r["stderr"] or "denied by hook"
            elif r["code"] == 2:
                deny = r["stderr"] or "denied by hook"
        fail = bool(step.get("fail")) or deny is not None
        result = deny if deny is not None else step.get("result", "")
        event = "PostToolUseFailure" if fail else "PostToolUse"
        post = self.run_hooks(
            event,
            {
                "tool_name": name,
                "tool_input": tinput,
                "tool_use_id": tid,
                "tool_response": result,
                **who,
            },
            tool=name,
        )
        self.context_from(post)
        self.user_line(
            [{"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": fail}],
            path,
            side,
        )
        self.out(f"\n* {name}({json.dumps(tinput)})\n")

    def subagent_step(self, sub: dict) -> None:
        aid, atype = sub["id"], sub.get("type", "general-purpose")
        path = os.path.join(self.proj_dir, self.session_id, "subagents", f"agent-{aid}.jsonl")
        self.run_hooks("SubagentStart", {"agent_id": aid, "agent_type": atype})
        self.user_line("subagent task", path, sidechain=True)
        self.assistant_line({"type": "text", "text": sub.get("text", "")}, 0, path, True)
        self.run_hooks(
            "SubagentStop",
            {
                "agent_id": aid,
                "agent_type": atype,
                "agent_transcript_path": path,
                "stop_hook_active": False,
            },
        )


def log_invocation(argv: list[str]) -> None:
    path = os.environ.get("FAKE_CLAUDE_LOG")
    if not path:
        return
    keys = (
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_PROJECT_DIR_NAME",
        "CCW_SESSION_ID",
        "CCW_INTERNAL_URL",
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"argv": argv, "cwd": os.getcwd(), "env": {k: os.environ.get(k) for k in keys}}
            )
            + "\n"
        )


def print_main(argv: list[str]) -> int:
    """`-p`/`--print`: no TTY, no hooks; prints the matching turn's text steps."""
    prompt = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in VALUED_FLAGS or a in ("--session-id", "-r", "--resume"):
            i += 1
        elif not a.startswith("-"):
            prompt = a
            break
        i += 1
    if prompt is None:
        prompt = "" if sys.stdin.isatty() else sys.stdin.read().rstrip("\n")
    turn = Fake.matching_turn_of(Fake._load_scenario(), prompt)
    texts = [st["text"] for st in turn.get("steps", []) if "text" in st]
    text = "".join(texts) if texts else f"echo: {prompt}"
    if _opt(argv, ("--output-format",)) == "json":
        print(json.dumps({"result": text}))
    else:
        print(text)
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--version"]:
        print(VERSION)
        return 0
    log_invocation(argv)
    if argv[:1] == ["plugin"]:
        return plugin_main(argv[1:])
    if "-p" in argv or "--print" in argv:
        return print_main(argv)
    fake = Fake(argv)
    if fake.resume and not os.path.isfile(fake.transcript):
        print(f"No conversation found with session ID: {fake.resume}")
        return 1
    if not sys.stdin.isatty():
        print("Error: Input must be provided via a TTY", file=sys.stderr)
        return 2
    saved = termios.tcgetattr(0)
    tty.setraw(0)
    try:
        return fake.run()
    except Exit as e:
        return e.code
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
