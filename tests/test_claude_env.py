import pytest
from pydantic import SecretStr, ValidationError

from claude_code_wrapper.claude_env import ClaudeEnv

REQUIRED = [
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "AWS_REGION",
    "AWS_PROFILE",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "CLOUD_ML_REGION",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "MAX_THINKING_TOKENS",
    "BASH_DEFAULT_TIMEOUT_MS",
    "BASH_MAX_TIMEOUT_MS",
    "BASH_MAX_OUTPUT_LENGTH",
    "MCP_TIMEOUT",
    "MCP_TOOL_TIMEOUT",
    "MAX_MCP_OUTPUT_TOKENS",
    "CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH",
    "DISABLE_TELEMETRY",
    "DISABLE_ERROR_REPORTING",
    "DISABLE_AUTOUPDATER",
    "DISABLE_COST_WARNINGS",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
    "CLAUDE_CODE_PROMPT_CACHE_TTL",
    "ENABLE_CLAUDEAI_MCP_SERVERS",
    "CLAUDE_CODE_SYNC_PLUGIN_INSTALL",
    "CLAUDE_CODE_PLUGIN_SEED_DIR",
    "CLAUDE_CODE_SKIP_PROMPT_HISTORY",
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
]

GROUPS = {
    "auth",
    "provider",
    "model",
    "behaviour",
    "mcp",
    "network",
    "telemetry",
    "plugins",
    "privacy",
}


def test_catalogue_covers_required():
    cat = {s.env: s for s in ClaudeEnv.catalogue()}
    assert set(REQUIRED) <= set(cat)
    for s in cat.values():
        assert s.field == s.env.lower()
        assert s.group in GROUPS
        assert s.type in {"str", "bool", "int", "secret"}
        assert s.description and s.description.isascii()
        assert s.field in ClaudeEnv.model_fields
    assert len(cat) > 300
    assert "DISABLE_NON_ESSENTIAL_MODEL_CALLS" not in cat


def test_catalogue_flags():
    cat = {s.env: s for s in ClaudeEnv.catalogue()}
    for env in [
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_FOUNDRY_API_KEY",
    ]:
        assert cat[env].secret and cat[env].type == "secret"
    assert cat["DISABLE_TELEMETRY"].type == "bool" and not cat["DISABLE_TELEMETRY"].secret
    assert cat["MCP_TIMEOUT"].type == "int"
    assert cat["CLAUDE_CONFIG_DIR"].managed
    assert sum(s.managed for s in cat.values()) == 1


def test_secret_fields_are_secretstr():
    e = ClaudeEnv(claude_code_oauth_token="tok-123456789")
    assert isinstance(e.claude_code_oauth_token, SecretStr)
    assert "tok-123456789" not in repr(e)


def test_to_env_rendering():
    e = ClaudeEnv(
        disable_telemetry=True,
        disable_autoupdater=False,
        mcp_timeout=500,
        anthropic_model="opus",
        extra={"FOO": "bar"},
    )
    assert e.to_env() == {
        "DISABLE_TELEMETRY": "1",
        "MCP_TIMEOUT": "500",
        "ANTHROPIC_MODEL": "opus",
        "FOO": "bar",
    }


@pytest.mark.parametrize(
    "kw",
    [
        {"mcp_timeout": 0},
        {"mcp_timeout": -1},
        {"claude_autocompact_pct_override": 0},
        {"claude_autocompact_pct_override": 101},
        {"claude_code_prompt_cache_ttl": "forever"},
        {"enable_claudeai_mcp_servers": "maybe"},
        {"unknown_field": 1},
    ],
)
def test_invalid_values(kw):
    with pytest.raises(ValidationError):
        ClaudeEnv(**kw)


def test_word_values_ok():
    e = ClaudeEnv(
        claude_code_prompt_cache_ttl="1h",
        enable_claudeai_mcp_servers="false",
        claude_autocompact_pct_override=50,
    )
    assert e.to_env() == {
        "CLAUDE_CODE_PROMPT_CACHE_TTL": "1h",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50",
    }


def test_config_dir_managed():
    with pytest.raises(
        ValidationError, match=r"CLAUDE_CONFIG_DIR is set per session.*sessions_root"
    ):
        ClaudeEnv(claude_config_dir="/x")


@pytest.mark.parametrize(
    "key,msg",
    [
        ("lower", "match"),
        ("ANTHROPIC_MODEL", "anthropic_model"),
        ("CLAUDE_CONFIG_DIR", "managed"),
        ("CCW_FOO", "CCW_"),
        ("TERM", "TERM"),
        ("HOME", "HOME"),
    ],
)
def test_extra_rejected(key, msg):
    with pytest.raises(ValidationError, match=msg):
        ClaudeEnv(extra={key: "v"})


def test_oauth_wins_over_api_key():
    e = ClaudeEnv(claude_code_oauth_token="oauth-token-1", anthropic_api_key="sk-ant-xxxxxxxx")
    env = e.to_env()
    assert "ANTHROPIC_API_KEY" not in env and env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token-1"
    assert any("OAuth" in w for w in e.warnings())
    assert ClaudeEnv(anthropic_api_key="k" * 10).warnings() == []


def test_provider_exclusive():
    ClaudeEnv(claude_code_use_bedrock=True, claude_code_use_vertex=False)
    with pytest.raises(ValidationError):
        ClaudeEnv(claude_code_use_bedrock=True, claude_code_use_vertex=True)
    with pytest.raises(ValidationError):
        ClaudeEnv(claude_code_use_foundry=True, claude_code_use_anthropic_aws=True)


def test_merged():
    base = ClaudeEnv(
        disable_telemetry=True, anthropic_model="a", mcp_timeout=1, extra={"A": "1", "B": "2"}
    )
    over = ClaudeEnv(disable_telemetry=False, anthropic_model="b", extra={"B": "3", "C": "4"})
    m = base.merged(over)
    assert m.to_env() == {"ANTHROPIC_MODEL": "b", "MCP_TIMEOUT": "1", "A": "1", "B": "3", "C": "4"}
    assert base.to_env()["DISABLE_TELEMETRY"] == "1"  # originals untouched


def test_redacted_and_secret_values():
    e = ClaudeEnv(
        claude_code_oauth_token="oauth-token-1",
        anthropic_model="m",
        extra={"MY_TOKEN": "tokval", "PLAIN": "p", "DB_PASSWORD": "pw"},
    )
    r = e.redacted()
    assert r["claude_code_oauth_token"] == "***"
    assert r["anthropic_model"] == "m"
    assert r["extra"] == {"MY_TOKEN": "***", "PLAIN": "p", "DB_PASSWORD": "***"}
    assert sorted(e.secret_values()) == sorted(["oauth-token-1", "tokval", "pw"])
