"""config.yaml models, loader and redaction (design.md "Config schema", D8)."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from claude_code_wrapper.claude_env import SECRET_KEY_RE, ClaudeEnv
from claude_code_wrapper.interpolation import InterpolationError, interpolate

MASK = "***"


@dataclass(frozen=True)
class HookEventSpec:
    """One hook event and what its actions may carry (the hooks editor reads this)."""

    name: str
    #: A blocking action may answer `deny` (the event can be vetoed).
    can_veto: bool = False
    #: Actions may set `matcher` (a tool-name regex).
    supports_matcher: bool = False
    #: Actions may set `when` (onTick: busy or always).
    supports_when: bool = False
    #: Actions may set `interval_seconds`.
    supports_interval: bool = False
    #: `interval_seconds` is required on every action.
    interval_required: bool = False


_TICK = {"supports_when": True, "supports_interval": True, "interval_required": True}
#: Every hook event, in config order. The orchestrator keeps an equal copy (no import).
HOOK_EVENT_SPECS: tuple[HookEventSpec, ...] = (
    HookEventSpec("onCreate", can_veto=True),
    HookEventSpec("onResume", can_veto=True),
    HookEventSpec("onDestroy"),
    HookEventSpec("onTurn", can_veto=True),
    HookEventSpec("onTurnComplete"),
    HookEventSpec("onStop"),
    HookEventSpec("onTick", **_TICK),
    HookEventSpec("onAgentResponse"),
    HookEventSpec("onAgentToolCall", can_veto=True, supports_matcher=True),
    HookEventSpec("onToolResult", supports_matcher=True),
    HookEventSpec("onSubagentStart"),
    HookEventSpec("onSubagentStop"),
    HookEventSpec("onInterrupt"),
    HookEventSpec("onCommand", can_veto=True),
    HookEventSpec("onCommandComplete"),
    HookEventSpec("onError"),
    HookEventSpec("preJoin", can_veto=True),
    HookEventSpec("postJoin"),
    HookEventSpec("preLeave"),
    HookEventSpec("postLeave"),
    HookEventSpec("onStart", can_veto=True),
)
HOOK_EVENTS: tuple[str, ...] = tuple(s.name for s in HOOK_EVENT_SPECS)
VETO_EVENTS: tuple[str, ...] = tuple(s.name for s in HOOK_EVENT_SPECS if s.can_veto)
TOOL_EVENTS: tuple[str, ...] = tuple(s.name for s in HOOK_EVENT_SPECS if s.supports_matcher)


def hook_events_meta() -> list[dict[str, Any]]:
    """`HOOK_EVENT_SPECS` as plain JSON objects, one per event, in config order."""
    return [asdict(spec) for spec in HOOK_EVENT_SPECS]


WELL_KNOWN_MARKETPLACES = frozenset({"claude-plugins-official"})
_PLUGIN_ID = re.compile(r"^[^@\s]+@[^@\s]+$")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class ConfigError(Exception):
    """Invalid config; the message lists every problem as `<dotted.path>: <message>`."""


def _at(sub: str, message: str) -> PydanticCustomError:
    """A validation error reported at a path below the validated model."""
    return PydanticCustomError("config_path", "{message}", {"message": message, "sub": sub})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _absolute(value: str) -> str:
    if not value.startswith("/"):
        raise ValueError(f"must be an absolute path, got {value!r}")
    return value


class ServerConfig(_Model):
    host: str = "0.0.0.0"
    port: int = Field(8080, ge=0, le=65535)
    internal_port: int = Field(8090, ge=0, le=65535, validate_default=True)
    auth_token: str = ""
    allow_anonymous: bool = False
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    callback_secret: str = ""

    def require_auth_policy(self) -> None:
        """Refuse an unauthenticated public API unless `allow_anonymous` is set."""
        if not self.auth_token and not self.allow_anonymous:
            raise ConfigError(
                "server.auth_token: empty; set server.auth_token, or set "
                "server.allow_anonymous: true to run /v1 without authentication"
            )

    @field_validator("internal_port")
    @classmethod
    def _distinct(cls, value: int, info: ValidationInfo) -> int:
        port = info.data.get("port")
        if value != 0 and value == port:
            raise ValueError("internal_port must differ from port")
        return value


class PathsConfig(_Model):
    workdir_root: str = "/workdir"
    sessions_root: str = "/sessions"

    _abs = field_validator("workdir_root", "sessions_root")(_absolute)


class BootDialog(_Model):
    pattern: str
    keys: list[str] = Field(default_factory=list)
    delay_seconds: float = Field(0.0, ge=0)


class BootConfig(_Model):
    timeout_seconds: float = Field(60, gt=0)
    min_boot_seconds: float = Field(4.0, ge=0)
    idle_ready_seconds: float = Field(1.5, ge=0)
    submit_delay_seconds: float = Field(0.4, ge=0)
    dialogs: list[BootDialog] = Field(
        default_factory=lambda: [
            BootDialog(pattern="Bypass Permissions mode", keys=["\x1b[B", "\r"], delay_seconds=0.6),
            BootDialog(pattern="Do you trust the files in this folder", keys=["\r"]),
        ]
    )
    ready_markers: list[str] = Field(
        default_factory=lambda: ["? for shortcuts", "bypass permissions on", "auto mode on"]
    )
    fatal_patterns: list[str] = Field(
        default_factory=lambda: [
            "API Error: 401",
            "API Error: 403",
            "Please run /login",
            "Credit balance is too low",
            "OAuth token has expired",
        ]
    )


class PermissionsConfig(_Model):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=lambda: ["EnterPlanMode", "ExitPlanMode"])


class McpStdio(_Model):
    type: Literal["stdio"]
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class McpHttp(_Model):
    type: Literal["http", "sse"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)


McpServer = Annotated[McpStdio | McpHttp, Field(discriminator="type")]


class ConfigFile(_Model):
    source: str
    target: str

    @field_validator("target")
    @classmethod
    def _relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError("must be a relative path without '..'")
        return value


PermissionMode = Literal["default", "acceptEdits", "plan", "auto", "bypassPermissions", "dontAsk"]


class ClaudeConfig(_Model):
    binary: str = "claude"
    model: str = ""
    effort: str = ""
    permission_mode: PermissionMode = "bypassPermissions"
    extra_args: list[str] = Field(default_factory=list)
    env: ClaudeEnv = Field(default_factory=ClaudeEnv)  # type: ignore[valid-type]
    passthrough_env: list[str] = Field(default_factory=list)
    passthrough_env_exclude: list[str] = Field(default_factory=list)
    main_agent_only_tools: list[str] = Field(default_factory=list)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    settings: dict[str, Any] = Field(default_factory=dict)
    claude_json: dict[str, Any] = Field(default_factory=dict)
    mcp_servers: dict[str, McpServer] = Field(default_factory=dict)
    config_files: list[ConfigFile] = Field(default_factory=list)
    boot: BootConfig = Field(default_factory=BootConfig)

    @field_validator("env", mode="before")
    @classmethod
    def _drop_empty_env(cls, value: Any) -> Any:
        """Empty strings (e.g. from ${VAR:-}) mean unset, never an exported empty var."""
        if not isinstance(value, dict):
            return value
        out = {k: v for k, v in value.items() if v != ""}
        if isinstance(out.get("extra"), dict):
            out["extra"] = {k: v for k, v in out["extra"].items() if v != ""}
        return out

    @field_validator("extra_args")
    @classmethod
    def _no_skip_permissions(cls, value: list[str]) -> list[str]:
        if any(a.split("=", 1)[0] == "--dangerously-skip-permissions" for a in value):
            raise ValueError(
                "--dangerously-skip-permissions is not allowed (it adds a boot dialog); "
                "use claude.permission_mode"
            )
        return value


class WorkdirConfig(_Model):
    mode: Literal["empty", "mounted", "clone"] = "empty"
    path: str = Field("", validate_default=True)
    name: str = ""
    repo_url: str = Field("", validate_default=True)
    branch: str = ""
    depth: int = Field(0, ge=0)
    submodules: bool = False

    @field_validator("path")
    @classmethod
    def _mounted_path(cls, value: str, info: ValidationInfo) -> str:
        if info.data.get("mode") == "mounted":
            if not value:
                raise ValueError("required when mode is mounted")
            _absolute(value)
        return value

    @field_validator("repo_url")
    @classmethod
    def _clone_url(cls, value: str, info: ValidationInfo) -> str:
        if info.data.get("mode") == "clone" and not value:
            raise ValueError("required when mode is clone")
        return value


class GitConfig(_Model):
    user_name: str = ""
    user_email: str = ""


class SetupCommand(_Model):
    run: str
    timeout_seconds: float = Field(600, gt=0)


class InitConfig(_Model):
    workdir: WorkdirConfig = Field(default_factory=WorkdirConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    setup_commands: list[SetupCommand] = Field(default_factory=list)

    @field_validator("setup_commands", mode="before")
    @classmethod
    def _strings(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [{"run": v} if isinstance(v, str) else v for v in value]
        return value


class TemplateVariable(_Model):
    required: bool = False
    default: str | None = None


class PromptTemplate(_Model):
    description: str = ""
    variables: dict[str, TemplateVariable] = Field(default_factory=dict)
    text: str = ""

    @field_validator("text")
    @classmethod
    def _declared(cls, value: str, info: ValidationInfo) -> str:
        declared = info.data.get("variables", {})
        missing = sorted({n for n in _PLACEHOLDER.findall(value) if n not in declared})
        if missing:
            raise ValueError(f"placeholders not declared in variables: {', '.join(missing)}")
        return value


class InitialPromptTemplate(_Model):
    template: str
    variables: dict[str, str] = Field(default_factory=dict)


def _prompt_kind(value: Any) -> str:
    return "text" if isinstance(value, str) else "template"


InitialPrompt = Annotated[
    Annotated[str, Tag("text")] | Annotated[InitialPromptTemplate, Tag("template")],
    Discriminator(_prompt_kind),
]


class PromptsConfig(_Model):
    global_claude_md: str = ""
    system_append: str = ""
    initial_prompt: InitialPrompt = ""
    templates: dict[str, PromptTemplate] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known_template(self) -> PromptsConfig:
        ip = self.initial_prompt
        if isinstance(ip, InitialPromptTemplate) and ip.template not in self.templates:
            raise _at("initial_prompt.template", f"unknown template {ip.template!r}")
        return self


class MiseConfig(_Model):
    tools: dict[str, str] = Field(default_factory=dict)
    config_file: str = ""
    install_on_start: bool = True
    install_workdir: bool = True
    trusted_paths: list[str] = Field(default_factory=lambda: ["/workdir"])
    required: bool = True
    install_timeout_seconds: float = Field(900, gt=0)
    global_config_path: str = "/home/agent/.config/mise/config.toml"

    @field_validator("trusted_paths")
    @classmethod
    def _abs_all(cls, value: list[str]) -> list[str]:
        for v in value:
            _absolute(v)
        return value

    _abs_global = field_validator("global_config_path")(_absolute)


class Marketplace(_Model):
    name: str
    source: str


class PluginInstall(_Model):
    plugin: str
    scope: Literal["user", "project", "local"] = "user"
    required: bool = True

    @field_validator("plugin")
    @classmethod
    def _shape(cls, value: str) -> str:
        if not _PLUGIN_ID.match(value):
            raise ValueError("must look like name@marketplace")
        return value


class PluginsConfig(_Model):
    marketplaces: list[Marketplace] = Field(default_factory=list)
    install: list[PluginInstall] = Field(default_factory=list)
    disable: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(180, gt=0)

    @model_validator(mode="after")
    def _marketplaces_known(self) -> PluginsConfig:
        known = {m.name for m in self.marketplaces} | WELL_KNOWN_MARKETPLACES
        for i, p in enumerate(self.install):
            market = p.plugin.rsplit("@", 1)[1]
            if market not in known:
                raise _at(
                    f"install.{i}.plugin",
                    f"marketplace {market!r} is not in plugins.marketplaces or well known",
                )
        return self


class _Action(_Model):
    timeout_seconds: float = Field(30, gt=0)
    blocking: bool = False
    on_error: Literal["allow", "deny"] = "allow"
    matcher: str | None = None
    interval_seconds: float | None = Field(None, gt=0)
    when: Literal["busy", "always"] | None = None

    @field_validator("matcher")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as e:
                raise ValueError(f"invalid regex: {e}") from e
        return value


class CommandAction(_Action):
    type: Literal["command"]
    run: str
    env: dict[str, str] = Field(default_factory=dict)


class WebhookAction(_Action):
    type: Literal["webhook"]
    url: str
    secret: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(10, gt=0)
    retries: int = Field(3, ge=0)

    @field_validator("url")
    @classmethod
    def _http(cls, value: str) -> str:
        if not re.match(r"^https?://[^/\s]+", value):
            raise ValueError("must be an http or https URL")
        return value


HookAction = Annotated[CommandAction | WebhookAction, Field(discriminator="type")]


class HooksConfig(_Model):
    onCreate: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onResume: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onDestroy: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onTurn: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onTurnComplete: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onStop: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onTick: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onAgentResponse: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onAgentToolCall: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onToolResult: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onSubagentStart: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onSubagentStop: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onInterrupt: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onCommand: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onCommandComplete: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onError: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    preJoin: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    postJoin: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    preLeave: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    postLeave: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onStart: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815

    @model_validator(mode="after")
    def _matcher_events(self) -> HooksConfig:
        for event in HOOK_EVENTS:
            if event in TOOL_EVENTS:
                continue
            for i, action in enumerate(getattr(self, event)):
                if action.matcher is not None:
                    raise _at(
                        f"{event}.{i}.{action.type}.matcher",
                        f"matcher is only allowed on {', '.join(TOOL_EVENTS)}",
                    )
        return self

    @model_validator(mode="after")
    def _tick_keys(self) -> HooksConfig:
        for event in HOOK_EVENTS:
            for i, action in enumerate(getattr(self, event)):
                for key in ("interval_seconds", "when"):
                    present = getattr(action, key) is not None
                    if event != "onTick" and present:
                        raise _at(
                            f"{event}.{i}.{action.type}.{key}", f"{key} is only allowed on onTick"
                        )
                if event == "onTick" and action.interval_seconds is None:
                    raise _at(
                        f"{event}.{i}.{action.type}.interval_seconds",
                        "interval_seconds is required on onTick",
                    )
        return self

    def for_event(self, event: str) -> list[CommandAction | WebhookAction]:
        return getattr(self, event)


#: A slash command name as Claude Code parses it (design D2): full match, no length cap.
COMMAND_NAME = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9:_-]*")
#: An allowlist entry that admits every name the deny list does not refuse.
COMMAND_WILDCARD = "*"
#: Commands that end, replace or move the claude session or process, hand it to another
#: controller, or change account identity or credentials (design D5). Aliases are listed by name
#: from claude's own alias table; `sessions.commands.deny` matches them case-insensitively.
#: The second block (user decision 2026-10-29, names checked against claude 2.1.304) holds the
#: commands that install or reconfigure claude or its host (heapdump "Dump the JS heap to the
#: Desktop", plugin/plugins/marketplace "Manage Claude Code plugins", install "Install Claude Code
#: native build", import "Import config from another AI coding agent", daemon "Manage background
#: services and routines", schedule/routines "Create and manage scheduled remote Claude Code
#: agents", add-dir "Add a new working directory", install-github-app, install-slack-app,
#: terminal-setup, ide "Manage IDE integrations", memory "Edit CLAUDE.md files and memory
#: settings"), export or report from the agent's account (export "Export the current conversation
#: to a file or clipboard", feedback and bug "Report a bug or share your conversation"), change
#: account consent or billing (design-consent, design-revoke, extra-usage "Renamed to
#: /usage-credits", usage-credits "Configure usage credits"), or hand the session to another
#: client (desktop/app "Continue the current session in Claude Desktop", remote-env "Choose the
#: default environment for cloud agents"). The third block (0.4.2, names checked against claude
#: 2.1.304) holds the commands that reach the agent account's cloud features (privacy-settings,
#: ultraplan, ultrareview, autofix-pr, cloud-plugins, session with its alias remote) and the
#: commands that rewrite the session's tool and permission settings (mcp, permissions with its
#: alias allowed-tools, config with its alias settings, hooks). A create override only extends
#: this list.
DEFAULT_COMMAND_DENY: tuple[str, ...] = (
    "exit",
    "quit",
    "clear",
    "reset",
    "new",
    "resume",
    "continue",
    "branch",
    "fork",
    "cd",
    "background",
    "bg",
    "teleport",
    "tp",
    "stop",
    "remote-control",
    "rc",
    "restart",
    "update",
    "login",
    "logout",
    "upgrade",
    "setup-bedrock",
    "setup-vertex",
    "web-setup",
    "design-login",
    "heapdump",
    "plugin",
    "plugins",
    "marketplace",
    "install",
    "import",
    "daemon",
    "schedule",
    "routines",
    "add-dir",
    "export",
    "install-github-app",
    "install-slack-app",
    "design-consent",
    "design-revoke",
    "desktop",
    "app",
    "remote-env",
    "feedback",
    "bug",
    "extra-usage",
    "usage-credits",
    "terminal-setup",
    "ide",
    "memory",
    "privacy-settings",
    "ultraplan",
    "ultrareview",
    "autofix-pr",
    "cloud-plugins",
    "session",
    "remote",
    "mcp",
    "permissions",
    "allowed-tools",
    "config",
    "settings",
    "hooks",
)
DEFAULT_CONTINUE_PROMPT = (
    "The slash commands you queued have run. Continue the task you were working on."
)
#: Footers of claude's dialogs (design D7): a command that shows one is dismissed by ESC.
DEFAULT_DIALOG_PATTERNS: tuple[str, ...] = (
    "esc to cancel",
    "esc to close",
    "esc to clear",
    "esc to exit",
    "enter to confirm",
)


def _check_name(path: str, name: str, *, wildcard: bool) -> None:
    if (wildcard and name == COMMAND_WILDCARD) or COMMAND_NAME.fullmatch(name):
        return
    allowed = f" or be {COMMAND_WILDCARD!r}" if wildcard else ""
    raise _at(path, f"invalid command name {name!r}; must match {COMMAND_NAME.pattern}{allowed}")


def _check_command_names(names: list[str]) -> list[str]:
    for i, name in enumerate(names):
        _check_name(str(i), name, wildcard=True)
    return names


def _check_deny_names(names: list[str]) -> list[str]:
    for i, name in enumerate(names):
        _check_name(str(i), name, wildcard=False)
    return names


def _check_command_timeouts(value: dict[str, float]) -> dict[str, float]:
    for key, seconds in value.items():
        _check_name(key, key, wildcard=False)
        if seconds <= 0:
            raise _at(key, "must be greater than 0")
    return value


def _check_dialog_patterns(patterns: list[str]) -> list[str]:
    for i, pattern in enumerate(patterns):
        if not pattern.strip():
            raise _at(str(i), "must not be blank")
    return patterns


class CommandsConfig(_Model):
    agent: list[str] = []
    client: list[str] = []
    deny: list[str] = Field(default_factory=lambda: list(DEFAULT_COMMAND_DENY))
    timeout_seconds: dict[str, float] = {"compact": 600, "default": 60}
    continue_prompt: str = Field(DEFAULT_CONTINUE_PROMPT, min_length=1)
    allow_force: bool = False
    quiet_seconds: float = Field(1.5, ge=0)
    dialog_grace_seconds: float = Field(2.0, ge=0)
    dialog_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_DIALOG_PATTERNS))

    _agent = field_validator("agent")(_check_command_names)
    _client = field_validator("client")(_check_command_names)
    _deny = field_validator("deny")(_check_deny_names)
    _timeouts = field_validator("timeout_seconds")(_check_command_timeouts)
    _dialogs = field_validator("dialog_patterns")(_check_dialog_patterns)


class CommandsOverride(_Model):
    """Per-session `sessions.commands` override: every field optional, unset fields inherit."""

    agent: list[str] | None = None
    client: list[str] | None = None
    deny: list[str] | None = None
    timeout_seconds: dict[str, float] | None = None
    continue_prompt: str | None = Field(None, min_length=1)
    allow_force: bool | None = None
    quiet_seconds: float | None = Field(None, ge=0)
    dialog_grace_seconds: float | None = Field(None, ge=0)
    dialog_patterns: list[str] | None = None

    @field_validator("agent", "client")
    @classmethod
    def _names(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _check_command_names(value)

    @field_validator("deny")
    @classmethod
    def _deny(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _check_deny_names(value)

    @field_validator("timeout_seconds")
    @classmethod
    def _timeouts(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        return None if value is None else _check_command_timeouts(value)

    @field_validator("dialog_patterns")
    @classmethod
    def _dialogs(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _check_dialog_patterns(value)


#: A compact's focus instructions: one line, no control characters, at most this long.
MAX_INSTRUCTIONS = 2000
INSTRUCTIONS_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


class AutoCompactConfig(_Model):
    """`sessions.auto_compact`: compact a session that stayed idle (design D6)."""

    idle_seconds: float = Field(0, ge=0)
    instructions: str = ""
    min_context_tokens: int = Field(0, ge=0)

    @field_validator("instructions")
    @classmethod
    def _one_line(cls, value: str) -> str:
        if INSTRUCTIONS_CONTROL.search(value):
            raise ValueError("must be one line without control characters")
        if len(value) > MAX_INSTRUCTIONS:
            raise ValueError(f"must not exceed {MAX_INSTRUCTIONS} characters")
        return value


class SessionsConfig(_Model):
    max_sessions: int = Field(4, ge=1)
    idle_timeout_seconds: float = Field(0, ge=0)
    turn_stall_seconds: float = Field(1800, ge=0)
    interrupt_settle_seconds: float = Field(20, ge=0)
    max_restarts: int = Field(3, ge=0)
    ask_user_question: Literal["forward", "deny", "allow"] = "forward"
    turn_marker: bool = True
    resume_on_start: bool = False
    defer_stop_on_background_tasks: bool = False
    probe_alive_token: str = Field("CCW-ALIVE", min_length=1)
    exit_when_last_destroyed: bool = False
    exit_when_dead: bool = False
    #: Pasted (with the same turn marker) into a resumed relaunch after a mid-turn crash, so the
    #: running turn continues instead of failing with claude_exited. Empty disables it.
    restart_continue_prompt: str = ""
    commands: CommandsConfig = Field(default_factory=CommandsConfig)
    auto_compact: AutoCompactConfig = Field(default_factory=AutoCompactConfig)

    @model_validator(mode="after")
    def _auto_compact_before_reaper(self) -> SessionsConfig:
        idle, reaper = self.auto_compact.idle_seconds, self.idle_timeout_seconds
        if idle > 0 and reaper > 0 and idle >= reaper:
            raise _at(
                "auto_compact.idle_seconds",
                "must be below idle_timeout_seconds (the reaper would stop the session first)",
            )
        return self


_PEER_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
_PEER_TOKEN_MIN = 16


def _check_http_url(value: str) -> str:
    """An absolute http(s) URL with a host and no trailing slash; `{port}` is allowed."""
    try:
        parts = urlsplit(value.replace("{port}", "1"))
        valid = parts.scheme in ("http", "https") and parts.hostname is not None
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"must be an absolute http:// or https:// URL with a host, got {value!r}")
    if value.endswith("/"):
        raise ValueError(f"must not end with a slash, got {value!r}")
    return value


class PeersConfig(_Model):
    """Opt-in peer messaging between ccw instances (README "Peer messaging")."""

    enabled: bool = False
    name: str = ""
    advertise_url: str = ""
    token: str = ""
    static: list[str] = Field(default_factory=list)
    heartbeat_seconds: float = Field(10, gt=0)
    max_missed_heartbeats: int = Field(3, ge=1)
    join_timeout_seconds: float = Field(10, gt=0)
    max_message_bytes: int = Field(65536, ge=1, le=1048576)
    inbox_max: int = Field(32, ge=1)
    max_clock_skew_seconds: float = Field(60, gt=0)
    max_messages_per_minute: int = Field(30, ge=1)
    max_messages_per_minute_total: int = Field(120, ge=1)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if value and (not _PEER_NAME.fullmatch(value) or value.startswith(".")):
            raise ValueError(
                f"must match [A-Za-z0-9._-]{{1,64}} and not start with '.', got {value!r}"
            )
        return value

    @field_validator("advertise_url")
    @classmethod
    def _advertise_url(cls, value: str) -> str:
        return _check_http_url(value) if value else value

    @field_validator("static", mode="before")
    @classmethod
    def _static_items(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [
            item["url"] if isinstance(item, dict) and set(item) == {"url"} else item
            for item in value
        ]

    @field_validator("static")
    @classmethod
    def _static(cls, value: list[str]) -> list[str]:
        for index, url in enumerate(value):
            try:
                _check_http_url(url)
            except ValueError as e:
                raise _at(str(index), str(e)) from e
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _token(self) -> PeersConfig:
        if self.enabled and not self.token:
            raise _at(
                "token",
                "required when peers.enabled; set it, normally from ${CCW_PEER_TOKEN}",
            )
        if self.token and len(self.token) < _PEER_TOKEN_MIN:
            raise _at("token", f"must be at least {_PEER_TOKEN_MIN} characters")
        return self

    def with_static(self, urls: list[str]) -> PeersConfig:
        """A copy whose seed list is the configured one extended by `urls` (`ccw serve --peer`)."""
        return PeersConfig.model_validate({**self.model_dump(), "static": [*self.static, *urls]})


_USAGE_AGENT = re.compile(r"[A-Za-z0-9._-]{0,128}")
_USAGE_TOKEN_MIN = 16


class UsageConfig(_Model):
    """Forward statusLine rate limits to agents-usage (README "Usage forwarding")."""

    url: str = ""
    token: str = ""
    agent: str = ""
    min_interval_seconds: float = Field(60, gt=0)
    timeout_seconds: float = Field(5, gt=0, le=30)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _check_http_url(value) if value else value

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str) -> str:
        if not _USAGE_AGENT.fullmatch(value):
            raise ValueError(f"must match [A-Za-z0-9._-]{{0,128}}, got {value!r}")
        return value

    @model_validator(mode="after")
    def _token(self) -> UsageConfig:
        if self.url and not self.token:
            raise _at(
                "token",
                "required when usage.url is set; set it, normally from ${CCW_USAGE_TOKEN}",
            )
        if self.token and len(self.token) < _USAGE_TOKEN_MIN:
            raise _at("token", f"must be at least {_USAGE_TOKEN_MIN} characters")
        return self


class LimitsConfig(_Model):
    max_prompt_bytes: int = Field(1048576, ge=1)
    max_file_bytes: int = Field(262144, ge=1)
    max_files_per_turn: int = Field(16, ge=0)
    files_max_entries: int = Field(2000, ge=1)
    file_preview_max_bytes: int = Field(1048576, ge=1)
    turn_max_seconds: float = Field(0, ge=0)
    event_ring_size: int = Field(1000, ge=1)
    event_log_max_bytes: int = Field(52428800, ge=0)
    sse_replay: int = Field(100, ge=0)
    sse_heartbeat_seconds: float = Field(15, gt=0)
    shutdown_timeout_seconds: float = Field(10, ge=0)
    terminals_max: int = Field(8, ge=0)
    terminal_scrollback_bytes: int = Field(262144, ge=1)
    terminal_idle_reap_seconds: float = Field(900, gt=0)
    terminal_max_frame_bytes: int = Field(65536, ge=1)
    terminal_max_subscribers: int = Field(8, ge=1)


class NotesConfig(_Model):
    """Client-pushed notes inbox for hived agents (README "Notes")."""

    enabled: bool = False
    max_notes: int = Field(50, ge=1)
    max_keyed_notes: int = Field(32, ge=1)
    max_note_chars: int = Field(2000, ge=1)
    delivery_max_chars: int = Field(8000, ge=1)
    throttle_seconds: int = Field(30, ge=0)
    throttle_tool_results: int = Field(10, ge=0)


class Config(_Model):
    server: ServerConfig = Field(default_factory=ServerConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    init: InitConfig = Field(default_factory=InitConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    mise: MiseConfig = Field(default_factory=MiseConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    peers: PeersConfig = Field(default_factory=PeersConfig)
    notes: NotesConfig = Field(default_factory=NotesConfig)
    usage: UsageConfig = Field(default_factory=UsageConfig)

    @model_validator(mode="after")
    def _peer_token_distinct(self) -> Config:
        if self.peers.token and self.peers.token == self.server.auth_token:
            raise _at("peers.token", "must differ from server.auth_token")
        return self

    @model_validator(mode="after")
    def _ccw_name_reserved(self) -> Config:
        if (
            self.sessions.commands.agent or self.peers.enabled
        ) and "ccw" in self.claude.mcp_servers:
            raise _at(
                "claude.mcp_servers.ccw", "the name ccw is reserved for the ccw command server"
            )
        return self

    def redacted(self) -> dict[str, Any]:
        """JSON-able dump with secrets replaced by `***`."""
        out = self.model_dump(mode="json")
        for key in ("auth_token", "callback_secret"):
            if out["server"][key]:
                out["server"][key] = MASK
        for section in ("peers", "usage"):
            if out[section]["token"]:
                out[section]["token"] = MASK
        out["claude"]["env"] = self.claude.env.redacted()  # type: ignore[attr-defined]
        for name, server in self.claude.mcp_servers.items():
            dumped = out["claude"]["mcp_servers"][name]
            if isinstance(server, McpHttp):
                dumped["headers"] = dict.fromkeys(server.headers, MASK)
            else:
                dumped["env"] = {
                    k: MASK if SECRET_KEY_RE.search(k.upper()) else v for k, v in server.env.items()
                }
        for event in HOOK_EVENTS:
            for dumped in out["hooks"][event]:
                if dumped.get("secret"):
                    dumped["secret"] = MASK
        return out


def _format_errors(exc: ValidationError) -> list[str]:
    lines = []
    for e in exc.errors():
        loc = [str(p) for p in e["loc"]]
        if e["type"] == "config_path":
            loc.extend(e["ctx"]["sub"].split("."))
            message = e["ctx"]["message"]
        else:
            message = e["msg"].removeprefix("Value error, ")
        lines.append(f"{'.'.join(loc) or '<root>'}: {message}")
    return lines


def load_config(path: str | os.PathLike[str]) -> Config:
    """Read, interpolate and validate a config file; raises `ConfigError` listing every problem."""
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except OSError as e:
        raise ConfigError(f"{path}: cannot read config: {e.strerror or e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("<root>: config must be a mapping")
    try:
        data = interpolate(raw, os.environ)
    except InterpolationError as e:
        raise ConfigError(str(e)) from e
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError("\n".join(_format_errors(e))) from e
