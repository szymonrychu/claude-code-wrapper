# Source analysis

What the three source projects do to drive Claude Code, and how each piece maps to this service
(`claude-code-wrapper`, CLI `ccw`). Deliverable D16 of the design (`docs/plans/in-progress/claude-code-wrapper-microser/design.md`,
"Source findings" was the starting point; every reference below was re-checked against the source).

Reference prefixes (all read-only):

- `W/` = tatara-new Go wrapper, `/Users/szymonri/Documents/tatara-new/code/tatara-claude-code-wrapper`, read at
  `origin/main` (the checkout is on a side branch).
- `M/` = magic-trainer Python port, `/Users/szymonri/Documents/magic-trainer/code/magic-cards-model-api/src/mtg_agent/wrapper`.
  `MT/` = `/Users/szymonri/Documents/magic-trainer/code/magic-cards-model-api` (tests, Dockerfile, compose).
- `R/` = ros2 agent node, `/Users/szymonri/Documents/ros2-lerobot-sverve-platform/nodes/claude_agent/claude_agent`.
  `RN/` = `/Users/szymonri/Documents/ros2-lerobot-sverve-platform/nodes/claude_agent`.
- `WB/` = workbench plugin, `/Users/szymonri/Documents/claude-infra-plugin`.

Line numbers are at the time of reading (2026-10); where a symbol is cited instead, it is stable across edits.

## tatara-new

Component: `tatara-claude-code-wrapper` (Go), one pod = one interactive `claude` plus an HTTP API.

- **Process model and backend.** Interactive `claude` under a PTY (creack/pty), never `-p` and never the SDK:
  `W/internal/session/pty.go:67-77` (`spawnClaude`, `exec.Command(cfg.ClaudePath, args...)`, `pty.Setsize` 50 rows x 200 cols).
  One `session.Manager` per pod (`W/internal/session/session.go:248` `New`), states Booting/Ready/Busy/Dead
  (`session.go:93-105`).
- **Spawn flags and env.** argv is only `--model`, `--effort`, `--continue` (`pty.go:51-65`, `ConfigClaudeArgs`). No permission
  flag: bypass is set in `settings.json` `defaultMode` (`W/internal/bootstrap/settings.go:60-62`). Env = `TERM=xterm-256color` +
  inherited env minus `CLAUDE_CODE_SUBAGENT_MODEL` + `HOME` (`W/cmd/wrapper/app.go:667-686`, `claudeEnv`). Config is read from env
  vars (`W/cmd/wrapper/config.go:116-193`: `TURN_TIMEOUT_SECONDS`=1800, `BOOT_TIMEOUT_SECONDS`=60, `CLAUDE_PATH`, `HOME_DIR`).
- **Boot handling.** `~/.claude.json` seeded with `hasCompletedOnboarding`, `autoUpdates`, `projects[<workspace>].hasTrustDialogAccepted`
  and the last 20 chars of the API key under `customApiKeyResponses.approved` (`W/internal/bootstrap/claudejson.go:16-69`).
  The "Bypass Permissions mode" warning is not seedable: `bootWait` sees it in the ring, sleeps 600 ms, writes ESC `[B` (Down), 250 ms,
  then CR (`session.go:641-707`, accept block at ~673). Ready = minimum boot 4 s and output idle 1.5 s, poll 150 ms, bounded by
  `BootTimeout` (`session.go:641-650`). Output goes into a 64 KiB ANSI-stripped ring that is reset between relaunches
  (`W/internal/session/ring.go`, `session.go:825-830`).
- **Turn submit and completion.** `Submit` rejects when Dead/Booting/busy (`ErrBusy`, 409 at the API), reserves the turn slot, then
  writes `ESC[200~ text ESC[201~`, sleeps `SubmitDelay`, writes a separate `\r` under `ptyMu` (`session.go:1170-1235`,
  `DefaultSubmitSeq` at `session.go:31`). Completion is the Stop hook: `cc-stop-hook` reads the hook JSON on stdin
  (`session_id`, `transcript_path`, `last_assistant_message`; no stop_reason in the payload) and POSTs to
  `127.0.0.1:8090/internal/turn-complete` with 5 attempts x 5 s, always exit 0 (`W/cmd/cc-stop-hook/main.go:26-83`,
  `hook.go:15-40`). `Complete` rejects hooks during Dead/Booting and stale hooks whose `session_id` differs from the recorded one
  (`session.go:1538-1646`). Inactivity timer (`TurnTimeout`) only marks a stall suspected, reset by tailer activity
  (`session.go:1213`, `markStallSuspected` at 368).
