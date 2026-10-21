"""Multi-session manager: the one place the parts are composed (D4, D2, D12, D14, D11).

Each session owns `<paths.sessions_root>/<session_id>/` (`session.json`, `config/` as its
`CLAUDE_CONFIG_DIR`, `events.jsonl`, `turns.jsonl`, `gitconfig`, `setup.done`) and a workdir
(`<paths.workdir_root>/<name>` for modes empty and clone, a mounted absolute path otherwise).

`create` prepares everything from config before claude starts, one small named step at a time;
`session.json` records the running step, so a failure names it (`CreateFailed(step, message)`).
`destroy` keeps every file unless `purge=True` (D19), and never deletes a mounted workdir.
`resume` relaunches a stopped session with `--resume` in the same config dir and workdir.

`session.json` never holds a secret value: the per-session env override is stored without its
secret fields, so a resume must resend them (D24). The session `callbackUrl` is persisted and is
the router's default callback when a turn has none (D20).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import inspect
import json
import logging
import re
import secrets
import shutil
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .bootstrap import (
    atomic_write,
    copy_config_files,
    ensure_config_dir,
    seed_claude_json,
    write_instructions,
)
from .bootstrap.settings import relay_timeouts, render_settings
from .claude_env import SECRET_KEY_RE, ClaudeEnv
from .config import Config, InitialPromptTemplate
from .events import EventLog
from .hooks import RETRY_BASE_SECONDS, HookDispatcher, HookOutcome, build_payload, gate_fail_mode
from .metrics import Metrics
from .plugins import install_plugins
from .prompts import render_template, resolve_prompt
from .redact import Redactor
from .router import DEFAULT_DENY_REASON, SessionInfo, SessionRouter, TurnVetoed
from .session import (
    BASE_ENV,
    PROJECT_DIR_NAME,
    PUBLIC_STATES,
    Session,
    SessionSpec,
    TurnRejected,
    build_env,
)
from .tooling import install_workdir, mise_env
from .turns import TurnStore, now_iso
from .workdir import prepare_workdir, run_setup_commands, write_gitconfig

log = logging.getLogger(__name__)

#: How often the idle reaper looks for sessions idle longer than `idle_timeout_seconds`.
REAPER_INTERVAL_SECONDS = 30.0
# Shares of `limits.shutdown_timeout_seconds` (see SessionManager.shutdown).
SHUTDOWN_GRACE_SHARE = 0.3
SHUTDOWN_CLOSE_SHARE = 0.15
SHUTDOWN_HOOK_SHARE = 0.25
SHUTDOWN_STOP_SHARE = 0.75
SHUTDOWN_KILL_CLOSE_SHARE = 0.1
SESSION_ID_RE = re.compile(r"^ses_[0-9a-f]{12}$")
META_FILE = "session.json"
#: Persisted states whose claude died with the previous service process.
LIVE_STATES = ("creating", "ready", "busy")


class ManagerError(Exception):
    """Base of the manager's errors; `code` is the API error code."""

    code = "error"


class SessionNotFound(ManagerError):
    code = "not_found"

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session {session_id!r} not found")
        self.session_id = session_id


class TooManySessions(ManagerError):
    code = "too_many_sessions"

    def __init__(self, limit: int) -> None:
        super().__init__(f"sessions.max_sessions ({limit}) reached")
        self.limit = limit


class CreateVetoed(ManagerError):
    code = "vetoed"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ResumeVetoed(CreateVetoed):
    """An `onResume` hook denied the resume."""


class CreateFailed(ManagerError):
    """A create or resume step failed; `message` is redacted."""

    code = "create_failed"

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"step {step} failed: {message}")
        self.step = step
        self.message = message


class ResumeUnavailable(ManagerError):
    code = "not_resumable"

    def __init__(self, session_id: str, reason: str) -> None:
        super().__init__(f"session {session_id} cannot be resumed: {reason}")
        self.session_id = session_id
        self.reason = reason


class InvalidState(ManagerError):
    """The operation does not apply to the session's (or the manager's) current state."""

    code = "invalid_state"

    def __init__(self, session_id: str | None, state: str, message: str = "") -> None:
        subject = f"session {session_id}" if session_id else "manager"
        super().__init__(message or f"{subject} is {state}")
        self.session_id = session_id
        self.state = state


class InvalidRequest(ManagerError, ValueError):
    """The merged env (config default plus override) does not validate."""

    code = "invalid_request"


