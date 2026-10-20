"""config.yaml models, loader and redaction (design.md "Config schema", D8)."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

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
HOOK_EVENTS: tuple[str, ...] = (
    "onCreate",
    "onResume",
    "onDestroy",
    "onTurn",
    "onTurnComplete",
    "onAgentResponse",
    "onAgentToolCall",
    "onToolResult",
    "onSubagentStart",
    "onSubagentStop",
    "onInterrupt",
    "onError",
)
VETO_EVENTS: tuple[str, ...] = ("onCreate", "onResume", "onTurn", "onAgentToolCall")
TOOL_EVENTS: tuple[str, ...] = ("onAgentToolCall", "onToolResult")
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
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    callback_secret: str = ""

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
        default_factory=lambda: ["? for shortcuts", "bypass permissions on"]
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


class ClaudeConfig(_Model):
    binary: str = "claude"
    model: str = ""
    effort: str = ""
    permission_mode: str = "bypassPermissions"
    extra_args: list[str] = Field(default_factory=list)
    env: ClaudeEnv = Field(default_factory=ClaudeEnv)  # type: ignore[valid-type]
    passthrough_env: list[str] = Field(default_factory=list)
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
    onAgentResponse: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onAgentToolCall: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onToolResult: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onSubagentStart: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onSubagentStop: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onInterrupt: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815
    onError: list[HookAction] = Field(default_factory=list)  # type: ignore[valid-type]  # noqa: N815

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

    def for_event(self, event: str) -> list[CommandAction | WebhookAction]:
        return getattr(self, event)


class SessionsConfig(_Model):
    max_sessions: int = Field(4, ge=1)
    idle_timeout_seconds: float = Field(0, ge=0)
    turn_stall_seconds: float = Field(1800, ge=0)
    interrupt_settle_seconds: float = Field(20, ge=0)
    max_restarts: int = Field(3, ge=0)
    ask_user_question: Literal["forward", "deny", "allow"] = "forward"
    turn_marker: bool = True
    resume_on_start: bool = False


class LimitsConfig(_Model):
    max_prompt_bytes: int = Field(1048576, ge=1)
    max_file_bytes: int = Field(262144, ge=1)
    max_files_per_turn: int = Field(16, ge=0)
    turn_max_seconds: float = Field(0, ge=0)
    event_ring_size: int = Field(1000, ge=1)
    event_log_max_bytes: int = Field(52428800, ge=0)
    sse_replay: int = Field(100, ge=0)
    sse_heartbeat_seconds: float = Field(15, gt=0)
    shutdown_timeout_seconds: float = Field(10, ge=0)


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

    def redacted(self) -> dict[str, Any]:
        """JSON-able dump with secrets replaced by `***`."""
        out = self.model_dump(mode="json")
        for key in ("auth_token", "callback_secret"):
            if out["server"][key]:
                out["server"][key] = MASK
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