- **Event and tool-call sources.** Transcript tailer: 200 ms poll, reopen on inode change, 16 MiB partial-line cap, typed blocks,
  tool_use/tool_result correlation, `Task` tool tracking for subagents (`W/internal/transcript/tailer.go:20-43,199-260,529-621`);
  subagent transcripts via `subagents/agent-*.jsonl` and `IsSidechain` (`tailer.go:668`, `session.go:1404-1495`). Tool calls are only
  observed, never gated.
- **Tool gating.** Static only: `permissions.deny` in settings.json including `AskUserQuestion`, `ExitPlanMode`, `EnterPlanMode`,
  plus destructive Bash and secret-file patterns; deny wins under bypassPermissions (`W/internal/bootstrap/settings.go:10-73`).
  No runtime decision hook.
- **Lifecycle hooks.** Shell commands run via `sh -c`, best effort, failures logged and counted, never abort
  (`W/internal/bootstrap/hooks_lifecycle.go:17-60`); events `conversationStart`, `conversationRestart`, `agentTurnFinished`
  (`W/cmd/wrapper/app.go:313,324,510`). Turn results are delivered by an HMAC `sha256=<hex>` webhook with backoff (max 60 s)
  (`W/internal/webhook/webhook.go:23-155`).
- **Prompts and instructions.** `global-claude.md` and `project-claude.md` rendered from `/etc/wrapper` into `~/.claude/CLAUDE.md`
  and the workspace (`W/cmd/wrapper/config.go:184-185`, `W/internal/bootstrap/bootstrap.go:591` `Render`), plus directive blocks
  (headless, delegation, auto-push, resume) appended (`bootstrap.go:904-996`).
- **MCP.** `.mcp.json` merged from a base doc, overlay `*.json` fragments and `TATARA_EXTRA_MCP_SERVERS`; `enableAllProjectMcpServers`
  in settings (`W/internal/bootstrap/mcp.go:11-60`, `settings.go:23`).
- **Plugins and skills.** No Claude plugin installs; skills are cloned from a git repo and copied into `.claude/skills` filtered by a
  profile frontmatter (`W/internal/bootstrap/skills.go:30-110,384-466`). Per-repo `mise install` is done by the agent
  (`W/Dockerfile:136-168`, mise baked in with shims).
- **Persistence and resume.** Crash relaunch with `--continue` only when a transcript exists, otherwise `--continue` would exit
  immediately and burn the restart budget (`session.go:796-907`, `shouldResume` at 855). Workspace recovery (stale git locks, rescue
  branch) in `W/internal/bootstrap/resume.go`. Cross-pod resume = copy `~/.claude/projects/<cwd with / -> ->/<sid>.jsonl` and run
  `--resume <sid>` (`W/docs/conversation-resume-spike.md`, section 1; verified on claude 2.1.183).
- **Auth.** Claude: OAuth token or API key from env (`W/cmd/cc-statusline/main.go` doc; `claudejson.go` key approval). API:
  OIDC JWT middleware, issuer + audience (`W/internal/auth/auth.go:6-20`, `middleware.go`), public listener plus loopback internal
  listener (`W/internal/httpapi/api.go:151-200`: `/v1/messages`, `/v1/session`, `/v1/transcript`, `/v1/probe`, `/v1/interrupt`,
  `/metrics`; internal `/internal/turn-complete`, `/internal/account-usage`).
- **Container and deploy.** node bookworm-slim, pinned `@anthropic-ai/claude-code` npm layer, non-root uid 10001 (`useradd -m -u 10001
  agent`), mise via mise.run, `/workspace` (`W/Dockerfile:90-168`); Helm chart with PVC, networkpolicy, servicemonitor
  (`W/charts/tatara-claude-code-wrapper/templates/`).
- **Concurrency and timeouts.** One turn at a time (409); `ptyMu` serialises PTY writes; interrupt = one ESC, resolved when the
  transcript shows `[Request interrupted by user` (`W/internal/session/interrupt.go:14,55-140`); mid-turn probe
  (`session.go:1236+`, `W/internal/httpapi/probe.go`); bounded `Shutdown`: Ctrl-C, close PTY, SIGKILL, goroutine join timeout
  (`session.go:1815-1860`). Pod TTL admission (`admit`, `podDeadline`).