class CreateSessionRequest(BaseModel):
    """`POST /v1/sessions` body. `workdir` is a mounted absolute path (mode mounted) or a
    workdir name (modes empty and clone); `env` is a partial `ClaudeEnv` override."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    workdir: str | None = None
    model: str | None = None
    effort: str | None = None
    env: ClaudeEnv | None = None  # type: ignore[valid-type]
    prompt: str | None = None
    template: str | None = None
    variables: dict[str, str] | None = None
    callback_url: str | None = Field(default=None, alias="callbackUrl")


class _StepFailed(Exception):
    def __init__(self, step: str, cause: BaseException) -> None:
        super().__init__(step)
        self.step = step
        self.cause = cause


class _ManagedRouter(SessionRouter):
    """The event router, also telling the manager about public state changes."""

    def __init__(self, *args: Any, on_state_change: Callable[[str], None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_state_change = on_state_change

    def on_state(self, state: str) -> None:
        super().on_state(state)
        self._on_state_change(state)


@dataclass
class _Active:
    """A session this process runs (or is creating, resuming or closing)."""

    meta: dict[str, Any]
    session_dir: Path
    override: Any
    claude_env: Any
    hook_token: str
    phase: str = "creating"  # creating | running | closing
    workdir: Path | None = None
    event_log: EventLog | None = None
    turns: TurnStore | None = None
    dispatcher: HookDispatcher | None = None
    dispatcher_cwd: Path | None = None
    router: SessionRouter | None = None
    session: Session | None = None
    task: asyncio.Task[Any] | None = None
    background: set[asyncio.Task[Any]] = field(default_factory=set)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    activated: float = field(default_factory=time.time)

    @property
    def session_id(self) -> str:
        return str(self.meta["session_id"])

    @property
    def config_dir(self) -> Path:
        return self.session_dir / "config"


def _persistable_env(env: Any) -> dict[str, Any]:
    """The override as stored in session.json: every set field except secret ones."""
    out: dict[str, Any] = {}
    for name in sorted(env.model_fields_set):
        if name == "extra":
            continue
        value = getattr(env, name)
        if not isinstance(value, SecretStr):
            out[name] = value
    extra = {k: v for k, v in env.extra.items() if not SECRET_KEY_RE.search(k)}
    if extra:
        out["extra"] = extra
    return out


def _epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


class SessionManager:
    """Runs up to `sessions.max_sessions` claude sessions and keeps every past one on disk.

    `started` is set by service-main once config, global mise, `load_persisted()` and both
    listeners are up (readiness). `internal_url` may be replaced once the internal port is bound.
    """

    def __init__(
        self,
        cfg: Config,
        *,
        internal_url: str,
        service_env: Mapping[str, str],
        redactor: Redactor,
        api_url: str = "",
        relay_command: str | None = None,
        reaper_interval: float = REAPER_INTERVAL_SECONDS,
        metrics: Metrics | None = None,
    ) -> None:
        self.metrics = metrics
        self.cfg = cfg
        self.internal_url = internal_url
        self.service_env = dict(service_env)
        self.api_url = api_url
        self.redactor = redactor
        self.started = False
        self._relay_command = relay_command
        self._reaper_interval = reaper_interval
        self._root = Path(cfg.paths.sessions_root)
        self._active: dict[str, _Active] = {}
        self._dispatchers: set[HookDispatcher] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._reaper: asyncio.Task[None] | None = None
        self._closing = False

    # ------------------------------------------------------------------ lookup

    def active_ids(self) -> list[str]:
        return list(self._active)

    def get(self, session_id: str) -> dict[str, Any]:
        ctx = self._active.get(session_id)
        if ctx is not None:
            return self._snapshot(ctx)
        return self._persisted_snapshot(self._read_meta(session_id))

    def list(self, state: str | None = None) -> list[dict[str, Any]]:
        """Active sessions plus every persisted one, optionally filtered by public state."""
        if state is not None and state not in PUBLIC_STATES:
            raise ValueError(f"state must be one of {', '.join(PUBLIC_STATES)}, got {state!r}")
        out = [self._snapshot(ctx) for ctx in list(self._active.values())]
        for meta in self._scan():
            if meta["session_id"] not in self._active:
                out.append(self._persisted_snapshot(meta))
        if state is not None:
            out = [s for s in out if s["state"] == state]
        return sorted(out, key=lambda s: (s.get("created_at") or "", s["session_id"]))

    def router(self, session_id: str) -> SessionRouter:
        """The live router (internal API); `InvalidState` when the session is not running."""
        ctx = self._active.get(session_id)
        if ctx is not None and ctx.router is not None:
            return ctx.router
        meta = self._read_meta(session_id)
        raise InvalidState(session_id, str(meta.get("state")))

    def hook_token_matches(self, session_id: str, token: str | None) -> bool:
        ctx = self._active.get(session_id)
        if ctx is None or not token:
            return False
        return hmac.compare_digest(ctx.hook_token.encode(), token.encode())

    def transcript_path(self, session_id: str) -> str | None:
        """The main transcript file: the live session's, else the persisted layout's."""
        ctx = self._active.get(session_id)
        if ctx is not None and ctx.session is not None:
            return str(ctx.session.transcript_path)
        meta = self._read_meta(session_id)
        claude_sid = meta.get("claude_session_id")
        if not claude_sid:
            return None
        return str(
            self._session_dir(session_id)
            / "config"
            / "projects"
            / PROJECT_DIR_NAME
            / f"{claude_sid}.jsonl"
        )

    def open_event_log(self, session_id: str) -> EventLog:
        """The live EventLog, else a read-only one opened from the session dir."""
        ctx = self._active.get(session_id)
        if ctx is not None and ctx.event_log is not None:
            return ctx.event_log
        self._read_meta(session_id)
        return EventLog(
            self._session_dir(session_id) / "events.jsonl",
            session_id,
            ring_size=self.cfg.limits.event_ring_size,
            max_bytes=self.cfg.limits.event_log_max_bytes,
            readonly=True,
        )

    def open_turns(self, session_id: str) -> TurnStore:
        """The live TurnStore, else a read-only one opened from the session dir."""
        ctx = self._active.get(session_id)
        if ctx is not None and ctx.turns is not None:
            return ctx.turns
        self._read_meta(session_id)
        return TurnStore(self._session_dir(session_id) / "turns.jsonl", readonly=True)

    # ------------------------------------------------------------------ create

    async def create(self, req: CreateSessionRequest | Mapping[str, Any]) -> dict[str, Any]:
        """Prepare and start a session; returns its ready snapshot once onCreate passed.

        The initial prompt is submitted in a tracked background task, never awaited here.
        """
        if not isinstance(req, CreateSessionRequest):
            req = CreateSessionRequest.model_validate(req)
        self._check_open()
        initial = self._initial_prompt(req)
        override = req.env if req.env is not None else ClaudeEnv()
        claude_env = self._merge_env(override)
        self._check_capacity()
        session_id = self._new_session_id()
        now = now_iso()
        meta: dict[str, Any] = {
            "session_id": session_id,
            "claude_session_id": str(uuid.uuid4()),
            "name": req.name,
            "workdir": None,
            "workdir_mode": self.cfg.init.workdir.mode,
            "model": req.model or self.cfg.claude.model,
            "effort": req.effort or self.cfg.claude.effort,
            "env": _persistable_env(override),
            "callbackUrl": req.callback_url,
            "created_at": now,
            "updated_at": now,
            "state": "creating",
            "step": None,
            "error": None,
            "last_activity_at": now,
            "claude_started": False,
        }
        ctx = self._activate(meta, override, claude_env)
        try:
            await self._step(ctx, "session_dir", self._open_session_dir, ctx)
            ctx.workdir = await self._step(
                ctx, "prepare_workdir", self._prepare_workdir, ctx, req.workdir
            )
            ctx.meta["workdir"] = str(ctx.workdir)
            gitconfig = ctx.session_dir / "gitconfig"
            await self._step(
                ctx,
                "write_gitconfig",
                write_gitconfig,
                ctx.session_dir,
                self.cfg.init.git,
                ctx.workdir,
            )
            await self._step(
                ctx,
                "run_setup_commands",
                run_setup_commands,
                self.cfg.init.setup_commands,
                ctx.workdir,
                self._tool_env(ctx, gitconfig),
                ctx.session_dir / "setup.done",
            )
            await self._step(
                ctx,
                "install_workdir",
                install_workdir,
                self.cfg.mise,
                ctx.workdir,
                self._tool_env(ctx, gitconfig),
            )
            await self._bootstrap(ctx)
            await self._launch(ctx, resume=False)
        except _StepFailed as exc:
            self._count_created("failed")
            raise await self._fail(ctx, exc, "create_failed") from exc.cause
        except asyncio.CancelledError:
            await asyncio.shield(self._abandon(ctx))
            raise
        finally:
            ctx.task = None
        if initial:  # tracked, not awaited: it first runs after this returns
            self._set_step(ctx, "initial_prompt")
            self._track(ctx, self._initial_turn(ctx, initial))
        self._set_step(ctx, "ready")
        self._count_created("ok")
        return self._snapshot(ctx)

    def _count_created(self, result: str) -> None:
        if self.metrics is not None:
            self.metrics.sessions_created(result)

    def _initial_prompt(self, req: CreateSessionRequest) -> str | None:
        text = resolve_prompt(
            self.cfg,
            text=req.prompt or None,
            template=req.template,
            variables=req.variables,
            required=False,
        )
        if text:
            return text
        configured = self.cfg.prompts.initial_prompt
        if isinstance(configured, InitialPromptTemplate):
            return render_template(self.cfg, configured.template, configured.variables)
        return configured or None

    async def _initial_turn(self, ctx: _Active, text: str) -> None:
        assert ctx.router is not None
        try:
            await ctx.router.submit_turn(text, origin="initial")
            return
        except (TurnVetoed, TurnRejected) as exc:
            reason = exc.reason
        except Exception as exc:  # noqa: BLE001 - reported as an event, the session stays up
            log.exception("session %s: initial turn failed", ctx.session_id)
            reason = f"{type(exc).__name__}: {exc}"
        self._emit(
            ctx,
            "error",
            {
                "kind": "initial_turn_rejected",
                "reason": reason,
                "message": f"initial turn rejected: {reason}",
            },
        )

    # ------------------------------------------------------------------ shared steps

    async def _bootstrap(self, ctx: _Active) -> None:
        """Config-dir rendering, plugins and settings: run by create and by resume."""
        await self._step(ctx, "ensure_config_dir", ensure_config_dir, ctx.config_dir)
        await self._step(
            ctx,
            "seed_claude_json",
            seed_claude_json,
            ctx.config_dir,
            ctx.workdir,
            self.cfg.claude,
            ctx.claude_env,
        )
        await self._step(
            ctx, "write_instructions", write_instructions, ctx.config_dir, self.cfg.prompts
        )
        await self._step(
            ctx, "copy_config_files", copy_config_files, ctx.config_dir, self.cfg.claude
        )
        await self._step(ctx, "install_plugins", self._install_plugins, ctx)
        await self._step(
            ctx,
            "render_settings",
            render_settings,
            ctx.config_dir,
            self.cfg,
            self._relay_command,
        )

    async def _launch(self, ctx: _Active, *, resume: bool) -> None:
        """Child env, wiring, claude start and the onCreate/onResume gate."""
        env = await self._step(ctx, "child_env", self._child_env, ctx)
        await self._step(ctx, "wire", self._wire, ctx, env, resume)
        assert ctx.session is not None
        ctx.meta["claude_started"] = True
        await self._step(ctx, "start", ctx.session.start)
        event = "onResume" if resume else "onCreate"
        outcome: HookOutcome = await self._step(
            ctx,
            "on_resume" if resume else "on_create",
            self._fire,
            ctx,
            event,
            self._snapshot(ctx, state="ready"),
        )
        if outcome.decision == "deny":
            reason = outcome.reason or DEFAULT_DENY_REASON
            log.info("session %s: %s vetoed: %s", ctx.session_id, event, reason)
            await self._teardown(ctx, self.cfg.limits.shutdown_timeout_seconds)
            ctx.meta.update(state="stopped", error=None)
            self._finish(ctx)
            if not resume:
                self._count_created("vetoed")
            raise (ResumeVetoed if resume else CreateVetoed)(reason)
        ctx.phase = "running"
        ctx.activated = time.time()
        ctx.meta.update(state=self._live_state(ctx), error=None)
        self._save(ctx)
        self._ensure_reaper()

    def _open_session_dir(self, ctx: _Active) -> None:
        ctx.session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if ctx.event_log is None:
            ctx.event_log = EventLog(
                ctx.session_dir / "events.jsonl",
                ctx.session_id,
                ring_size=self.cfg.limits.event_ring_size,
                max_bytes=self.cfg.limits.event_log_max_bytes,
            )

    async def _prepare_workdir(self, ctx: _Active, requested: str | None) -> Path:
        mounted = self.cfg.init.workdir.mode == "mounted"
        return await prepare_workdir(
            self.cfg.init,
            self.cfg.paths.workdir_root,
            session_id=ctx.session_id,
            name=None if mounted else requested,
            path_override=requested if mounted else None,
            env=self._tool_env(ctx, None),
        )

    async def _install_plugins(self, ctx: _Active) -> None:
        plugins = self.cfg.plugins
        if not (plugins.marketplaces or plugins.install):
            return
        assert ctx.workdir is not None
        report = await install_plugins(
            plugins,
            claude_binary=self.cfg.claude.binary,
            config_dir=ctx.config_dir,
            workdir=ctx.workdir,
            env=self._tool_env(ctx, ctx.session_dir / "gitconfig"),
        )
        for warning in report.warnings:
            log.warning("session %s: %s", ctx.session_id, self.redactor.redact(warning))

    def _tool_env(self, ctx: _Active, gitconfig: Path | None) -> dict[str, str]:
        """Env for git, setup commands, mise and plugin installs: what the child gets from the
        service and config (base vars, passthrough, claude env, mise), without the CCW_* relay
        plumbing that only the claude child needs."""
        env = {n: self.service_env[n] for n in BASE_ENV if n in self.service_env}
        for name in self.cfg.claude.passthrough_env:
            if name in self.service_env and not name.startswith("CCW_"):
                env[name] = self.service_env[name]
        env.update(ctx.claude_env.to_env())
        env.update(mise_env(self.cfg.mise))
        env["CLAUDE_CONFIG_DIR"] = str(ctx.config_dir)
        if gitconfig is not None and gitconfig.is_file():
            env["GIT_CONFIG_GLOBAL"] = str(gitconfig)
        return env

    def _gate_fail(self) -> str:
        return gate_fail_mode(self.cfg.hooks)

    def _child_env(self, ctx: _Active) -> dict[str, str]:
        return build_env(
            service_env=self.service_env,
            claude_env=ctx.claude_env,
            config_dir=str(ctx.config_dir),
            gitconfig=str(ctx.session_dir / "gitconfig"),
            session_id=ctx.session_id,
            internal_url=self.internal_url,
            hook_token=ctx.hook_token,
            gate_fail=self._gate_fail(),
            gate_timeout=relay_timeouts(self.cfg).attempt_timeout,
            mise_env=mise_env(self.cfg.mise),
            passthrough=self.cfg.claude.passthrough_env,
        )

    def _wire(self, ctx: _Active, env: dict[str, str], resume: bool) -> None:
        assert ctx.event_log is not None and ctx.workdir is not None
        meta = ctx.meta
        ctx.turns = TurnStore(ctx.session_dir / "turns.jsonl")
        ctx.router = _ManagedRouter(
            None,
            ctx.event_log,
            self._dispatcher(ctx),
            self.redactor,
            self.cfg,
            session_info=SessionInfo(
                session_id=ctx.session_id,
                claude_session_id=str(meta["claude_session_id"]),
                workdir=str(ctx.workdir),
                callback_url=meta.get("callbackUrl"),
            ),
            metrics=self.metrics,
            on_state_change=lambda state: self._on_session_state(ctx, state),
        )
        spec = SessionSpec(
            session_id=ctx.session_id,
            claude_session_id=str(meta["claude_session_id"]),
            workdir=str(ctx.workdir),
            config_dir=str(ctx.config_dir),
            session_dir=str(ctx.session_dir),
            binary=self.cfg.claude.binary,
            model=meta.get("model") or "",
            effort=meta.get("effort") or "",
            extra_args=list(self.cfg.claude.extra_args),
            env=env,
            boot=self.cfg.claude.boot,
            max_restarts=self.cfg.sessions.max_restarts,
            resume=resume,
            sessions=self.cfg.sessions,
            limits=self.cfg.limits,
        )
        ctx.session = Session(spec, ctx.router, turn_store=ctx.turns)

    async def _step(self, ctx: _Active, name: str, fn: Callable[..., Any], *args: Any) -> Any:
        """Record `name` as the running step, run it; any failure becomes `_StepFailed`."""
        try:
            self._set_step(ctx, name)
            result = fn(*args)
            if inspect.isawaitable(result):
                result = await result
            return result
        except (CreateVetoed, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise _StepFailed(name, exc) from exc

    async def _fail(self, ctx: _Active, exc: _StepFailed, kind: str) -> CreateFailed:
        """A create/resume step failed: record it, report it, stop claude, free the slot."""
        cause = exc.cause
        message = self.redactor.redact(str(cause) or type(cause).__name__)
        log.error("session %s: step %s failed: %s", ctx.session_id, exc.step, message)
        ctx.meta.update(state="failed", step=exc.step, error={"step": exc.step, "message": message})
        with contextlib.suppress(Exception):
            self._save(ctx)
        data = {"kind": kind, "step": exc.step, "message": message}
        self._emit(ctx, "error", data)
        await self._fire(ctx, "onError", data)
        await self._teardown(ctx, self.cfg.limits.shutdown_timeout_seconds)
        self._finish(ctx)
        return CreateFailed(exc.step, message)

    async def _abandon(self, ctx: _Active, close_timeout: float = 0.0) -> None:
        """A create/resume was cancelled, or a session outlived the shutdown bound: kill
        claude and record it (a failed or dead state is kept)."""
        await self._teardown(ctx, 0, close_timeout=close_timeout)
        if ctx.meta.get("state") in ("failed", "dead"):
            pass
        elif ctx.meta.get("claude_started"):
            ctx.meta.update(state="stopped")
        else:
            step = ctx.meta.get("step") or "session_dir"
            ctx.meta.update(state="failed", error={"step": step, "message": "cancelled"})
        self._finish(ctx)

    # ------------------------------------------------------------------ destroy

    async def destroy(self, session_id: str, purge: bool = False) -> dict[str, Any]:
        """Stop the session and fire onDestroy; files stay unless `purge` (D19)."""
        ctx = self._active.get(session_id)
        if ctx is not None:
            if ctx.phase == "creating":
                raise InvalidState(session_id, "creating")
            if ctx.phase == "closing":
                await ctx.closed.wait()
            else:
                # Shielded: a caller that goes away never leaves a session half destroyed.
                await asyncio.shield(
                    self._start_destroy(
                        ctx,
                        grace=self.cfg.limits.shutdown_timeout_seconds,
                        hook_timeout=self._hook_bound("onDestroy"),
                    )
                )
        snapshot = self._persisted_snapshot(self._read_meta(session_id))
        if purge:
            self._purge(session_id)
            snapshot["purged"] = True
        return snapshot

    def _start_destroy(
        self,
        ctx: _Active,
        *,
        grace: float,
        hook_timeout: float,
        close_timeout: float | None = None,
    ) -> asyncio.Task[None]:
        """Run the destroy as a tracked task (shutdown awaits, and past its bound cancels)."""
        ctx.phase = "closing"
        return self._spawn(
            self._destroy_active(
                ctx, grace=grace, hook_timeout=hook_timeout, close_timeout=close_timeout
            )
        )

    async def _destroy_active(
        self,
        ctx: _Active,
        *,
        grace: float,
        hook_timeout: float,
        close_timeout: float | None = None,
    ) -> None:
        try:
            await self._teardown(ctx, grace, close_timeout=close_timeout)
            ctx.meta.update(state="stopped")
            self._save(ctx)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + max(hook_timeout, 0.0)
            try:
                await asyncio.wait_for(
                    self._fire(ctx, "onDestroy", self._snapshot(ctx, state="stopped")),
                    max(hook_timeout, 0.0),
                )
            except TimeoutError:
                log.warning("session %s: onDestroy exceeded %gs", ctx.session_id, hook_timeout)
            if ctx.dispatcher is not None:
                # Awaited: non-blocking actions (and other hooks still running) finish too.
                await ctx.dispatcher.drain(max(deadline - loop.time(), 0.0))
        finally:
            # Cancelled by shutdown before claude was down: its kill path finishes the ctx.
            if self._active.get(ctx.session_id) is not ctx:
                self._finish(ctx)

    async def _teardown(
        self, ctx: _Active, grace: float, *, close_timeout: float | None = None
    ) -> None:
        """Stop claude (if started) and close the router (its drain bounded by
        `close_timeout`, else the router default); the slot is free afterwards."""
        ctx.phase = "closing"
        for task in list(ctx.background):
            task.cancel()
        if ctx.session is not None:
            try:
                await ctx.session.stop(grace)
            except Exception:
                log.exception("session %s: stopping claude failed", ctx.session_id)
            ctx.meta["transcript_path"] = ctx.session.transcript_path
        if ctx.router is not None:
            try:
                if close_timeout is None:
                    await ctx.router.close()
                else:
                    await ctx.router.close(drain_timeout=close_timeout)
            except Exception:
                log.exception("session %s: closing the router failed", ctx.session_id)
        if self._active.get(ctx.session_id) is ctx:
            del self._active[ctx.session_id]
            if self.metrics is not None:
                self.metrics.session_closed()

    def _finish(self, ctx: _Active) -> None:
        """Persist the final metadata and close the event log."""
        with contextlib.suppress(Exception):
            self._save(ctx)
        if ctx.event_log is not None:
            ctx.event_log.close()
        ctx.closed.set()

    def _purge(self, session_id: str) -> None:
        meta = self._read_meta(session_id)
        workdir = meta.get("workdir")
        delete_workdir = (
            meta.get("workdir_mode") in ("empty", "clone")
            and bool(workdir)
            and self._owns_workdir(session_id, Path(str(workdir)))
        )
        shutil.rmtree(self._session_dir(session_id), ignore_errors=True)
        if delete_workdir:
            shutil.rmtree(str(workdir), ignore_errors=True)
            log.info("session %s: purged workdir %s", session_id, workdir)

    def _owns_workdir(self, session_id: str, workdir: Path) -> bool:
        """Only a workdir under workdir_root that no other session uses may be deleted."""
        root = Path(self.cfg.paths.workdir_root).resolve()
        target = workdir.resolve()
        if target == root or not target.is_relative_to(root):
            return False
        for meta in self._scan():
            other = meta.get("workdir")
            if meta["session_id"] != session_id and other and Path(other).resolve() == target:
                return False
        return True

    # ------------------------------------------------------------------ resume

    async def resume(
        self, session_id: str, env: Any | Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Relaunch a stopped, dead or failed-after-start session with `--resume`.

        `env` is the resume request's override, applied over the persisted non-secret one;
        secret overrides are never persisted and must be resent here (D24).
        """
        self._check_open()
        ctx = self._active.get(session_id)
        if ctx is not None:
            if ctx.phase == "running" and ctx.session is not None and ctx.session.state == "dead":
                await self._retire(ctx)
            else:
                raise InvalidState(session_id, self._snapshot(ctx)["state"])
        meta = self._read_meta(session_id)
        self._check_resumable(meta)
        resend = ClaudeEnv.model_validate(env) if isinstance(env, Mapping) else env
        try:
            persisted = ClaudeEnv.model_validate(meta.get("env") or {})
        except ValidationError as exc:
            raise ResumeUnavailable(
                session_id, f"persisted env no longer validates: {exc}"
            ) from exc
        override = persisted.merged(resend) if resend is not None else persisted
        claude_env = self._merge_env(override)
        self._check_capacity()
        meta.update(state="creating", error=None, env=_persistable_env(override))
        ctx = self._activate(meta, override, claude_env)
        ctx.workdir = Path(str(meta["workdir"]))
        try:
            await self._step(ctx, "session_dir", self._open_session_dir, ctx)
            await self._bootstrap(ctx)
            await self._launch(ctx, resume=True)
        except _StepFailed as exc:
            raise await self._fail(ctx, exc, "resume_failed") from exc.cause
        except asyncio.CancelledError:
            await asyncio.shield(self._abandon(ctx))
            raise
        finally:
            ctx.task = None
        self._set_step(ctx, "ready")
        return self._snapshot(ctx)

    def _check_resumable(self, meta: dict[str, Any]) -> None:
        session_id = str(meta["session_id"])
        state = meta.get("state")
        if state not in ("stopped", "dead") and not (
            state == "failed" and meta.get("claude_started")
        ):
            raise ResumeUnavailable(session_id, f"state is {state}")
        config_dir = self._session_dir(session_id) / "config"
        if not config_dir.is_dir():
            raise ResumeUnavailable(session_id, "config dir is missing")
        transcript = meta.get("transcript_path") or str(
            config_dir / "projects" / PROJECT_DIR_NAME / f"{meta['claude_session_id']}.jsonl"
        )
        if not Path(transcript).is_file():
            raise ResumeUnavailable(session_id, "no transcript (claude never took a turn)")
        workdir = meta.get("workdir")
        if not workdir or not Path(str(workdir)).is_dir():
            raise ResumeUnavailable(session_id, f"workdir {workdir!r} is missing")

    async def _retire(self, ctx: _Active) -> None:
        """A running session's claude is dead for good: release it, keep state `dead`."""
        if ctx.phase != "running":
            return
        await self._teardown(ctx, 0)
        ctx.meta.update(state="dead")
        self._finish(ctx)

    # ------------------------------------------------------------------ service lifecycle

    async def load_persisted(self) -> None:
        """After a service start: sessions persisted as live (creating, ready, busy) are
        stopped (their claude died with the old process), and resumed when
        `sessions.resume_on_start`. A create cut short before claude took a turn has no
        transcript, so its resume is refused as unavailable and it stays stopped."""
        to_resume: list[str] = []
        for meta in self._scan():
            session_id = meta["session_id"]
            if session_id in self._active or meta.get("state") not in LIVE_STATES:
                continue
            to_resume.append(session_id)
            meta.update(state="stopped", updated_at=now_iso())
            self._write_meta(session_id, meta)
        if self.cfg.sessions.resume_on_start:
            for session_id in to_resume:
                try:
                    await self.resume(session_id)
                except ManagerError as exc:
                    log.warning("session %s: not resumed on start: %s", session_id, exc)
                except Exception:
                    log.exception("session %s: resume on start failed", session_id)
        self._ensure_reaper()

    def _ensure_reaper(self) -> None:
        idle = self.cfg.sessions.idle_timeout_seconds
        if self._reaper is None and idle > 0 and not self._closing:
            self._reaper = asyncio.ensure_future(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reaper_interval)
            try:
                await self.reap_idle()
            except Exception:
                log.exception("idle reaper failed")

    async def reap_idle(self) -> list[str]:
        """Destroy (without purge) ready sessions idle longer than `idle_timeout_seconds`."""
        limit = self.cfg.sessions.idle_timeout_seconds
        if limit <= 0:
            return []
        now = time.time()
        reaped: list[str] = []
        for ctx in list(self._active.values()):
            if ctx.phase != "running" or ctx.session is None or ctx.session.state != "ready":
                continue
            idle = now - self._last_activity(ctx)
            if idle > limit:
                log.info("session %s: idle for %.0fs, stopping", ctx.session_id, idle)
                await self.destroy(ctx.session_id)
                reaped.append(ctx.session_id)
        return reaped

    async def shutdown(self) -> None:
        """Stop every active session concurrently within `shutdown_timeout_seconds`, then
        drain the hook dispatchers.

        The bound is split: claude stops (SIGKILL after 30% of it), the router drains (15%)
        and onDestroy runs (25%) inside the first 75%, destroys already in flight are
        hurried to the same grace, and creates are cancelled. Whatever is still running
        then is cancelled and killed outright, and the dispatchers drain in the rest."""
        self._closing = True
        budget = self.cfg.limits.shutdown_timeout_seconds
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        grace = budget * SHUTDOWN_GRACE_SHARE
        if self._reaper is not None:
            self._reaper.cancel()  # a reap in progress carries on in its destroy task
            await asyncio.gather(self._reaper, return_exceptions=True)
            self._reaper = None
        contexts = list(self._active.values())
        for ctx in contexts:
            if ctx.phase == "running":
                self._start_destroy(
                    ctx,
                    grace=grace,
                    hook_timeout=min(self._hook_bound("onDestroy"), budget * SHUTDOWN_HOOK_SHARE),
                    close_timeout=budget * SHUTDOWN_CLOSE_SHARE,
                )
                continue
            if ctx.phase == "creating" and ctx.task is not None and not ctx.task.done():
                ctx.task.cancel()
            elif ctx.session is not None:  # already closing: hurry claude to the same grace
                self._spawn(ctx.session.stop(grace))
        waits: list[asyncio.Future[Any]] = [
            asyncio.ensure_future(ctx.closed.wait()) for ctx in contexts
        ]
        waits.extend(self._tasks)
        if waits:
            await asyncio.wait(waits, timeout=budget * SHUTDOWN_STOP_SHARE)
        # Past the stop share: cancel what is left, then kill whatever claude still runs.
        late = [
            task
            for task in (*waits, *(ctx.task for ctx in contexts if ctx.task is not None))
            if not task.done()
        ]
        for task in late:
            task.cancel()
        await asyncio.gather(*late, return_exceptions=True)
        leftovers = list(self._active.values())
        for ctx in leftovers:
            log.warning(
                "session %s: not stopped within the shutdown bound, killing", ctx.session_id
            )
        close_timeout = budget * SHUTDOWN_KILL_CLOSE_SHARE
        await asyncio.gather(
            *(self._abandon(ctx, close_timeout) for ctx in leftovers), return_exceptions=True
        )
        remaining = max(deadline - loop.time(), 0.0)
        await asyncio.gather(
            *(d.drain(remaining) for d in self._dispatchers), return_exceptions=True
        )

    # ------------------------------------------------------------------ internals

    def _check_open(self) -> None:
        if self._closing:
            raise InvalidState(None, "shutting_down", "the service is shutting down")

    def _check_capacity(self) -> None:
        live = sum(ctx.phase in ("creating", "running") for ctx in self._active.values())
        if live >= self.cfg.sessions.max_sessions:
            raise TooManySessions(self.cfg.sessions.max_sessions)

    def _merge_env(self, override: Any) -> Any:
        try:
            merged = self.cfg.claude.env.merged(override)
        except ValidationError as exc:
            raise InvalidRequest(f"env: {exc}") from exc
        for warning in merged.warnings():
            log.warning("%s", warning)
        return merged

    def _activate(self, meta: dict[str, Any], override: Any, claude_env: Any) -> _Active:
        """Register a session as active (no await since the capacity check)."""
        ctx = _Active(
            meta=meta,
            session_dir=self._session_dir(str(meta["session_id"])),
            override=override,
            claude_env=claude_env,
            hook_token=secrets.token_urlsafe(32),
        )
        self.redactor.add([*claude_env.secret_values(), ctx.hook_token])
        ctx.task = asyncio.current_task()
        self._active[ctx.session_id] = ctx
        if self.metrics is not None:
            self.metrics.session_opened()
        return ctx

    def _new_session_id(self) -> str:
        while True:
            session_id = "ses_" + secrets.token_hex(6)
            if session_id not in self._active and not self._session_dir(session_id).exists():
                return session_id

    def _session_dir(self, session_id: str) -> Path:
        return self._root / session_id

    def _read_meta(self, session_id: str) -> dict[str, Any]:
        if not SESSION_ID_RE.match(session_id or ""):
            raise SessionNotFound(session_id)
        try:
            meta = json.loads((self._session_dir(session_id) / META_FILE).read_text())
        except (OSError, ValueError) as exc:
            raise SessionNotFound(session_id) from exc
        if not isinstance(meta, dict) or meta.get("session_id") != session_id:
            raise SessionNotFound(session_id)
        return meta

    def _scan(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self._root.glob(f"*/{META_FILE}")):
            with contextlib.suppress(SessionNotFound):
                out.append(self._read_meta(path.parent.name))
        return out

    def _write_meta(self, session_id: str, meta: dict[str, Any]) -> None:
        path = self._session_dir(session_id) / META_FILE
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(path, json.dumps(meta, indent=2, sort_keys=True) + "\n")

    def _save(self, ctx: _Active) -> None:
        ctx.meta["updated_at"] = now_iso()
        ctx.meta["last_activity_at"] = self._last_activity_iso(ctx)
        self._write_meta(ctx.session_id, ctx.meta)

    def _set_step(self, ctx: _Active, step: str) -> None:
        ctx.meta["step"] = step
        self._save(ctx)

    def _on_session_state(self, ctx: _Active, state: str) -> None:
        if ctx.phase != "running":
            return
        ctx.meta["state"] = state
        with contextlib.suppress(Exception):
            self._save(ctx)
        if state == "dead":  # out of restarts: release the slot, keep the files
            self._spawn(self._retire(ctx))

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        """A manager-level task (destroy, release, hurry) that shutdown waits for."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _track(self, ctx: _Active, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        ctx.background.add(task)
        task.add_done_callback(ctx.background.discard)

    def _dispatcher(self, ctx: _Active) -> HookDispatcher:
        """The session's dispatcher; hooks run in the workdir once it exists."""
        cwd = ctx.workdir if ctx.workdir is not None and ctx.workdir.is_dir() else None
        if cwd is None:
            cwd = ctx.session_dir if ctx.session_dir.is_dir() else Path(self._root)
        if ctx.dispatcher is None or ctx.dispatcher_cwd != cwd:
            state_dir = ctx.session_dir / "hook-state"
            state_dir.mkdir(parents=True, exist_ok=True)
            ctx.dispatcher = HookDispatcher(
                self.cfg.hooks,
                self.redactor,
                base_env=self.service_env,
                default_cwd=cwd,
                config_dir=ctx.config_dir,
                state_dir=state_dir,
                api_url=self.api_url,
            )
            ctx.dispatcher_cwd = cwd
            self._dispatchers.add(ctx.dispatcher)
        return ctx.dispatcher

    async def _fire(self, ctx: _Active, event: str, data: Mapping[str, Any]) -> HookOutcome:
        payload = build_payload(
            event,
            session_id=ctx.session_id,
            claude_session_id=str(ctx.meta.get("claude_session_id") or ""),
            turn_id=None,
            workdir=str(ctx.workdir or ""),
            data=data,
        )
        try:
            return await self._dispatcher(ctx).fire(event, payload)
        except Exception:  # noqa: BLE001 - user hooks never break the manager
            log.exception("session %s: firing %s failed", ctx.session_id, event)
            return HookOutcome()

    def _emit(self, ctx: _Active, type: str, data: dict[str, Any]) -> None:
        if ctx.event_log is None:
            return
        try:
            ctx.event_log.append(type, self.redactor.redact_obj(data))
        except Exception:
            log.exception("session %s: emitting %s failed", ctx.session_id, type)

    def _hook_bound(self, event: str) -> float:
        """How long the awaited actions of `event` can take (timeouts and webhook retries)."""
        bound = 0.0
        for action in self.cfg.hooks.for_event(event):
            retries = getattr(action, "retries", 0)
            bound = max(
                bound,
                action.timeout_seconds * (retries + 1) + RETRY_BASE_SECONDS * (2**retries - 1),
            )
        return bound + 1.0

    def _last_activity(self, ctx: _Active) -> float:
        last = ctx.event_log.last(1) if ctx.event_log is not None else []
        stamp = _epoch(last[0].ts) if last else None
        return max(stamp or 0.0, ctx.activated)

    def _last_activity_iso(self, ctx: _Active) -> str:
        last = ctx.event_log.last(1) if ctx.event_log is not None else []
        if last:
            return last[0].ts
        return str(ctx.meta.get("last_activity_at") or ctx.meta.get("created_at") or now_iso())

    def _live_state(self, ctx: _Active) -> str:
        if ctx.session is None:
            return str(ctx.meta.get("state") or "creating")
        return str(ctx.session.snapshot()["state"])

    # ------------------------------------------------------------------ snapshots

    def _snapshot(self, ctx: _Active, state: str | None = None) -> dict[str, Any]:
        session = ctx.session
        live = session.snapshot() if session is not None else {}
        if state is None:
            if ctx.phase == "creating":
                state = "creating"
            elif ctx.phase == "closing":
                state = "dead" if ctx.meta.get("state") == "dead" else "stopped"
            else:
                state = str(live.get("state") or ctx.meta.get("state"))
        current = session.current_turn if session is not None else None
        turns = ctx.turns.list() if ctx.turns is not None else []
        return self._compose(
            ctx.meta,
            state=state,
            env=ctx.claude_env.redacted(),
            turns_completed=sum(t.state == "completed" for t in turns),
            current=current,
            live=live,
            last_activity_at=self._last_activity_iso(ctx),
        )

    def _persisted_snapshot(self, meta: dict[str, Any]) -> dict[str, Any]:
        session_id = str(meta["session_id"])
        try:
            env = self.cfg.claude.env.merged(
                ClaudeEnv.model_validate(meta.get("env") or {})
            ).redacted()
        except ValidationError:
            env = dict(meta.get("env") or {})
        turns = TurnStore(self._session_dir(session_id) / "turns.jsonl", readonly=True).list()
        return self._compose(
            meta,
            state=str(meta.get("state")),
            env=env,
            turns_completed=sum(t.state == "completed" for t in turns),
            current=None,
            live={},
            last_activity_at=meta.get("last_activity_at"),
        )

    @staticmethod
    def _compose(
        meta: dict[str, Any],
        *,
        state: str,
        env: dict[str, Any],
        turns_completed: int,
        current: Any,
        live: dict[str, Any],
        last_activity_at: str | None,
    ) -> dict[str, Any]:
        return {
            "session_id": meta["session_id"],
            "claude_session_id": meta.get("claude_session_id"),
            "name": meta.get("name"),
            "state": state,
            "step": meta.get("step"),
            "error": meta.get("error"),
            "workdir": meta.get("workdir"),
            "workdir_mode": meta.get("workdir_mode"),
            "model": meta.get("model"),
            "effort": meta.get("effort"),
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "last_activity_at": last_activity_at,
            "turns_completed": turns_completed,
            "current_turn_id": current.turn_id if current is not None else None,
            "stall_suspected_since": (
                current.stall_suspected_since if current is not None else None
            ),
            "last_fatal": live.get("last_fatal"),
            "restarts": live.get("restarts", 0),
            "pid": live.get("pid"),
            "env": env,
            "callbackUrl": meta.get("callbackUrl"),
        }
