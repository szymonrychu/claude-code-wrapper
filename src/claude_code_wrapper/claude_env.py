# ruff: noqa: E501
"""Typed model of the environment variables Claude Code reads (design decision D2).

One optional field per documented variable; the field name is the variable name
lowercased. The table below was checked against
https://code.claude.com/docs/en/env-vars on 2026-10-20 (plus AWS_REGION, AWS_PROFILE and
CLOUD_ML_REGION, which the provider guides document and the service models too).
Variables the service sets itself (CLAUDE_CONFIG_DIR) are `managed`; `CCW_*`, TERM and HOME
are reserved and cannot be passed through `extra`.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    create_model,
    field_validator,
    model_validator,
)

Group = Literal["auth", "provider", "model", "behaviour", "mcp", "network", "telemetry",
                "plugins", "privacy"]  # fmt: skip
VarType = Literal["str", "bool", "int", "secret"]

EXTRA_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SECRET_KEY_RE = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|AUTH|CREDENTIAL")
RESERVED_EXTRA = frozenset({"TERM", "HOME"})
MANAGED_ENV = frozenset({"CLAUDE_CONFIG_DIR"})
PROVIDER_FLAGS = (
    "claude_code_use_bedrock",
    "claude_code_use_vertex",
    "claude_code_use_foundry",
    "claude_code_use_anthropic_aws",
)
MASK = "***"


class EnvVarSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    env: str
    group: Group
    secret: bool
    managed: bool
    type: VarType
    description: str


# (env name, group, kind, description). kind: secret|bool|int|pct|ttl|word|str.
_TABLE: tuple[tuple[str, str, str, str], ...] = (
    ("ANTHROPIC_API_KEY", "auth", "secret", "API key sent as X-Api-Key header"),
    (
        "ANTHROPIC_AUTH_TOKEN",
        "auth",
        "secret",
        "Custom value for the Authorization header (the value you set here will be prefixed with Bearer )",
    ),
    (
        "ANTHROPIC_AWS_API_KEY",
        "auth",
        "secret",
        "Workspace API key for Claude Platform on AWS, generated in the AWS Console",
    ),
    (
        "ANTHROPIC_AWS_BASE_URL",
        "provider",
        "str",
        "Override the Claude Platform on AWS endpoint URL. Use for custom regions or when routing through an LLM gateway",
    ),
    (
        "ANTHROPIC_AWS_WORKSPACE_ID",
        "provider",
        "str",
        "Required for Claude Platform on AWS. Sent on every request as the anthropic-workspace-id header",
    ),
    (
        "ANTHROPIC_BASE_URL",
        "behaviour",
        "str",
        "Override the API endpoint to route requests through a proxy or gateway",
    ),
    (
        "ANTHROPIC_BEDROCK_BASE_URL",
        "provider",
        "str",
        "Override the Amazon Bedrock endpoint URL. Use for custom Amazon Bedrock endpoints or when routing through an LLM gateway",
    ),
    (
        "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
        "provider",
        "str",
        "Override the Amazon Bedrock Mantle endpoint URL. See Mantle endpoint",
    ),
    (
        "ANTHROPIC_BEDROCK_REGION_PREFIX",
        "provider",
        "str",
        "Cross-region inference profile prefix (us, eu, apac, jp, au, or global) Claude Code tries first instead of the one de...",
    ),
    (
        "ANTHROPIC_BEDROCK_SERVICE_TIER",
        "provider",
        "str",
        "Amazon Bedrock service tier (default, flex, or priority)",
    ),
    (
        "ANTHROPIC_BETAS",
        "model",
        "str",
        "Comma-separated list of additional anthropic-beta header values to include in API requests",
    ),
    (
        "ANTHROPIC_CUSTOM_HEADERS",
        "behaviour",
        "secret",
        "Custom headers to add to requests (Name: Value format, newline-separated for multiple headers)",
    ),
    (
        "ANTHROPIC_CUSTOM_MODEL_OPTION",
        "model",
        "str",
        "Model ID to add as a custom entry in the /model picker",
    ),
    (
        "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
        "model",
        "str",
        "Display description for the custom model entry in the /model picker",
    ),
    (
        "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
        "model",
        "str",
        "Display name for the custom model entry in the /model picker",
    ),
    (
        "ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES",
        "model",
        "str",
        "Comma-separated list of capabilities the custom model supports, for example effort,thinking",
    ),
    (
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "model",
        "str",
        "Model ID that the fable alias resolves to, and the ID Claude Code recognizes as a Fable model for automatic model fal...",
    ),
    (
        "ANTHROPIC_DEFAULT_FABLE_MODEL_DESCRIPTION",
        "model",
        "str",
        "Display description for the pinned Fable model in the /model picker",
    ),
    (
        "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
        "model",
        "str",
        "Display name for the pinned Fable model in the /model picker",
    ),
    (
        "ANTHROPIC_DEFAULT_FABLE_MODEL_SUPPORTED_CAPABILITIES",
        "model",
        "str",
        "Comma-separated list of capabilities the pinned Fable model supports, for example effort,thinking",
    ),
    (
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "model",
        "str",
        "Model ID that the haiku alias resolves to, also used for background functionality",
    ),
    (
        "ANTHROPIC_DEFAULT_HAIKU_MODEL_DESCRIPTION",
        "model",
        "str",
        "Display description for the pinned Haiku model in the /model picker",
    ),
    (
        "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
        "model",
        "str",
        "Display name for the pinned Haiku model in the /model picker",
    ),
    (
        "ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES",
        "model",
        "str",
        "Comma-separated list of capabilities the pinned Haiku model supports, for example effort,thinking",
    ),
    ("ANTHROPIC_DEFAULT_MODEL", "model", "str", "Model that new sessions start on by default"),
    (
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "model",
        "str",
        "Model ID that the opus alias resolves to, and that opusplan uses while Plan Mode is active",
    ),
    (
        "ANTHROPIC_DEFAULT_OPUS_MODEL_DESCRIPTION",
        "model",
        "str",
        "Display description for the pinned Opus model in the /model picker",
    ),
    (
        "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
        "model",
        "str",
        "Display name for the pinned Opus model in the /model picker",
    ),
    (
        "ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES",
        "model",
        "str",
        "Comma-separated list of capabilities the pinned Opus model supports, for example effort,thinking",
    ),
    (
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "model",
        "str",
        "Model ID that the sonnet alias resolves to, and that opusplan uses when Plan Mode is not active",
    ),
    (
        "ANTHROPIC_DEFAULT_SONNET_MODEL_DESCRIPTION",
        "model",
        "str",
        "Display description for the pinned Sonnet model in the /model picker",
    ),
    (
        "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
        "model",
        "str",
        "Display name for the pinned Sonnet model in the /model picker",
    ),
    (
        "ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES",
        "model",
        "str",
        "Comma-separated list of capabilities the pinned Sonnet model supports, for example effort,thinking",
    ),
    (
        "ANTHROPIC_FEDERATION_RULE_ID",
        "behaviour",
        "str",
        "Federation rule ID for Workload Identity Federation",
    ),
    (
        "ANTHROPIC_FOUNDRY_API_KEY",
        "auth",
        "secret",
        "API key for Microsoft Foundry authentication (see Microsoft Foundry)",
    ),
    (
        "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
        "auth",
        "secret",
        "Bearer token for Microsoft Foundry authentication, such as a Microsoft Entra access token",
    ),
    (
        "ANTHROPIC_FOUNDRY_BASE_URL",
        "provider",
        "str",
        "Full base URL for the Microsoft Foundry resource (for example, https://my-resource.services.ai.azure.com/anthropic)",
    ),
    (
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "provider",
        "str",
        "Microsoft Foundry resource name (for example, my-resource)",
    ),
    (
        "ANTHROPIC_MODEL",
        "model",
        "str",
        "Name of the model setting to use (see Model Configuration)",
    ),
    (
        "ANTHROPIC_ORGANIZATION_ID",
        "behaviour",
        "str",
        "Organization ID for Workload Identity Federation",
    ),
    (
        "ANTHROPIC_PROFILE",
        "behaviour",
        "str",
        "Name of the Anthropic profile to authenticate with, such as one created by ant auth login or by signing in to a Conso...",
    ),
    (
        "ANTHROPIC_SMALL_FAST_MODEL",
        "model",
        "str",
        "[DEPRECATED] Name of Haiku-class model for background tasks",
    ),
    (
        "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
        "model",
        "str",
        "Override AWS region for the Haiku-class model when using Amazon Bedrock or Amazon Bedrock Mantle",
    ),
    (
        "ANTHROPIC_VERTEX_BASE_URL",
        "provider",
        "str",
        "Override Google Cloud's Agent Platform endpoint URL. Use for custom Google Cloud's Agent Platform endpoints or when r...",
    ),
    (
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "provider",
        "str",
        "GCP project ID that Google Cloud's Agent Platform requests are addressed to",
    ),
    ("ANTHROPIC_WORKSPACE_ID", "behaviour", "str", "Workspace ID for workload identity federation"),
    (
        "API_FORCE_IDLE_TIMEOUT",
        "behaviour",
        "int",
        "Override the 5-minute body idle timeout that aborts a streaming model response when no bytes arrive",
    ),
    (
        "API_TIMEOUT_MS",
        "behaviour",
        "int",
        "Time in milliseconds that each API request attempt has to receive its response headers (default: 600000, or 10 minute...",
    ),
    (
        "AWS_BEARER_TOKEN_BEDROCK",
        "provider",
        "secret",
        "Amazon Bedrock API key for authentication (see Amazon Bedrock API keys)",
    ),
    ("AWS_PROFILE", "provider", "str", "AWS profile name for Amazon Bedrock credentials"),
    ("AWS_REGION", "provider", "str", "AWS region for Amazon Bedrock"),
    (
        "BASH_DEFAULT_TIMEOUT_MS",
        "behaviour",
        "int",
        "Default timeout for a foreground Bash or PowerShell tool command, in milliseconds (default: 120000, or 2 minutes)",
    ),
    (
        "BASH_MAX_OUTPUT_LENGTH",
        "behaviour",
        "int",
        "Maximum number of characters of bash output that Claude Code reads back into a command's result (default: 30000; maxi...",
    ),
    (
        "BASH_MAX_TIMEOUT_MS",
        "behaviour",
        "int",
        "Maximum timeout the model can set for a foreground Bash or PowerShell tool command, in milliseconds (default: 600000,...",
    ),
    (
        "BETA_TRACING_ENDPOINT",
        "behaviour",
        "str",
        "OTLP/HTTP endpoint for detailed beta tracing: with ENABLE_BETA_TRACING_DETAILED=1, logs and traces go there instead o...",
    ),
    (
        "CCR_FORCE_BUNDLE",
        "behaviour",
        "bool",
        "Set to 1 to force claude --cloud to bundle and upload your local repository instead of cloning from its remote",
    ),
    (
        "CLAUDECODE",
        "behaviour",
        "str",
        "Set to 1 in subprocesses Claude Code spawns (Bash and PowerShell tools, tmux sessions, hook commands, status line com...",
    ),
    (
        "CLAUDE_AFK_COUNTDOWN_MS",
        "behaviour",
        "int",
        "How many milliseconds before auto-continue the on-screen countdown appears on an unanswered AskUserQuestion dialog",
    ),
    (
        "CLAUDE_AFK_TIMEOUT_MS",
        "behaviour",
        "int",
        "How many milliseconds of idle time before an unanswered AskUserQuestion dialog auto-continues without you",
    ),
    (
        "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS",
        "behaviour",
        "bool",
        "Set to 1 to disable all built-in subagent types such as Explore and Plan",
    ),
    (
        "CLAUDE_AGENT_SDK_MCP_NO_PREFIX",
        "mcp",
        "str",
        "Set to 1 to skip the mcp__<server>__ prefix on tool names from SDK-created MCP servers",
    ),
    (
        "CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS",
        "behaviour",
        "int",
        "Stall timeout in milliseconds for subagents",
    ),
    (
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
        "model",
        "pct",
        "Set the percentage (1-100) of the auto-compact window at which auto-compaction triggers",
    ),
    (
        "CLAUDE_AUTO_BACKGROUND_TASKS",
        "behaviour",
        "bool",
        "Set to 1 to force-enable automatic backgrounding of long-running agent tasks",
    ),
    (
        "CLAUDE_AX_PREPARK_MS",
        "behaviour",
        "int",
        "In screen reader mode, how many milliseconds Claude Code waits before it writes a new or changed line",
    ),
    (
        "CLAUDE_AX_SCREEN_READER",
        "behaviour",
        "bool",
        "Set to 1 to render screen-reader friendly output: flat text without decorative borders or animations",
    ),
    (
        "CLAUDE_AX_STARTUP_QUIET_MS",
        "behaviour",
        "int",
        "In screen reader mode, how many milliseconds Claude Code holds the first interface render after the startup confirmat...",
    ),
    (
        "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR",
        "behaviour",
        "str",
        "Return to the original working directory after each Bash or PowerShell command in the main session",
    ),
    (
        "CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS",
        "behaviour",
        "int",
        "Timeout in milliseconds for the byte-level streaming idle watchdog; when set, it takes precedence over CLAUDE_STREAM_...",
    ),
    (
        "CLAUDE_CLIENT_PRESENCE_FILE",
        "behaviour",
        "str",
        "Path to a file that an external tool, such as a screen-lock listener, creates when you unlock your screen and deletes...",
    ),
    (
        "CLAUDE_CODE_ACCESSIBILITY",
        "behaviour",
        "bool",
        "Set to 1 to keep the native terminal cursor visible and disable the inverted-text cursor indicator",
    ),
    (
        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
        "behaviour",
        "bool",
        "Set to 1 to load memory files from directories specified with --add-dir",
    ),
    (
        "CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT",
        "behaviour",
        "bool",
        "Set to 1 to repaint the entire screen on every frame in fullscreen rendering instead of sending incremental updates",
    ),
    (
        "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT",
        "model",
        "str",
        "Set to 1 to send the effort parameter with every request, even when Claude Code does not recognize the model ID as ef...",
    ),
    (
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
        "auth",
        "int",
        "Interval in milliseconds at which credentials should be refreshed (when using apiKeyHelper)",
    ),
    (
        "CLAUDE_CODE_ARTIFACT_AUTO_OPEN",
        "behaviour",
        "str",
        "Set to 0 to stop Claude Code from opening the browser automatically when a new artifact is published",
    ),
    (
        "CLAUDE_CODE_ARTIFACT_COMMENTS",
        "behaviour",
        "str",
        "Set to 0 to stop Claude reading and replying to comments on an artifact",
    ),
    (
        "CLAUDE_CODE_ARTIFACT_COMMENTS_AUTOREACT",
        "behaviour",
        "str",
        "Set to 0 to stop Claude replying on its own to comments sent to it",
    ),
    (
        "CLAUDE_CODE_ARTIFACT_PUBLISH_EDITS",
        "behaviour",
        "str",
        "Set to 0 to stop Claude from applying small changes to an artifact's file as part of publishing it",
    ),
    (
        "CLAUDE_CODE_ATTRIBUTION_HEADER",
        "behaviour",
        "str",
        "Set to 0 to omit the attribution block, which carries the client version and a prompt fingerprint, from the start of ...",
    ),
    (
        "CLAUDE_CODE_AUTO_BACKGROUND_WORKER_CHECKIN_SECONDS",
        "behaviour",
        "str",
        "Removed in v2.1.283. Use CLAUDE_CODE_WORKER_CHECKIN_SCHEDULE instead",
    ),
    (
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "model",
        "str",
        "Set the auto-compact window, how full the context gets before compaction, from 100000 to 1000000 tokens",
    ),
    ("CLAUDE_CODE_AUTO_CONNECT_IDE", "behaviour", "str", "Override automatic IDE connection"),
    (
        "CLAUDE_CODE_AUTO_MODE_MODEL",
        "model",
        "str",
        "Sets the model for the classifier requests Claude Code sends in auto mode",
    ),
    (
        "CLAUDE_CODE_AUTO_MODE_SERVER",
        "behaviour",
        "str",
        "Controls whether Claude Code asks the server to review auto mode actions",
    ),
    (
        "CLAUDE_CODE_AWS_CHAIN_RESOLVE_TIMEOUT_MS",
        "behaviour",
        "int",
        "Time in milliseconds Claude Code waits for the AWS default credential provider chain to produce credentials before th...",
    ),
    (
        "CLAUDE_CODE_BASH_EDIT_DIFF",
        "behaviour",
        "str",
        "Set to 0 to turn off the diff of the files that changed while a Bash command ran, or 1 to record it in every permissi...",
    ),
    (
        "CLAUDE_CODE_BG_TASKS_REPORT_RUNNING",
        "behaviour",
        "str",
        "Set to 0 to make a non-interactive session report an idle status to its host at every turn end, even while background...",
    ),
    (
        "CLAUDE_CODE_BRIDGE_SESSION_ID",
        "behaviour",
        "str",
        "Set automatically in Bash tool and hook command subprocesses while the session has an active Remote Control connectio...",
    ),
    (
        "CLAUDE_CODE_BS_AS_CTRL_BACKSPACE",
        "behaviour",
        "str",
        "Set to 0 to make Claude Code read the 0x08 byte, also written ^H, as plain Backspace, or 1 to read it as Ctrl+Backspace",
    ),
    (
        "CLAUDE_CODE_CERT_STORE",
        "network",
        "str",
        "Comma-separated list of CA certificate sources for TLS connections",
    ),
    (
        "CLAUDE_CODE_CHILD_SESSION",
        "behaviour",
        "str",
        "Set to 1 in subprocesses Claude Code spawns via the Bash, PowerShell, and Monitor tools, hook commands, and status li...",
    ),
    (
        "CLAUDE_CODE_CLIENT_CERT",
        "network",
        "str",
        "Path to client certificate file for mTLS authentication",
    ),
    (
        "CLAUDE_CODE_CLIENT_KEY",
        "behaviour",
        "str",
        "Path to client private key file for mTLS authentication",
    ),
    (
        "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
        "behaviour",
        "secret",
        "Passphrase for encrypted CLAUDE_CODE_CLIENT_KEY (optional)",
    ),
    ("CLAUDE_CODE_CONNECT_TIMEOUT_MS", "behaviour", "str", "Removed in v2.1.186 and now a no-op"),
    ("CLAUDE_CODE_DEBUG_LOGS_DIR", "behaviour", "str", "Override the debug log file path"),
    (
        "CLAUDE_CODE_DEBUG_LOG_LEVEL",
        "behaviour",
        "str",
        "Minimum log level written to the debug log file",
    ),
    (
        "CLAUDE_CODE_DISABLE_1M_CONTEXT",
        "model",
        "bool",
        "Set to 1 to turn off 1M context window support",
    ),
    (
        "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
        "model",
        "bool",
        "Set to 1 to disable adaptive reasoning on Opus 4.6 and Sonnet 4.6 and fall back to the fixed thinking budget controll...",
    ),
    (
        "CLAUDE_CODE_DISABLE_ADMIN_ENV_UNION",
        "behaviour",
        "bool",
        "Set to 1 to stop Claude Code from merging managed settings env blocks per key across admin sources, so only the highe...",
    ),
    (
        "CLAUDE_CODE_DISABLE_ADVISOR_TOOL",
        "behaviour",
        "bool",
        "Set to 1 to disable the advisor tool",
    ),
    (
        "CLAUDE_CODE_DISABLE_AGENT_VIEW",
        "behaviour",
        "bool",
        "Set to 1 to turn off background agents and agent view: claude agents, --bg, /background, and the on-demand supervisor",
    ),
    (
        "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN",
        "behaviour",
        "bool",
        "Set to 1 to disable fullscreen rendering and use the classic main-screen renderer",
    ),
    (
        "CLAUDE_CODE_DISABLE_ARTIFACT",
        "behaviour",
        "bool",
        "Set to 1 to turn off the Artifact tool, which publishes session output as a private web page on claude.ai",
    ),
    (
        "CLAUDE_CODE_DISABLE_ATTACHMENTS",
        "behaviour",
        "bool",
        "Set to 1 to disable attachment processing",
    ),
    (
        "CLAUDE_CODE_DISABLE_AUTH_REFRESH_LOCK",
        "behaviour",
        "str",
        "Set to 1 to make a Claude Code process run its gcpAuthRefresh or awsAuthRefresh command itself instead of waiting whi...",
    ),
    ("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "privacy", "bool", "Set to 1 to disable auto memory"),
    (
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
        "behaviour",
        "bool",
        "Set to 1 to disable all background task functionality, including the run_in_background parameter on Bash and subagent...",
    ),
    (
        "CLAUDE_CODE_DISABLE_BEDROCK_CONTENT_TYPE_DEFAULT",
        "behaviour",
        "bool",
        "Set to 1 to stop Claude Code from treating an Amazon Bedrock streaming response with a missing or empty Content-Type ...",
    ),
    (
        "CLAUDE_CODE_DISABLE_BEDROCK_CONTENT_TYPE_GUARD",
        "behaviour",
        "bool",
        "Set to 1 to skip the check that an Amazon Bedrock streaming response carries the application/vnd.amazon.eventstream c...",
    ),
    (
        "CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF",
        "behaviour",
        "str",
        "Set to 1 to stop a background session's running background shell commands, dynamic workflows, and, as of v2.1.198, ba...",
    ),
    (
        "CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP",
        "behaviour",
        "str",
        "Set to 1 to stop Claude Code from terminating background shell commands under memory pressure",
    ),
    (
        "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS",
        "behaviour",
        "str",
        "Set to 1 to disable the skills and workflows included with Claude Code: bundled skills and workflows are removed enti...",
    ),
    (
        "CLAUDE_CODE_DISABLE_CFC_PROMPT",
        "behaviour",
        "bool",
        "Set to 1 to keep the Claude in Chrome browser tools available while omitting the Chrome section of the system prompt ...",
    ),
    (
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
        "behaviour",
        "bool",
        "Set to 1 to prevent loading any CLAUDE.md memory files into context, including user, project, and auto memory files",
    ),
    (
        "CLAUDE_CODE_DISABLE_CRON",
        "behaviour",
        "bool",
        "Set to 1 to disable session-scoped scheduled tasks",
    ),
    (
        "CLAUDE_CODE_DISABLE_DANGEROUS_RM_TIMEOUT",
        "behaviour",
        "str",
        "Set to 1 to turn off the time limit on critical-path removal prompts",
    ),
    (
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
        "model",
        "bool",
        "Set to 1 to strip pre-release anthropic-beta request headers, the body fields that pair with them, and beta tool-sche...",
    ),
    (
        "CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS",
        "behaviour",
        "bool",
        "Set to 1 to disable the built-in Explore and Plan subagents",
    ),
    ("CLAUDE_CODE_DISABLE_FAST_MODE", "model", "bool", "Set to 1 to disable fast mode"),
    (
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY",
        "privacy",
        "bool",
        'Set to 1 to disable the "How is Claude doing?" session quality surveys',
    ),
    (
        "CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING",
        "behaviour",
        "bool",
        "Set to 1 to disable file checkpointing",
    ),
    (
        "CLAUDE_CODE_DISABLE_FILE_MENTIONS",
        "behaviour",
        "bool",
        "Set to 1 to stop Claude Code from expanding @ mentions of files and directories",
    ),
    (
        "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS",
        "behaviour",
        "bool",
        "Set to 1 to remove built-in commit and PR workflow instructions and the git status snapshot from Claude's context",
    ),
    (
        "CLAUDE_CODE_DISABLE_INLINE_SHELL_RM_PROMPT",
        "behaviour",
        "str",
        "Set to 1 to stop Claude Code from reading scripts passed to a shell with -c, such as bash -c 'rm -rf ~', for critical...",
    ),
    (
        "CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP",
        "model",
        "bool",
        "Set to 1 to prevent automatic remapping of Opus 4 and Opus 4.1 to the current Opus version on the Anthropic API. Use ...",
    ),
    (
        "CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK",
        "model",
        "str",
        "Set to 1 to stop Claude Code on Amazon Bedrock and Google Cloud's Agent Platform from switching to an older model whe...",
    ),
    (
        "CLAUDE_CODE_DISABLE_MOUNTED_DRIVE_RM_PROMPT",
        "behaviour",
        "str",
        "Set to 1 to turn off the mounted drive prompt for paths such as /Volumes/USB. Set it in the environment that launches...",
    ),
    (
        "CLAUDE_CODE_DISABLE_MOUSE",
        "behaviour",
        "bool",
        "Set to 1 to disable mouse tracking in fullscreen rendering",
    ),
    (
        "CLAUDE_CODE_DISABLE_MOUSE_CLICKS",
        "behaviour",
        "bool",
        "Set to 1 to disable click, drag, and hover handling in fullscreen rendering while keeping mouse-wheel scrolling",
    ),
    (
        "CLAUDE_CODE_DISABLE_MTLS_RELOAD_ON_STALE_CONNECTION",
        "network",
        "bool",
        "Set to 1 to stop Claude Code from re-reading the mTLS client certificate and key when an API request fails with a con...",
    ),
    (
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "privacy",
        "bool",
        "Set to any non-empty value, such as 1, to disable nonessential network traffic: auto-updates, telemetry, error report...",
    ),
    (
        "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK",
        "behaviour",
        "str",
        "Set to 1 to disable the non-streaming fallback when a streaming request fails mid-stream",
    ),
    (
        "CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK",
        "behaviour",
        "bool",
        "Set to 1 to send the PushNotification tool's desktop notification even while you are typing in or focused on the term...",
    ),
    (
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL",
        "plugins",
        "bool",
        "Set to 1 to disable automatic registration of the official plugin marketplace",
    ),
    (
        "CLAUDE_CODE_DISABLE_PERMISSION_PROMPT_NOTIFY_HOOKS",
        "behaviour",
        "bool",
        "Set to 1 to stop Claude Code from running your Notification hooks for unanswered permission requests in sessions wher...",
    ),
    (
        "CLAUDE_CODE_DISABLE_POLICY_SKILLS",
        "behaviour",
        "str",
        "Set to 1 to skip loading skills from the system-wide managed skills directory",
    ),
    (
        "CLAUDE_CODE_DISABLE_POWERSHELL_CMD_RM_DENY",
        "behaviour",
        "str",
        "Set to 1 to turn off the PowerShell tool check that denies the cmd built-ins rd, rmdir, del, and erase on a system pa...",
    ),
    (
        "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK",
        "behaviour",
        "str",
        "Set to 1 to turn off the automatic model switch when a safety classifier flags a request, the behavior the switchMode...",
    ),
    (
        "CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS",
        "behaviour",
        "bool",
        "Set to 1 to stop Claude Code from sending the structured-output output_config.format field and the anthropic-beta val...",
    ),
    (
        "CLAUDE_CODE_DISABLE_SUBSTITUTION_RM_PROMPT",
        "behaviour",
        "str",
        "Set to 1 to turn off the critical-path check for a recursive rm whose target is entirely the output of a command subs...",
    ),
    (
        "CLAUDE_CODE_DISABLE_TERMINAL_TITLE",
        "behaviour",
        "bool",
        "Set to 1 to disable automatic terminal title updates based on conversation context",
    ),
    (
        "CLAUDE_CODE_DISABLE_THINKING",
        "model",
        "bool",
        "Set to 1 to omit the thinking parameter from API requests entirely",
    ),
    (
        "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT",
        "model",
        "str",
        "Set to 1 to skip proactive auto-compaction when Claude Code doesn't recognize the model ID, such as an LLM gateway alias",
    ),
    (
        "CLAUDE_CODE_DISABLE_VIRTUAL_SCROLL",
        "behaviour",
        "bool",
        "Set to 1 to disable virtual scrolling in fullscreen rendering and render every message in the transcript",
    ),
    (
        "CLAUDE_CODE_DISABLE_WEB_FETCH",
        "behaviour",
        "bool",
        "Set to 1 to turn off the WebFetch tool",
    ),
    (
        "CLAUDE_CODE_DISABLE_WINDOWS_SHELL_LAUNCHER",
        "behaviour",
        "str",
        "Set to 1 to start PowerShell tool commands on Windows directly instead of through the cmd.exe launcher",
    ),
    ("CLAUDE_CODE_DISABLE_WORKFLOWS", "behaviour", "bool", "Set to 1 to disable workflows"),
    ("CLAUDE_CODE_EFFORT_LEVEL", "model", "str", "Set the effort level for supported models"),
    (
        "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS",
        "behaviour",
        "bool",
        "Set to 1 to add session_state_changed messages, which carry the session's state, to the message stream",
    ),
    (
        "CLAUDE_CODE_ENABLE_AUTO_MODE",
        "behaviour",
        "str",
        "Accepted for compatibility with older releases and has no effect",
    ),
    ("CLAUDE_CODE_ENABLE_AWAY_SUMMARY", "behaviour", "str", "Override session recap availability"),
    (
        "CLAUDE_CODE_ENABLE_BACKGROUND_PLUGIN_REFRESH",
        "plugins",
        "bool",
        "Set to 1 to refresh plugin state at turn boundaries in non-interactive mode after a background install completes",
    ),
    (
        "CLAUDE_CODE_ENABLE_CFC",
        "behaviour",
        "bool",
        "Set to 1 to start a CLI session with Chrome integration on, or 0 to start with it off",
    ),
    (
        "CLAUDE_CODE_ENABLE_FEEDBACK_SURVEY_FOR_OTEL",
        "telemetry",
        "bool",
        'Set to 1 to route the "How is Claude doing?" session quality survey to your own OpenTelemetry collector when Anthropi...',
    ),
    (
        "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING",
        "behaviour",
        "bool",
        "Controls whether tool call inputs stream from the API as Claude generates them",
    ),
    (
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
        "model",
        "str",
        "Set to 1 to populate the /model picker from your gateway's /v1/models endpoint when ANTHROPIC_BASE_URL points at an A...",
    ),
    (
        "CLAUDE_CODE_ENABLE_OPUS_4_7_FAST_MODE",
        "model",
        "str",
        "Removed in v2.1.142, when the fast mode default moved from Opus 4.6 to Opus 4.7",
    ),
    (
        "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION",
        "behaviour",
        "bool",
        "Set to false to turn off prompt suggestions, the grayed-out predictions that appear in your prompt input",
    ),
    (
        "CLAUDE_CODE_ENABLE_TASKS",
        "behaviour",
        "str",
        "Selects which task-tracking tools Claude Code provides in sessions that have them",
    ),
    (
        "CLAUDE_CODE_ENABLE_TELEMETRY",
        "telemetry",
        "bool",
        "Set to 1 to enable OpenTelemetry data collection for metrics and logging",
    ),
    (
        "CLAUDE_CODE_ENABLE_TODO_TOOLS",
        "behaviour",
        "str",
        "Set to 1 to get the task-tracking tools on every model",
    ),
    (
        "CLAUDE_CODE_EXIT_AFTER_STOP_DELAY",
        "behaviour",
        "int",
        "Time in milliseconds to wait after the query loop becomes idle before automatically exiting",
    ),
    ("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "behaviour", "bool", "Set to 1 to enable agent teams"),
    (
        "CLAUDE_CODE_EXTRA_BODY",
        "behaviour",
        "str",
        "JSON object to merge into the top level of every API request body",
    ),
    (
        "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS",
        "model",
        "int",
        "Override the default token limit for file reads, which is 25,000 tokens",
    ),
    (
        "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE",
        "behaviour",
        "str",
        "Set to 1 to force transcript persistence, prompt history, and claude agents registration even when this claude was la...",
    ),
    (
        "CLAUDE_CODE_FORCE_STRIKETHROUGH",
        "behaviour",
        "bool",
        "Set to 1 to force strikethrough rendering for ~~text~~ in Claude's responses when your terminal supports it but is no...",
    ),
    (
        "CLAUDE_CODE_FORCE_SYNC_OUTPUT",
        "behaviour",
        "bool",
        "Set to 1 to force-enable DEC private mode 2026 synchronized output when your terminal supports it but is not auto-det...",
    ),
    (
        "CLAUDE_CODE_FORCE_TERMINAL_IMAGES",
        "behaviour",
        "bool",
        "Set to 1 to draw mod Image elements as pictures when your terminal draws kitty graphics protocol images with Unicode ...",
    ),
    (
        "CLAUDE_CODE_FORK_SUBAGENT",
        "behaviour",
        "str",
        "Controls fork mode, which is on by default and lets Claude start forks on its own",
    ),
    (
        "CLAUDE_CODE_FORWARD_SUBAGENT_TEXT",
        "behaviour",
        "bool",
        "Set to 1 to emit subagent text and thinking blocks in claude -p --output-format stream-json output, the same behavior...",
    ),
    (
        "CLAUDE_CODE_GATEWAY_HINT_HEADERS",
        "behaviour",
        "str",
        "Set to 1 to send the gateway hint headers, such as x-claude-code-request-class and x-claude-code-compaction, on a cus...",
    ),
    (
        "CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY_TIMEOUT_MS",
        "model",
        "int",
        "Timeout in milliseconds for the gateway model discovery request that CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY turns...",
    ),
    (
        "CLAUDE_CODE_GIT_BASH_PATH",
        "behaviour",
        "str",
        "Windows only: path to the Git Bash executable (bash.exe)",
    ),
    (
        "CLAUDE_CODE_GLOB_HIDDEN",
        "behaviour",
        "str",
        "Set to false to exclude dotfiles from results when Claude invokes the Glob tool",
    ),
    (
        "CLAUDE_CODE_GLOB_NO_IGNORE",
        "behaviour",
        "str",
        "Set to false to make the Glob tool respect .gitignore patterns",
    ),
    (
        "CLAUDE_CODE_GLOB_TIMEOUT_SECONDS",
        "behaviour",
        "int",
        "Timeout in seconds for Glob tool file discovery",
    ),
    (
        "CLAUDE_CODE_GOAL_CHECKIN_MINUTES",
        "behaviour",
        "int",
        "How many minutes background work can keep an active goal waiting before Claude Code asks Claude to check on it",
    ),
    (
        "CLAUDE_CODE_GZIP_REQUEST_BODIES",
        "behaviour",
        "str",
        "Set to 0 to turn off gzip compression of Claude API, telemetry, and artifact publish request bodies sent to api.anthr...",
    ),
    (
        "CLAUDE_CODE_HIDE_CWD",
        "behaviour",
        "str",
        "Set to 1 to hide the working directory in the startup logo",
    ),
    (
        "CLAUDE_CODE_IDE_HOST_OVERRIDE",
        "behaviour",
        "str",
        "Override the host address used to connect to the IDE extension",
    ),
    (
        "CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL",
        "behaviour",
        "str",
        "Set to 1 to skip auto-installation of IDE extensions",
    ),
    (
        "CLAUDE_CODE_IDE_SKIP_VALID_CHECK",
        "behaviour",
        "str",
        "Set to 1 to skip validation of IDE lockfile entries during connection",
    ),
    (
        "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS",
        "model",
        "int",
        "Context size, in tokens, at which idle compaction applies to a conversation (default: 200000)",
    ),
    (
        "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
        "behaviour",
        "int",
        "How many subagents can be running in one session before the Agent tool refuses to spawn another (default: 20)",
    ),
    (
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
        "model",
        "int",
        "Override the context window size Claude Code assumes for the active model",
    ),
    (
        "CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH",
        "mcp",
        "int",
        "Maximum length in characters of MCP tool descriptions and MCP server instructions that Claude Code sends to the model",
    ),
    (
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        "model",
        "int",
        "Set the maximum number of output tokens for most requests",
    ),
    (
        "CLAUDE_CODE_MAX_RETRIES",
        "behaviour",
        "int",
        "Override the number of times to retry failed API requests (default: 10)",
    ),
    (
        "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION",
        "behaviour",
        "str",
        "Removed in v2.1.224 and now a no-op",
    ),
    (
        "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
        "behaviour",
        "int",
        "Number of subagent layers allowed below the main conversation (default: 3)",
    ),
    (
        "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY",
        "provider",
        "int",
        "Maximum number of read-only tools and subagents that can execute in parallel (default: 10)",
    ),
    (
        "CLAUDE_CODE_MAX_TURNS",
        "behaviour",
        "int",
        "Cap the number of agentic turns when no explicit limit is passed",
    ),
    (
        "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION",
        "behaviour",
        "int",
        "Cap on WebSearch calls (default: 200)",
    ),
    (
        "CLAUDE_CODE_MCP_ALLOWLIST_ENV",
        "mcp",
        "bool",
        "Set to 1 to spawn stdio MCP servers with only a safe baseline environment plus the server's configured env, instead o...",
    ),
    (
        "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS",
        "mcp",
        "int",
        "Elapsed time in milliseconds before a still-running MCP tool call moves to a background task (default: 120000, or 2 m...",
    ),
    (
        "CLAUDE_CODE_MCP_STARTUP_WAIT_MS",
        "mcp",
        "int",
        "How long in milliseconds the first turn of a non-interactive session waits for MCP servers that are still connecting,...",
    ),
    (
        "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT",
        "mcp",
        "int",
        "Idle timeout in milliseconds for MCP tool calls",
    ),
    (
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "behaviour",
        "str",
        "Set by Claude Code, not by you: in sessions that bind an inbox socket, Claude Code exports that socket's path to hook...",
    ),
    (
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "auth",
        "secret",
        "Set by Claude Code, not by you: in sessions that bind an inbox socket, Claude Code exports this per-session token to ...",
    ),
    (
        "CLAUDE_CODE_NATIVE_CURSOR",
        "behaviour",
        "bool",
        "Set to 1 to show the terminal's own cursor at the input caret instead of a drawn block",
    ),
    (
        "CLAUDE_CODE_NEW_INIT",
        "behaviour",
        "bool",
        "Set to 1 to make /init run an interactive setup flow",
    ),
    (
        "CLAUDE_CODE_NONBLOCKING_STDOUT",
        "behaviour",
        "str",
        "Set to 1 to write terminal output through a second non-blocking file descriptor, so a terminal that stops reading, su...",
    ),
    (
        "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES",
        "behaviour",
        "int",
        "Limit how many times Claude Code re-sends a non-streaming request that times out",
    ),
    (
        "CLAUDE_CODE_NO_FLICKER",
        "behaviour",
        "bool",
        "Set to 1 to enable fullscreen rendering, a research preview that reduces flicker and keeps memory flat in long conver...",
    ),
    (
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "auth",
        "secret",
        "OAuth refresh token for Claude.ai authentication",
    ),
    (
        "CLAUDE_CODE_OAUTH_SCOPES",
        "auth",
        "str",
        'Space-separated OAuth scopes the refresh token was issued with, such as "user:profile user:inference user:sessions:cl...',
    ),
    (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "auth",
        "secret",
        "OAuth access token for claude.ai authentication",
    ),
    (
        "CLAUDE_CODE_OPUS_4_6_FAST_MODE_OVERRIDE",
        "model",
        "str",
        "Removed in v2.1.160 and now a no-op",
    ),
    (
        "CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH",
        "telemetry",
        "int",
        "Maximum length of content-bearing OpenTelemetry attributes (model responses, tool content, system prompts, raw API bo...",
    ),
    (
        "CLAUDE_CODE_OTEL_DIAG_STDERR",
        "telemetry",
        "bool",
        "Set to 1 to write OpenTelemetry exporter diagnostic errors to stderr",
    ),
    (
        "CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS",
        "telemetry",
        "int",
        "Timeout in milliseconds for flushing pending OpenTelemetry spans (default: 5000)",
    ),
    (
        "CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS",
        "telemetry",
        "int",
        "Interval for refreshing dynamic OpenTelemetry headers in milliseconds (default: 1740000 / 29 minutes)",
    ),
    (
        "CLAUDE_CODE_OTEL_SHUTDOWN_TIMEOUT_MS",
        "telemetry",
        "int",
        "Timeout in milliseconds for the OpenTelemetry exporter to finish on shutdown (default: 2000)",
    ),
    (
        "CLAUDE_CODE_OVERLOADED_RETRY_BASE_DELAY_MS",
        "behaviour",
        "int",
        "Starting delay in milliseconds, in place of the default 500, of the exponential backoff between automatic retries of ...",
    ),
    (
        "CLAUDE_CODE_OVERLOADED_RETRY_MAX_DELAY_MS",
        "behaviour",
        "int",
        "Ceiling in milliseconds, in place of the default 32000, on the exponential backoff between automatic retries of a req...",
    ),
    (
        "CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE",
        "behaviour",
        "str",
        "Set to 1 to let Claude Code run your package manager's upgrade command in the background when a new version is available",
    ),
    (
        "CLAUDE_CODE_PERFORCE_MODE",
        "behaviour",
        "bool",
        "Set to 1 to enable Perforce-aware write protection",
    ),
    ("CLAUDE_CODE_PLUGIN_CACHE_DIR", "plugins", "str", "Override the plugins root directory"),
    (
        "CLAUDE_CODE_PLUGIN_DIRS",
        "plugins",
        "str",
        "Plugin directories to load for the session, each loaded the way a --plugin-dir flag loads it",
    ),
    (
        "CLAUDE_CODE_PLUGIN_DIR_WATCH",
        "plugins",
        "bool",
        "Controls whether Claude Code reloads a mod when the mod's files change",
    ),
    (
        "CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS",
        "plugins",
        "int",
        "Timeout in milliseconds for cloning or refreshing a plugin marketplace (default: 120000)",
    ),
    (
        "CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE",
        "plugins",
        "bool",
        "Set to 1 to skip the re-clone attempt and keep using the existing marketplace checkout when a marketplace refresh can...",
    ),
    (
        "CLAUDE_CODE_PLUGIN_PREFER_HTTPS",
        "plugins",
        "bool",
        "Set to 1 to clone GitHub owner/repo shorthand sources over HTTPS instead of SSH. Applies to plugin install and update...",
    ),
    (
        "CLAUDE_CODE_PLUGIN_SEED_DIR",
        "plugins",
        "str",
        "Path to one or more read-only plugin seed directories, separated by : on Unix or ; on Windows",
    ),
    (
        "CLAUDE_CODE_POWERSHELL_RESPECT_EXECUTION_POLICY",
        "behaviour",
        "str",
        "Set to 1 to stop Claude Code from passing -ExecutionPolicy Bypass when spawning PowerShell for tool calls, hooks, and...",
    ),
    (
        "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS",
        "behaviour",
        "int",
        "Ceiling in milliseconds on idle waiting for background work, such as subagents and workflows, after the final turn in...",
    ),
    (
        "CLAUDE_CODE_PROCESS_WRAPPER",
        "behaviour",
        "str",
        "Launch the processes Claude Code starts from its own binary, such as the background service that hosts agent view ses...",
    ),
    (
        "CLAUDE_CODE_PROJECT_DIR_NAME",
        "behaviour",
        "str",
        "Set together with CLAUDE_CONFIG_DIR to choose the projects/ directory name Claude Code stores that session's transcri...",
    ),
    (
        "CLAUDE_CODE_PROMPT_CACHE_TTL",
        "model",
        "ttl",
        "Set 5m or 1h, the only values Claude Code accepts, to choose the prompt cache TTL for the main conversation: your int...",
    ),
    (
        "CLAUDE_CODE_PROPAGATE_TRACEPARENT",
        "telemetry",
        "str",
        "Set to 1 to propagate W3C trace context when ANTHROPIC_BASE_URL points at a custom proxy",
    ),
    (
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
        "behaviour",
        "str",
        "Set by host platforms that embed Claude Code and manage model provider routing on its behalf",
    ),
    (
        "CLAUDE_CODE_PROXY_RESOLVES_HOSTS",
        "network",
        "bool",
        "Set to 1 to allow the proxy to perform DNS resolution instead of the caller",
    ),
    (
        "CLAUDE_CODE_REMOTE",
        "behaviour",
        "str",
        "Set automatically to true when Claude Code is running as a cloud session",
    ),
    (
        "CLAUDE_CODE_REMOTE_SESSION_ID",
        "behaviour",
        "str",
        "Set automatically in cloud sessions to the current session's ID. Read this to construct a link back to the session tr...",
    ),
    (
        "CLAUDE_CODE_RESTRICTED",
        "behaviour",
        "bool",
        "Set to 1 to start the session in restricted mode, the same as passing --restricted",
    ),
    (
        "CLAUDE_CODE_RESUME_INTERRUPTED_TURN",
        "behaviour",
        "str",
        "Set to 1 to automatically resume if the previous session ended mid-turn",
    ),
    (
        "CLAUDE_CODE_RESUME_INTERRUPTED_TURN_MAX_AGE_MS",
        "behaviour",
        "int",
        "Maximum age in milliseconds of the last transcript message for a session that ended mid-turn to continue automaticall...",
    ),
    (
        "CLAUDE_CODE_RESUME_PROMPT",
        "behaviour",
        "str",
        "Override the continuation message Claude Code sends to Claude when CLAUDE_CODE_RESUME_INTERRUPTED_TURN continues an i...",
    ),
    (
        "CLAUDE_CODE_RETRY_WATCHDOG",
        "behaviour",
        "bool",
        "Set to 1 for unattended sessions such as eval harnesses, CI jobs, or remote workers",
    ),
    (
        "CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS",
        "behaviour",
        "int",
        "Maximum time in milliseconds that each API request spends waiting out 429 and 529 errors when CLAUDE_CODE_RETRY_WATCH...",
    ),
    (
        "CLAUDE_CODE_SAFE_MODE",
        "behaviour",
        "bool",
        "Set to 1 to start in safe mode: CLAUDE.md, skills, plugins, hooks, MCP servers, custom commands and agents, output st...",
    ),
    (
        "CLAUDE_CODE_SCRIPT_CAPS",
        "behaviour",
        "str",
        "JSON object limiting how many times specific scripts may be invoked per session when CLAUDE_CODE_SUBPROCESS_ENV_SCRUB...",
    ),
    (
        "CLAUDE_CODE_SCROLL_SPEED",
        "behaviour",
        "str",
        "Set the mouse wheel scroll multiplier in fullscreen rendering",
    ),
    (
        "CLAUDE_CODE_SEND_FEEDBACK",
        "privacy",
        "bool",
        "Set to 0 to turn off Claude-drafted feedback for a session",
    ),
    (
        "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS",
        "behaviour",
        "int",
        "Override the time budget in milliseconds for SessionEnd hooks",
    ),
    (
        "CLAUDE_CODE_SESSION_ID",
        "behaviour",
        "str",
        "Set automatically to the current session ID in Bash and PowerShell tool subprocesses, hook command subprocesses, and ...",
    ),
    (
        "CLAUDE_CODE_SHELL",
        "behaviour",
        "str",
        "Set the shell Claude Code uses to run Bash tool commands",
    ),
    (
        "CLAUDE_CODE_SHELL_PREFIX",
        "behaviour",
        "str",
        "Command prefix that wraps shell commands Claude Code spawns: Bash tool calls, hook commands, status line commands, an...",
    ),
    (
        "CLAUDE_CODE_SIMPLE",
        "behaviour",
        "bool",
        "Set to 1 to run with a minimal system prompt and only the Bash, file read, and file edit tools",
    ),
    (
        "CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT",
        "behaviour",
        "bool",
        "Choose between Claude Code's full system prompt and a shorter one with abbreviated tool descriptions",
    ),
    (
        "CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH",
        "provider",
        "bool",
        "Skip client-side authentication for Claude Platform on AWS, for gateways that sign requests themselves",
    ),
    (
        "CLAUDE_CODE_SKIP_AWS_CRED_CACHE",
        "model",
        "str",
        "Set to 1 to turn off the in-process cache of credentials resolved from the AWS default credential provider chain, so ...",
    ),
    (
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "provider",
        "bool",
        "Skip AWS authentication for Amazon Bedrock (for example, when using an LLM gateway)",
    ),
    (
        "CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS",
        "network",
        "bool",
        "Set to 1 to treat a failed fast mode availability check as available, for networks that block the check's direct requ...",
    ),
    (
        "CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK",
        "model",
        "str",
        "Set to 1 to skip the client-side fast mode availability check, for proxies that intercept the check's request rather ...",
    ),
    (
        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
        "provider",
        "bool",
        "Skip Azure authentication for Microsoft Foundry, for a proxy or gateway that injects its own Authorization header",
    ),
    (
        "CLAUDE_CODE_SKIP_MANTLE_AUTH",
        "behaviour",
        "bool",
        "Skip AWS authentication for Amazon Bedrock Mantle (for example, when using an LLM gateway)",
    ),
    (
        "CLAUDE_CODE_SKIP_MODEL_ACCESS_MEMORY",
        "model",
        "str",
        "The startup model checks on Amazon Bedrock and Google Cloud's Agent Platform remember on this machine which models th...",
    ),
    (
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY",
        "privacy",
        "bool",
        "Set to 1 to skip writing prompt history and session transcripts to disk",
    ),
    (
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        "provider",
        "bool",
        "Skip Google authentication for Google Cloud's Agent Platform (for example, when using an LLM gateway)",
    ),
    (
        "CLAUDE_CODE_STARTUP_FAILURE_RESULTS",
        "behaviour",
        "bool",
        "Set to 1 to have a session started with --output-format stream-json write a result message naming why Claude Code ref...",
    ),
    (
        "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP",
        "behaviour",
        "str",
        "Maximum number of consecutive times a Stop or SubagentStop hook may block the turn from ending before Claude Code ove...",
    ),
    (
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "model",
        "str",
        "The default model for subagents, agent team teammates, and workflow agents that aren't assigned a model another way",
    ),
    (
        "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
        "model",
        "str",
        "Set to 1 to force one model onto subagents, teammates, and workflow agents",
    ),
    (
        "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL",
        "model",
        "str",
        "Set 5m or 1h, the only values Claude Code accepts, to choose the prompt cache TTL for requests outside the main conve...",
    ),
    (
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB",
        "behaviour",
        "str",
        "Set to 1 to strip credentials from the environments of the subprocesses Claude Code starts, such as Bash commands, ho...",
    ),
    (
        "CLAUDE_CODE_SYNC_PLUGIN_INSTALL",
        "plugins",
        "bool",
        "Set to 1 in non-interactive mode (the -p flag) to wait for plugin installation to complete before the first query",
    ),
    (
        "CLAUDE_CODE_SYNC_PLUGIN_INSTALL_TIMEOUT_MS",
        "plugins",
        "int",
        "Timeout in milliseconds for synchronous plugin installation",
    ),
    (
        "CLAUDE_CODE_SYNC_SKILLS",
        "behaviour",
        "bool",
        "Set to 1 in non-interactive mode with the -p flag to make Claude Code download the skills enabled for your claude.ai ...",
    ),
    (
        "CLAUDE_CODE_SYNC_SKILLS_INSTALL_TIMEOUT_MS",
        "behaviour",
        "int",
        "Timeout in milliseconds for the skills resync that runs mid-session when an app built on the Agent SDK reloads skills...",
    ),
    (
        "CLAUDE_CODE_SYNC_SKILLS_WAIT_TIMEOUT_MS",
        "behaviour",
        "int",
        "Timeout in milliseconds for the first query to wait for the initial skill list when CLAUDE_CODE_SYNC_SKILLS is set (d...",
    ),
    (
        "CLAUDE_CODE_SYNTAX_HIGHLIGHT",
        "behaviour",
        "str",
        "Set to false to disable syntax highlighting in diff output",
    ),
    ("CLAUDE_CODE_TASK_LIST_ID", "behaviour", "str", "Share a task list across sessions"),
    (
        "CLAUDE_CODE_TEAM_TEARDOWN_PARK_TIMEOUT_MS",
        "behaviour",
        "int",
        "Override, in milliseconds, how long a non-interactive session waits at exit for its agent team to finish tearing down",
    ),
    (
        "CLAUDE_CODE_TMPDIR",
        "behaviour",
        "str",
        "Override the temp directory used for internal temp files",
    ),
    (
        "CLAUDE_CODE_TMUX_TRUECOLOR",
        "behaviour",
        "str",
        "Set to any non-empty value, such as 1, to allow 24-bit truecolor output inside tmux",
    ),
    (
        "CLAUDE_CODE_TOOL_MEMORY_CGROUP_EXCLUDE",
        "privacy",
        "str",
        "On Linux and WSL, set to a comma-separated list of the kinds of processes Claude Code excludes from the tool memory c...",
    ),
    (
        "CLAUDE_CODE_TOOL_MEMORY_LIMIT",
        "privacy",
        "str",
        "On Linux and WSL, set to a size such as 4G to cap the memory that Bash and PowerShell tool commands can use, and Moni...",
    ),
    (
        "CLAUDE_CODE_TRANSCRIPT_LOCAL_GC",
        "behaviour",
        "bool",
        "Set to 1 to limit how large the transcript file of a long -p or Agent SDK session grows",
    ),
    (
        "CLAUDE_CODE_USER_DIALOG_TIMEOUT_MS",
        "behaviour",
        "int",
        "Deadline in milliseconds before Claude Code cancels a dialog it forwards to a remote client such as a Remote Control ...",
    ),
    ("CLAUDE_CODE_USE_ANTHROPIC_AWS", "provider", "bool", "Use Claude Platform on AWS"),
    ("CLAUDE_CODE_USE_BEDROCK", "provider", "bool", "Use Amazon Bedrock"),
    ("CLAUDE_CODE_USE_FOUNDRY", "provider", "bool", "Use Microsoft Foundry"),
    ("CLAUDE_CODE_USE_MANTLE", "provider", "str", "Use the Amazon Bedrock Mantle endpoint"),
    (
        "CLAUDE_CODE_USE_NATIVE_FILE_SEARCH",
        "provider",
        "str",
        "Set to 1 to discover custom commands, subagents, and output styles using Node.js file APIs instead of ripgrep",
    ),
    ("CLAUDE_CODE_USE_POWERSHELL_TOOL", "provider", "bool", "Controls the PowerShell tool"),
    ("CLAUDE_CODE_USE_VERTEX", "provider", "bool", "Use Google Cloud's Agent Platform"),
    (
        "CLAUDE_CODE_WEBFETCH_CACHE_TTL_MS",
        "model",
        "int",
        "Set to the number of milliseconds WebFetch keeps each fetched URL's response cached",
    ),
    (
        "CLAUDE_CODE_WEBFETCH_DEADLINE_MS",
        "behaviour",
        "int",
        "Upper bound in milliseconds on how long WebFetch waits for a page to download, including any redirects it follows",
    ),
    (
        "CLAUDE_CODE_WEB_SEARCH_REFILLS_PER_HOUR",
        "behaviour",
        "str",
        "Rate at which a session's WebSearch limit refills, in calls per hour",
    ),
    (
        "CLAUDE_CODE_WORKER_CHECKIN_SCHEDULE",
        "behaviour",
        "bool",
        "When CLAUDE_AUTO_BACKGROUND_TASKS is set to 1, how long Claude Code waits before each reminder to Claude to check on ...",
    ),
    (
        "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS",
        "behaviour",
        "int",
        "How many agents a single workflow run executes at once, from 1 to 256. By default, a run executes up to 16 agents at ...",
    ),
    (
        "CLAUDE_CODE_WORKFLOW_PREFIX_STAGGER_MS",
        "behaviour",
        "int",
        "Upper bound in milliseconds on how long a workflow agent waits for a same-prefix sibling's first response to begin be...",
    ),
    (
        "CLAUDE_CODE_YOU_SHOULD_KNOW_DEBUG",
        "behaviour",
        "bool",
        "Set to 1 to show, above the prompt, a line for each of the last three checks the You should know mod made in this ses...",
    ),
    (
        "CLAUDE_CONFIG_DIR",
        "behaviour",
        "str",
        "Override the configuration directory (default: ~/.claude)",
    ),
    (
        "CLAUDE_DISABLE_ADOPT",
        "behaviour",
        "bool",
        "Set to 1 to stop in-flight background work instead of carrying it over when you background a session by pressing  or ...",
    ),
    (
        "CLAUDE_EFFORT",
        "model",
        "str",
        "Set automatically in Bash tool subprocesses and hook commands to the effort level in effect when the subprocess start...",
    ),
    (
        "CLAUDE_ENABLE_BYTE_WATCHDOG",
        "behaviour",
        "str",
        "Set to 1 to force-enable the byte-level streaming idle watchdog, or set to 0 to force-disable it",
    ),
    (
        "CLAUDE_ENABLE_BYTE_WATCHDOG_BEDROCK",
        "provider",
        "str",
        "Set to 1 to enable the byte-level streaming idle watchdog on Amazon Bedrock vnd.amazon.eventstream responses, which a...",
    ),
    (
        "CLAUDE_ENABLE_STREAM_WATCHDOG",
        "behaviour",
        "str",
        "Set to 0 to force-disable the event-level streaming idle watchdog, or set to 1 to force-enable it",
    ),
    (
        "CLAUDE_ENV_FILE",
        "behaviour",
        "str",
        "Path to a shell script whose contents Claude Code runs before each Bash command in the same shell process, so exports...",
    ),
    (
        "CLAUDE_JOB_DIR",
        "behaviour",
        "str",
        "Set by Claude Code in each background session to that session's ~/.claude/jobs/<id> directory",
    ),
    (
        "CLAUDE_PID",
        "behaviour",
        "str",
        "Claude Code sets this to its own process ID in the subprocesses it spawns: Bash and PowerShell tool commands and hook...",
    ),
    (
        "CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX",
        "behaviour",
        "str",
        "Prefix for auto-generated Remote Control session names when no explicit name is provided",
    ),
    (
        "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS",
        "behaviour",
        "int",
        "Deadline in milliseconds for the first response byte of a streaming request, on the connections where the first-byte ...",
    ),
    (
        "CLAUDE_STREAM_IDLE_TIMEOUT_MS",
        "behaviour",
        "int",
        "Timeout in milliseconds before the event- and byte-level streaming idle watchdogs close a stalled connection",
    ),
    ("CLAUDE_SUBAGENT_BG_SHELL_MAX_MS", "behaviour", "str", "Removed in v2.1.260 and now a no-op"),
    ("CLOUD_ML_REGION", "provider", "str", "Google Cloud region for Vertex AI"),
    (
        "DEBUG",
        "behaviour",
        "bool",
        "Set to 1 to enable debug mode, equivalent to launching with --debug",
    ),
    ("DISABLE_AUTOUPDATER", "privacy", "bool", "Set to 1 to disable automatic background updates"),
    (
        "DISABLE_AUTO_COMPACT",
        "model",
        "str",
        "Set to 1 to disable automatic compaction when approaching the context limit, along with idle compaction",
    ),
    (
        "DISABLE_COMPACT",
        "model",
        "str",
        "Set to 1 to disable all compaction: both automatic compaction and the manual /compact command",
    ),
    ("DISABLE_COST_WARNINGS", "privacy", "bool", "Set to 1 to disable cost warning messages"),
    (
        "DISABLE_DOCTOR_COMMAND",
        "behaviour",
        "str",
        "Set to 1 to hide the /doctor setup checkup skill and its /checkup alias",
    ),
    (
        "DISABLE_ERROR_REPORTING",
        "privacy",
        "bool",
        "Set to any non-empty value, such as 1, to opt out of error reporting",
    ),
    (
        "DISABLE_EXTRA_USAGE_COMMAND",
        "behaviour",
        "str",
        "Set to 1 to hide the /usage-credits command that lets users purchase additional usage beyond rate limits",
    ),
    (
        "DISABLE_FEEDBACK_COMMAND",
        "privacy",
        "str",
        "Set to 1 to disable the /feedback command and Claude-drafted feedback",
    ),
    (
        "DISABLE_GROWTHBOOK",
        "behaviour",
        "bool",
        "Set to 1 or true to disable GrowthBook feature-flag fetching and use code defaults for every flag",
    ),
    (
        "DISABLE_INSTALLATION_CHECKS",
        "behaviour",
        "bool",
        "Set to 1 to disable installation warnings",
    ),
    (
        "DISABLE_INSTALL_GITHUB_APP_COMMAND",
        "behaviour",
        "str",
        "Set to 1 to hide the /install-github-app command",
    ),
    (
        "DISABLE_INTERLEAVED_THINKING",
        "model",
        "bool",
        "Set to 1 to prevent sending the interleaved-thinking beta header",
    ),
    ("DISABLE_LOGIN_COMMAND", "auth", "str", "Set to 1 to hide the /login command"),
    ("DISABLE_LOGOUT_COMMAND", "behaviour", "str", "Set to 1 to hide the /logout command"),
    (
        "DISABLE_PROMPT_CACHING",
        "behaviour",
        "str",
        "Set to 1 to disable prompt caching for all models (takes precedence over per-model settings)",
    ),
    (
        "DISABLE_PROMPT_CACHING_FABLE",
        "behaviour",
        "str",
        "Set to 1 to disable prompt caching for Fable models",
    ),
    (
        "DISABLE_PROMPT_CACHING_HAIKU",
        "behaviour",
        "str",
        "Set to 1 to disable prompt caching for the default Haiku model, wherever it runs",
    ),
    (
        "DISABLE_PROMPT_CACHING_OPUS",
        "behaviour",
        "str",
        "Set to 1 to disable prompt caching for the default Opus model",
    ),
    (
        "DISABLE_PROMPT_CACHING_SONNET",
        "behaviour",
        "str",
        "Set to 1 to disable prompt caching for the default Sonnet model",
    ),
    (
        "DISABLE_TELEMETRY",
        "telemetry",
        "bool",
        "Set to any non-empty value, such as 1, to opt out of telemetry",
    ),
    (
        "DISABLE_UPDATES",
        "behaviour",
        "bool",
        "Set to 1 to block all updates including manual claude update and claude install",
    ),
    ("DISABLE_UPGRADE_COMMAND", "behaviour", "str", "Set to 1 to hide the /upgrade command"),
    (
        "DO_NOT_TRACK",
        "behaviour",
        "bool",
        "Set to 1 to opt out of telemetry, with the same effect as DISABLE_TELEMETRY, including on feature-flag fetching",
    ),
    (
        "ENABLE_BETA_TRACING_DETAILED",
        "behaviour",
        "bool",
        "Set to 1, and set BETA_TRACING_ENDPOINT to your OTLP/HTTP collector endpoint, to turn on detailed beta tracing, which...",
    ),
    (
        "ENABLE_CLAUDEAI_MCP_SERVERS",
        "mcp",
        "word",
        "Set to false to stop Claude Code from fetching claude.ai MCP servers",
    ),
    (
        "ENABLE_PROMPT_CACHING_1H",
        "behaviour",
        "bool",
        "Set to 1 to request a 1-hour prompt cache TTL instead of the default 5 minutes",
    ),
    ("ENABLE_PROMPT_CACHING_1H_BEDROCK", "provider", "str", "Deprecated"),
    ("ENABLE_TOOL_SEARCH", "behaviour", "str", "Controls MCP tool search"),
    (
        "FALLBACK_FOR_ALL_PRIMARY_MODELS",
        "model",
        "str",
        "Set to any non-empty value, such as 1, to make Claude Code stop retrying on repeated overload errors for every model ...",
    ),
    (
        "FORCE_AUTOUPDATE_PLUGINS",
        "plugins",
        "bool",
        "Set to 1 to force plugin auto-updates even when the main auto-updater is disabled via DISABLE_AUTOUPDATER",
    ),
    (
        "FORCE_HYPERLINK",
        "behaviour",
        "bool",
        "Set to 1 to enable clickable OSC 8 hyperlinks when your terminal supports them but isn't auto-detected, or 0 to disab...",
    ),
    (
        "FORCE_PROMPT_CACHING_5M",
        "behaviour",
        "bool",
        "Set to 1 to force the 5-minute prompt cache TTL even when 1-hour TTL would otherwise apply",
    ),
    ("HTTPS_PROXY", "network", "str", "Specify HTTPS proxy server for network connections"),
    ("HTTP_PROXY", "network", "str", "Specify HTTP proxy server for network connections"),
    (
        "IS_DEMO",
        "behaviour",
        "str",
        "Set to any non-empty value, such as 1, to enable demo mode: hides your email and organization name from the header an...",
    ),
    (
        "MAX_MCP_OUTPUT_TOKENS",
        "mcp",
        "int",
        "Maximum number of tokens allowed in MCP tool responses (default: 25000)",
    ),
    (
        "MAX_STRUCTURED_OUTPUT_RETRIES",
        "behaviour",
        "int",
        "Number of attempts Claude Code allows when the model's response fails validation against the --json-schema in non-int...",
    ),
    ("MAX_THINKING_TOKENS", "model", "int", "Fixed token budget for extended thinking"),
    (
        "MCP_CLIENT_SECRET",
        "mcp",
        "secret",
        "OAuth client secret for MCP servers that require pre-configured credentials",
    ),
    (
        "MCP_CONNECTION_NONBLOCKING",
        "mcp",
        "str",
        "Controls whether startup waits for MCP servers to connect before the first query",
    ),
    (
        "MCP_CONNECT_TIMEOUT_MS",
        "mcp",
        "int",
        "How long blocking MCP startup waits, in milliseconds, for the connection batch before snapshotting the tool list (def...",
    ),
    ("MCP_DISCOVERY_CACHE", "mcp", "bool", "Turns the MCP discovery cache on or off"),
    (
        "MCP_DISCOVERY_CACHE_MAX_STALE_S",
        "mcp",
        "int",
        "Maximum age, in seconds, of a discovery-cache entry (default: 14400, or 4 hours)",
    ),
    (
        "MCP_DISCOVERY_CACHE_STRIKES",
        "mcp",
        "str",
        "At a start where a discovery-cache entry is older than MCP_DISCOVERY_CACHE_TTL_S, Claude Code refreshes it in the bac...",
    ),
    (
        "MCP_DISCOVERY_CACHE_TTL_S",
        "mcp",
        "str",
        "Seconds for which Claude Code uses a discovery-cache entry without refreshing it (default: 900)",
    ),
    (
        "MCP_OAUTH_CALLBACK_PORT",
        "auth",
        "int",
        "Fixed port for the OAuth redirect callback, as an alternative to --callback-port when adding an MCP server with pre-c...",
    ),
    (
        "MCP_PROTOCOL_NEGOTIATION",
        "mcp",
        "str",
        "On the v2 MCP client runtime only, whether Claude Code probes servers for MCP protocol revision 2026-07-28. Set auto ...",
    ),
    (
        "MCP_REMOTE_SERVER_CONNECTION_BATCH_SIZE",
        "mcp",
        "int",
        "Maximum number of remote MCP servers (HTTP/SSE) to connect in parallel during startup (default: 20)",
    ),
    (
        "MCP_SDK_GENERATION",
        "mcp",
        "str",
        "Pin which MCP client runtime this process connects to MCP servers with: v1, built on MCP TypeScript SDK 1.x, or v2, b...",
    ),
    (
        "MCP_SERVER_CONNECTION_BATCH_SIZE",
        "mcp",
        "int",
        "Maximum number of local MCP servers (stdio) to connect in parallel during startup (default: 3)",
    ),
    (
        "MCP_TIMEOUT",
        "mcp",
        "int",
        "Timeout in milliseconds for MCP server startup (default: 30000, or 30 seconds)",
    ),
    (
        "MCP_TOOL_TIMEOUT",
        "mcp",
        "int",
        "Timeout in milliseconds for MCP tool execution (default: 100000000, about 28 hours)",
    ),
    (
        "NO_PROXY",
        "network",
        "str",
        "List of domains and IPs to which requests will be directly issued, bypassing proxy",
    ),
    (
        "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT",
        "telemetry",
        "int",
        "Standard OpenTelemetry SDK limit on attribute value length",
    ),
    (
        "OTEL_LOG_ASSISTANT_RESPONSES",
        "telemetry",
        "str",
        "Set to 1 to include the model's response text on assistant_response OpenTelemetry log events",
    ),
    (
        "OTEL_LOG_MANAGED_SETTINGS",
        "telemetry",
        "bool",
        "Set to 1 to add the redacted managed settings, and a SHA-256 digest of the settings before redaction, to managed_sett...",
    ),
    (
        "OTEL_LOG_RAW_API_BODIES",
        "telemetry",
        "bool",
        "Emit Anthropic Messages API request and response JSON as api_request_body / api_response_body log events",
    ),
    (
        "OTEL_LOG_TOOL_CONTENT",
        "telemetry",
        "bool",
        "Set to 1 to include tool content in the tool.output OpenTelemetry span event",
    ),
    (
        "OTEL_LOG_TOOL_DETAILS",
        "telemetry",
        "str",
        "Set to 1 to include tool input arguments; MCP server names; user-authored workflow names; raw error strings on tool f...",
    ),
    (
        "OTEL_LOG_USER_PROMPTS",
        "telemetry",
        "bool",
        "Set to 1 to include user prompt text in OpenTelemetry traces and logs",
    ),
    (
        "OTEL_METRICS_INCLUDE_ACCOUNT_UUID",
        "telemetry",
        "str",
        "Set to false to exclude account UUID from metrics attributes (default: included)",
    ),
    (
        "OTEL_METRICS_INCLUDE_ENTRYPOINT",
        "telemetry",
        "bool",
        "Set to true to include the session entrypoint in metrics attributes (default: excluded)",
    ),
    (
        "OTEL_METRICS_INCLUDE_REPOSITORY",
        "telemetry",
        "bool",
        "Set to true to tag OpenTelemetry metrics and events with vcs.* attributes identifying the session's repository (defau...",
    ),
    (
        "OTEL_METRICS_INCLUDE_RESOURCE_ATTRIBUTES",
        "telemetry",
        "str",
        "As of v2.1.161, Claude Code attaches OTEL_RESOURCE_ATTRIBUTES keys to metric datapoint labels",
    ),
    (
        "OTEL_METRICS_INCLUDE_SESSION_ID",
        "telemetry",
        "str",
        "Set to false to exclude session ID from metrics attributes (default: included)",
    ),
    (
        "OTEL_METRICS_INCLUDE_VERSION",
        "telemetry",
        "bool",
        "Set to true to include Claude Code version in metrics attributes (default: excluded)",
    ),
    (
        "SLASH_COMMAND_TOOL_CHAR_BUDGET",
        "behaviour",
        "str",
        "Override the character budget for skill metadata shown to the Skill tool",
    ),
    (
        "TASK_MAX_OUTPUT_LENGTH",
        "behaviour",
        "str",
        "Removed in v2.1.277 and now a no-op, together with the TaskOutput tool it sized",
    ),
    (
        "USE_BUILTIN_RIPGREP",
        "provider",
        "str",
        "Set to 0 to use system-installed rg instead of rg included with Claude Code",
    ),
    (
        "VERTEX_REGION_CLAUDE_3_5_HAIKU",
        "provider",
        "str",
        "Override region for Claude 3.5 Haiku when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_3_5_SONNET",
        "provider",
        "str",
        "Override region for Claude 3.5 Sonnet when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_3_7_SONNET",
        "provider",
        "str",
        "Override region for Claude 3.7 Sonnet when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_4_0_OPUS",
        "provider",
        "str",
        "Override region for Claude Opus 4 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_4_0_SONNET",
        "provider",
        "str",
        "Override region for Claude Sonnet 4 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_4_1_OPUS",
        "provider",
        "str",
        "Override region for Claude Opus 4.1 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_4_5_OPUS",
        "provider",
        "str",
        "Override region for Claude Opus 4.5 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_4_5_SONNET",
        "provider",
        "str",
        "Override region for Claude Sonnet 4.5 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_4_6_OPUS",
        "provider",
        "str",
        "Override region for Claude Opus 4.6 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_4_6_SONNET",
        "provider",
        "str",
        "Override region for Claude Sonnet 4.6 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_4_7_OPUS",
        "provider",
        "str",
        "Override region for Claude Opus 4.7 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_4_8_OPUS",
        "provider",
        "str",
        "Override region for Claude Opus 4.8 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_5_5_OPUS",
        "provider",
        "str",
        "Override region for Claude Opus 5.5 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_5_5_SONNET",
        "provider",
        "str",
        "Override region for Claude Sonnet 5.5 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_5_OPUS",
        "provider",
        "str",
        "Override region for Claude Opus 5 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_5_SONNET",
        "provider",
        "str",
        "Override region for Claude Sonnet 5 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_FABLE_5",
        "provider",
        "str",
        "Override region for Claude Fable 5 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_FABLE_5_1",
        "provider",
        "str",
        "Override region for Claude Fable 5.1 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_HAIKU_4_5",
        "provider",
        "str",
        "Override region for Claude Haiku 4.5 when using Google Cloud's Agent Platform",
    ),
    (
        "VERTEX_REGION_CLAUDE_HAIKU_5_5",
        "provider",
        "str",
        "Override region for Claude Haiku 5.5 when using Google Cloud's Agent Platform",
    ),
)

_Str = Annotated[str, StringConstraints(min_length=1)]
_ANNOTATIONS: dict[str, Any] = {
    "secret": SecretStr,
    "bool": bool,
    "int": Annotated[int, Field(gt=0)],
    "pct": Annotated[int, Field(ge=1, le=100)],
    "ttl": Annotated[str, StringConstraints(pattern=r"^[0-9]+[smh]$")],
    "word": Annotated[str, StringConstraints(pattern=r"^(true|false)$")],
    "str": _Str,
}
_PUBLIC_TYPE: dict[str, VarType] = {
    "secret": "secret", "bool": "bool", "int": "int", "pct": "int", "ttl": "str",
    "word": "str", "str": "str",
}  # fmt: skip

_SPECS: tuple[EnvVarSpec, ...] = tuple(
    EnvVarSpec(
        field=env.lower(),
        env=env,
        group=group,  # type: ignore[arg-type]
        secret=kind == "secret",
        managed=env in MANAGED_ENV,
        type=_PUBLIC_TYPE[kind],
        description=desc,
    )
    for env, group, kind, desc in _TABLE
)
_KIND = {env.lower(): kind for env, _, kind, _ in _TABLE}
_BY_FIELD = {s.field: s for s in _SPECS}
_MODELLED_ENV = frozenset(s.env for s in _SPECS)


class _ClaudeEnvBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extra: dict[str, str] = Field(
        default_factory=dict,
        description="Additional env vars passed through verbatim (names match [A-Z_][A-Z0-9_]*)",
    )

    @field_validator("extra")
    @classmethod
    def _check_extra(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if not EXTRA_KEY_RE.match(key):
                raise ValueError(f"extra key {key!r} must match ^[A-Z_][A-Z0-9_]*$")
            if key in MANAGED_ENV:
                raise ValueError(f"extra key {key} is managed by the service and cannot be set")
            if key in _MODELLED_ENV:
                raise ValueError(f"extra key {key} is modelled; use the field {key.lower()!r}")
            if key.startswith("CCW_") or key in RESERVED_EXTRA:
                raise ValueError(f"extra key {key} is reserved (CCW_*, TERM, HOME)")
        return value

    @model_validator(mode="after")
    def _check_model(self) -> _ClaudeEnvBase:
        if getattr(self, "claude_config_dir", None) is not None:
            raise ValueError(
                "CLAUDE_CONFIG_DIR is set per session by the service from paths.sessions_root"
            )
        if sum(getattr(self, f) is True for f in PROVIDER_FLAGS) > 1:
            raise ValueError(
                "at most one of claude_code_use_bedrock, claude_code_use_vertex, "
                "claude_code_use_foundry, claude_code_use_anthropic_aws may be true"
            )
        return self

    @classmethod
    def catalogue(cls) -> list[EnvVarSpec]:
        return list(_SPECS)

    def _set_values(self) -> dict[str, Any]:
        return {f: v for f in _BY_FIELD if (v := getattr(self, f)) is not None}

    def warnings(self) -> list[str]:
        if self.claude_code_oauth_token and self.anthropic_api_key:  # type: ignore[attr-defined]
            return [
                "both CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY are set; "
                "the OAuth token wins and ANTHROPIC_API_KEY is dropped"
            ]
        return []

    def to_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for name, value in self._set_values().items():
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            elif value is False:
                continue
            elif value is True:
                value = "1"
            env[_BY_FIELD[name].env] = str(value)
        if self.warnings():
            env.pop("ANTHROPIC_API_KEY", None)
        env.update(self.extra)
        return env

    def merged(self, override: _ClaudeEnvBase) -> _ClaudeEnvBase:
        data: dict[str, Any] = {f: getattr(self, f) for f in self.model_fields_set}
        data.update({f: getattr(override, f) for f in override.model_fields_set})
        data["extra"] = {**self.extra, **override.extra}
        return type(self)(**data)

    def redacted(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            name: MASK if isinstance(value, SecretStr) else value
            for name, value in self._set_values().items()
        }
        if self.extra:
            out["extra"] = {
                k: MASK if SECRET_KEY_RE.search(k) else v for k, v in self.extra.items()
            }
        return out

    def secret_values(self) -> list[str]:
        values = [
            v.get_secret_value() for v in self._set_values().values() if isinstance(v, SecretStr)
        ]
        values.extend(v for k, v in self.extra.items() if SECRET_KEY_RE.search(k) and v)
        return values


ClaudeEnv = create_model(  # type: ignore[call-overload]
    "ClaudeEnv",
    __base__=_ClaudeEnvBase,
    __module__=__name__,
    **{
        s.field: (
            _ANNOTATIONS[_KIND[s.field]] | None,
            Field(
                default=None,
                description=s.description,
                json_schema_extra={
                    "env": s.env,
                    "group": s.group,
                    "secret": s.secret,
                    "managed": s.managed,
                    "type": s.type,
                },
            ),
        )
        for s in _SPECS
    },
)