- **Tests and fakes.** Stub interactive claude `W/cmd/wrapper/testdata/stubclaude/main.go` (prints readiness, parses paste blocks,
  appends transcript lines, runs the stop hook); injectable `now`, `newID`, spawn (`session.go:248`, `SetSpawnForTest` at 502).
- **Pitfalls hit.** Bypass dialog cannot be seeded; `--continue` with no transcript crash-loops (`session.go:855+`); stale ring tail
  after relaunch (`session.go:825-830`); hook arrives during recovery or from a previous session; ambient
  `CLAUDE_CODE_SUBAGENT_MODEL` overrides agent models (`app.go:670-677`); status line fires on every redraw so it must not retry
  (`W/cmd/cc-statusline/main.go` header).

## magic-trainer

Component: `mtg_agent.wrapper` (Python port of the tatara wrapper), one container = one seat session.

- **Process model and backend.** `pty.fork` + `os.execvpe` of interactive claude, no SDK (`M/pty_proc.py:1-8,122-162`); explicit
  note that it never runs `claude -p` for the play session (`M/oneshot.py:1-12`). Two FastAPI apps on two uvicorn servers: public
  `:8080` and loopback `127.0.0.1:8090` carrying only `/internal/turn-complete` (`M/api.py:1-7,317-325`, `M/__main__.py`).
- **Spawn flags and env.** argv = claude path + `--model`, `--effort`, `--continue` only (`M/config.py:255-270` `claude_args`);
  winsize 50x200 (`M/pty_proc.py:31-32,117`). OAuth token wins and `ANTHROPIC_API_KEY` is popped, else the reverse
  (`M/pty_proc.py:133-143`); `MAX_THINKING_TOKENS` set or popped (`pty_proc.py:150-152`). Settings come from env
  (`M/config.py:60-203`, dataclass `Settings`).
- **Boot handling.** Same method as tatara: `BYPASS_NEEDLE`, Down then Enter, `MIN_BOOT` 4 s, `IDLE_READY` 1.5 s, `BOOT_POLL` 0.15 s
  (`M/session.py:39-75,292-360`); ready markers `bypass permissions on` / `? for shortcuts` (`session.py:43`); one relaunch on boot
  failure (`_relaunch`, `_boot_failed` at 362-388). `~/.claude.json` seed (`M/bootstrap.py:36-72`).
- **Turn submit and completion.** Bracketed paste, `submit_delay` 0.4 s, separate CR (`M/session.py:505-548`, `config.py:177`). A
  turn marker `<!-- wrapper turn: <id> -->` is appended to the prompt because Stop carries no turn id; the Stop hook reads the last
  marker in the transcript (`M/transcript.py:30-36,114`, `session.py:527-529`). Stop hook is a stdlib script with 5 x 5 s retries
  (`M/stop_hook.py:26-108`). Completion on `/internal/turn-complete` -> `Manager.complete` (`session.py:591`).
- **Event and tool-call sources.** Post-hoc transcript reads for the result and per-model token totals deduplicated by message id
  (`M/transcript.py:64-148`); live narration by a transcript tail that globs `$HOME/.claude/projects/*/*.jsonl` and follows the
  newest file, plus subagent roster from `agent-*.jsonl` (`M/activity.py:1-30,458,809-815,1083-1087`). Written to stdout only; there
  is no event stream API.
- **Tool gating.** Static deny list `AskUserQuestion`, `ExitPlanMode`, `EnterPlanMode` in settings (`M/bootstrap.py:27,75-93`);
  per-seat agent definitions with tool allowlists (`bootstrap.py:140-330`). No runtime gate.
- **Lifecycle hooks.** None beyond the Stop relay; a handover marker wait for the driver's preStop
  (`M/handover_wait.py`).
- **Prompts and instructions.** Project and global `CLAUDE.md` copied from configured paths (`M/bootstrap.py:119-130`); `files` map
  written into the workspace before submit with relative-path, escape and 256 KiB guards (`M/api.py:46-80`).
- **MCP.** `.mcp.json` from base file + overlay dir, `enableAllProjectMcpServers` (`M/bootstrap.py:96-116`); MCP limits via env
  `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH`, `MAX_MCP_OUTPUT_TOKENS` (`MT/docker-compose.yml:195-198`).
- **Plugins and skills.** Skills copied into `.claude/skills` (`M/bootstrap.py:333-350`); no plugin installs.
- **Persistence and resume.** Relaunch uses `--continue` (`config.py:268`); `POST /v1/session` restarts the session
  (`M/api.py:190-214`, `Manager.restart` at `session.py:442`). No cross-container resume.
- **Auth.** Claude: OAuth token (`claude setup-token`) or API key (`MT/docker-compose.yml:163-172`). API: shared secret header
  `X-Wrapper-Secret` (`M/api.py:151-156`).
- **Container and deploy.** `MT/Dockerfile.agent:25-47`: pinned claude npm layer, non-root `agent` user because claude refuses
  bypass mode as root; compose service with `HOME=/agent-home`, `/workspace`, named volumes (`MT/docker-compose.yml:130-230`).
- **Concurrency and timeouts.** One turn (BusyError -> 409); turn inactivity timer re-armed from transcript progress
  (`session.py:549-590`); `turn_timeout` 1800 s; interrupt = ESC then a second ESC after `interrupt_settle` 20 s that fails the turn,
  because an interrupted turn runs no Stop hook (`session.py:728-797`); PTY close order SIGTERM group, close fd, SIGKILL after 5 s
  (`M/pty_proc.py:28-29,73-110`, macOS deadlock otherwise); fatal needles `API Error: 401/403`, `Please run /login`,
  `Credit balance is too low`, `OAuth token has expired` fail the turn (`session.py:45-68,258-290`).
- **Tests and fakes.** `MT/tests/testdata/stubclaude.py` is an interactive claude stand-in (bypass dialog needs Down+Enter, paste
  + CR, writes the transcript, runs the Stop hook); injectable `now`, `new_id`, `spawn` (`session.py:128-150`); tests under
  `MT/tests/wrapper/`.
- **Pitfalls hit.** Unseedable bypass dialog; claude exits when submitted too early (boot on quiescence, never a delay);
  macOS PTY close deadlock; hung turns on 401 with claude parked at the prompt (the `FATAL_NEEDLES` docstring, `session.py:46-61`);
  root refusal; Stop has no turn id; a `-p` review route diverges from the interactive harness (`M/oneshot.py:1-12`).

## ros2-lerobot-sverve-platform

Component: `nodes/claude_agent` (FastAPI + Claude Agent SDK, systemd, rclpy node).

- **Process model and backend.** `ClaudeSDKClient` from `claude-agent-sdk==0.2.163` (`RN/pyproject.toml:8`; `R/runner.py:16-19,357`),
  not a PTY and not `-p`; one session, one instruction at a time (`R/runner.py` `AgentRunner`). Rejected as backend for this service
  (design D1): it does not run the interactive harness.
- **Spawn options and env.** `ClaudeAgentOptions(... permission_mode="default", can_use_tool=gate.can_use_tool, setting_sources=[],
  strict_mcp_config=True, env=env, cwd=work_dir)` (`R/runner.py:305-326`). Child env built explicitly: `ANTHROPIC_API_KEY` and
  `ANTHROPIC_AUTH_TOKEN` removed, `DISABLE_AUTOUPDATER=1`, `ENABLE_CLAUDEAI_MCP_SERVERS=false`, optional
  `CLAUDE_CODE_PROMPT_CACHE_TTL`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (`R/runner.py:97,234-255`).
- **Boot handling.** None needed (SDK); connect bounded by `connect_timeout_s` (`R/runner.py:1157-1169`).
- **Turn submit and completion.** `POST /api/message` returns 202 and the result arrives as events, 409 while busy
  (`R/api.py:141-156`); `POST /api/stop` interrupts, `POST /api/reset` starts a new session (`api.py:158-168`). Completion is the
  SDK `ResultMessage` mapped to a `turn_end` event (`R/events.py:318-359`).
- **Event and tool-call sources.** SDK messages normalised to events (`R/events.py:252-318`). Event log: JSONL file with `seq`
  continuing across restarts, RAM ring, paging via `before_seq`, rotation dropping the oldest half, subscriber queues
  (`R/events.py:54-226`). `/ws/events` sends a `history` frame then live events with seq de-dup and a reset marker
  (`R/api.py:170-189`).
- **Tool gating.** `can_use_tool` callback `PermissionGate.can_use_tool` wraps `decide()` and turns any exception into an explicit
  deny with a reason (`R/tools.py:319-340`); also time-budget refusals (`tools.py:300-317`) and a `deny_tools` per-request list
  (`R/api.py:45-64,141-156`). PostToolUse hook adds `additionalContext` (`R/runner.py:514-526`). `AskUserQuestion` is in the
  disallowed set (`R/tools.py:71`).
- **Lifecycle hooks.** Only the PostToolUse callback; domain-specific start/end checks (job-end check, park, retrospectives) are
  in `R/runner.py:1110-1560` and `R/retrospectives.py`.
- **Prompts.** System prompt from a template plus `system_prompt_extra` (`R/prompt.py:5,380`; `R/config.py:267`).
- **MCP.** Robot MCP over HTTP with bearer token from a file, plus an in-process plan server; `strict_mcp_config`
  (`R/runner.py:319-322`); MCP health probing (`R/mcp_health.py`, `runner.py:628-701`).
- **Plugins and skills.** A read-only local plugin of skills shipped with the node (`R/skills.py:1-30`, option
  `plugins=[{"type": "local", ...}]` at `runner.py:299-303`).
- **Persistence and resume.** Event log persisted, conversation not resumed (`reset` starts fresh; `R/__main__.py:79-83` wires the
  log path).
- **Auth.** Claude: `CLAUDE_CODE_OAUTH_TOKEN` only; a 401 result is detected and reported with remediation text
  (`R/runner.py:217-218`, `R/events.py:345`). HTTP API: loopback bind only, no token (`RN/README.md:10-20`).
- **Container and deploy.** No container: systemd service on the robot, uvicorn on 127.0.0.1:18300 (`RN/README.md:10-20,655-662`).
- **Concurrency and timeouts.** `asyncio.timeout(instruction_time_left())` watchdog (`R/runner.py:1110-1135`); interrupt bounded by
  `stop_timeout_s` (`runner.py:1040-1045`); bounded shutdown: each of interrupt and disconnect within
  `shutdown_client_timeout_s`, then the CLI child is terminated and killed (`runner.py:1915-1959`); uvicorn
  `timeout_graceful_shutdown=3` (`R/__main__.py:25,98-104`).
- **Config.** pydantic `extra="forbid"`; invalid config exits `os.EX_CONFIG` = 78 (`R/config.py:238`, `R/__main__.py:62-66`).
- **Metrics.** prometheus_client counters/histograms for instructions, turns, tool uses, tokens, watchdog fires
  (`R/metrics.py:1-49`).
- **Tests and fakes.** `FakeClient` injected via `client_factory` (`RN/tests/test_runner.py:48,215`).
- **Pitfalls hit.** An exception escaping the permission callback gives the model a generic rejection (hence the explicit-deny
  wrapper, `tools.py:319-325`); disk failures must not break the agent (`events.py:119-134`); open WebSockets block shutdown
  (`__main__.py:25`); a missing/expired OAuth token surfaces as a 401 result (`runner.py:217`).

## Workbench compatibility

What the workbench plugin (`workbench@szymonrychu-workbench`, `WB/.claude-plugin/plugin.json`, version 2.1.0, directory marketplace
`WB/.claude-plugin/marketplace.json`) needs from a host, and how the service provides it:

| Need | Evidence | Service answer |
|---|---|---|
| Tree root as cwd; plans root = git top of cwd | `WB/scripts/lib/workbench.py:131` (`git rev-parse --show-toplevel`) | workdir mode `mounted` or `clone` at the session workdir; claude is spawned with cwd = workdir |
| Fixed absolute path (task files store absolute worktree paths) | plan-execute task files, `WB/workflows/plan-execute.js` | mode `mounted` at a fixed path, e.g. `/workdir/workbench` |
| Plugin install from a directory marketplace (no git remote, must be mounted) | `WB/.claude-plugin/marketplace.json` (`source: "./"`) | `plugins` section: `claude plugin marketplace add <dir>`, `claude plugin install workbench@szymonrychu-workbench`, per session config dir |
| Plugin hooks run in the claude process | `plugin.json` hooks (PreToolUse Bash `guard-bash.py`, PostToolUse, SubagentStop) | service never copies them into settings.json (no double firing); its own relay hooks coexist |
| Host tools: bash, python3 >= 3.11, git, jq, node, mise, uv, ruff, shellcheck, gh/glab, ssh | `WB/hooks/*.py`, `WB/scripts/run-tests.sh`, skills | container image (D10) installs them |
| `~/.claude/CLAUDE.md` and `~/.claude/rules/*.md` | workbench flow relies on them | `claude.config_files` copies them into the session `CLAUDE_CONFIG_DIR`; `prompts.global_claude_md` |
| Auth: OAuth token | `WB/scripts/lib/claude_json.py:4,29` strips `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` for its `claude -p` helpers | `CLAUDE_CODE_OAUTH_TOKEN` via `claude.env`; token wins, key dropped with a warning |
| Long-lived sessions (Workflow, Monitor, background Bash outlive a turn) | `WB/README.md` (workflows, hooks reference) | one interactive claude per session until destroyed; unsolicited Stop hooks become `origin: agent` turns |
| `AskUserQuestion` for approval gates | `WB/skills/start-development/SKILL.md:214` | PreToolUse relay denies it with guidance and emits a `question` event, also POSTed to `callbackUrl` (D20) |
| Forge tokens and SCM env | `GH_TOKEN`, `GITLAB_TOKEN`, `SSH_AUTH_SOCK`, `WORKBENCH_*` | `claude.env.extra` passthrough |

## Feature mapping

Adopted as: `as is` = same mechanism ported; `adapted` = same idea, changed shape; `new` = not in any source.

| Feature | Sources (project + file) | Adopted as | Service module |
|---|---|---|---|
| PTY driver | W `internal/session/pty.go`; M `pty_proc.py` | adapted (stdlib `pty.fork`, SIGTERM group then SIGKILL after 5 s, 50x200) | `pty_proc.py`, `ring.py` |
| Boot dialogs | W `session.go` `bootWait`, `claudejson.go`; M `session.py` `boot_wait`, `bootstrap.py` | adapted (configurable pattern -> keys handlers, quiescence, boot timeout with one relaunch) | `bootstrap/claude_json.py`, `session.py` |
| Turn submit | W `session.go` `Submit`; M `session.py` `submit` | as is (bracketed paste, delay, separate CR, 409 busy) plus turn marker from M | `session.py`, `turns.py` |
| Stop completion | W `cmd/cc-stop-hook`, `session.go` `Complete`; M `stop_hook.py`, `session.py` `complete` | adapted (stale-hook rejection by state and session id, marker for turn id) | `router.py`, `hook_relay.py` |
| Transcript tailer | W `transcript/tailer.go`; M `activity.py`, `transcript.py` | adapted (200 ms poll, inode reopen, partial cap, subagent files, usage dedupe by message id) | `transcript.py` |
| Relay hooks | W `cmd/cc-stop-hook`, `cc-statusline`; M `stop_hook.py` | adapted (one `ccw-hook <Event>` for UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, Stop, SubagentStart/Stop) | `hook_relay.py`, `internal_api.py`, `bootstrap/settings.py` |
| Tool gate | R `tools.py` `can_use_tool`, `runner.py` PostToolUse; W/M `settings.go` deny list | adapted (PreToolUse relay answered by blocking `onAgentToolCall` actions, exception-safe deny, PostToolUse `additionalContext`) | `router.py`, `hooks.py` |
| AskUserQuestion forwarding | W/M deny list; WB `start-development/SKILL.md:214` | new (deny with guidance, `question` event, POST to callbackUrl) | `router.py`, `bootstrap/settings.py` |
| Event log and SSE | R `events.py`, `api.py` `/ws/events`; W/M none | adapted (JSONL log with continuing seq, ring, paging, subscribers; SSE with `Last-Event-ID` replay instead of WebSocket) | `events.py`, `api/events.py` |
| Hooks dispatcher | W `hooks_lifecycle.go`, `webhook.go` | adapted (command and webhook actions per event, blocking/veto, HMAC, retries, `on_error`) | `hooks.py` |
| Typed claude.env | W `app.go` `claudeEnv`; M `pty_proc.py` env; R `runner.py` `build_child_env` | new model, adapted rules (OAuth wins, explicit child env, `DISABLE_AUTOUPDATER`) | `claude_env.py`, `redact.py` |
| Workdir init | W `bootstrap/bootstrap.go`, `resume.go`; M `api.py` `files` | adapted (empty / mounted / clone modes, setup commands, path-escape guard for `files`) | `workdir.py` |
| Plugin install | WB `.claude-plugin/marketplace.json`; W `skills.go` (skills only) | new (real `claude plugin marketplace add` / `install`, verified via `plugin list --json`) | `plugins.py` |
| mise | W `Dockerfile:136-168`; WB tool needs | adapted (global config at start, per-session trust + install, `MISE_*` env) | `tooling.py` |
| Sessions and resume | W `session.go` `relaunch`/`shouldResume`, `docs/conversation-resume-spike.md`; M `Manager.restart` | adapted (multi-session, per-session config dir, `--session-id` then `--resume`, `--resume` only when the transcript exists) | `manager.py`, `session.py` |
| Auth | W `internal/auth`; M `X-Wrapper-Secret`; R loopback | adapted (optional bearer token, constant-time compare; loopback internal API with per-session token) | `api/auth.py`, `internal_api.py` |
| Metrics | W `internal/metrics/metrics.go`; M `metrics.py`; R `metrics.py` | adapted (prometheus_client, service-level) | `metrics.py` |
| Container | W `Dockerfile`; M `Dockerfile.agent`, `docker-compose.yml`; R systemd | adapted (python slim + node + pinned claude, non-root uid 1000, named volumes workdir/sessions/mise) | `Dockerfile`, `docker-compose.yml` |
| Config loading | R `config.py` (`extra="forbid"`, exit 78) | as is | `config.py`, `interpolation.py`, `cli.py` |
| Bounded shutdown | R `runner.py:1915-1959`, `__main__.py:25`; W `Shutdown` | as is | `service.py`, `manager.py` |
| Fake claude for tests | W `stubclaude/main.go`; M `tests/testdata/stubclaude.py`; R `FakeClient` | adapted (scripted PTY program with hooks, transcript, plugin subcommands) | `tests/fake_claude/fake_claude.py` |

## Not adopted

| Source feature | Where | Why not |
|---|---|---|
| Agent SDK backend (`ClaudeSDKClient`) | R `runner.py` | D1: the service must drive the interactive harness a human gets (skills, plugin hooks, Workflow, Monitor); the SDK runs a different one |
| `claude -p` review route | M `oneshot.py`, `api.py` `/v1/review` | print mode assembles a different system prompt and diverges on skills, hooks and permissions (M's own docstring); domain-specific review job |
| S3 / cross-pod conversation resume | W `convstore`, `docs/conversation-resume-spike.md` | the service resumes from its own `sessions` volume; no object storage, single instance per project (D23) |
| Operator stall policy (stall -> restart/kill) | W `session.go` `markStallSuspected` and operator contract; M `mark_stall_suspected` | stall is only reported as `stall_suspected` (D13); optional hard `turn_max_seconds` instead |
| Metrics push (domain counters such as git commit/push results) | W `internal/metrics/metrics.go:18-20` (`CommitPushTotal`, `SafetyPushTotal`); none of the three sources pushes metrics to a gateway, all expose a scrape endpoint (W chart `servicemonitor.yaml`, M `api.py:309`, R `api.py:96`) | service exposes `/metrics` for scrape only; git push counters are tatara domain flow |
| Account usage statusline | W `cmd/cc-statusline`, `/internal/account-usage` | tatara-operator specific, subscriber-only data, fires on every redraw; no consumer here |
| OIDC JWT auth | W `internal/auth` | bearer token is enough for the trusted-caller model (D9, D23) |
| Domain flows | W: task branches, repo claims, rescue branches, skills profiles, tatara MCP, directive blocks, resume directives; M: seats, decks, handover, review, agents, activity roles; R: robot MCP, plan tools, budget, retro, workflows, ROS topics | project-specific, not part of a generic wrapper |
| Per-pod single session | W/M | replaced by multi-session manager (D4) |
| WebSocket event feed | R `/ws/events` | SSE plus polling (D5) |
| Hard-coded deny lists (kubectl, helm, git push -f ...) | W `settings.go:48-57` | policy belongs in user config / hooks, not the service |
