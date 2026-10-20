from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from claude_code_wrapper.config import PluginsConfig
from claude_code_wrapper.plugins import PluginInstallError, install_plugins

FAKE = r"""#!{py}
import json, os, sys
cfg = os.environ["CLAUDE_CONFIG_DIR"]
assert os.environ["DISABLE_AUTOUPDATER"] == "1"
state_p = os.path.join(cfg, "state.json")
state = json.load(open(state_p)) if os.path.exists(state_p) else {{"m": [], "p": []}}
with open(os.path.join(cfg, "calls.log"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
a = sys.argv[1:]
fail = os.environ.get("FAIL_ON", "")
if fail and fail in " ".join(a):
    print("boom token=" + os.environ.get("SECRET", ""))
    sys.exit(3)
if a[:3] == ["plugin", "marketplace", "list"]:
    print(json.dumps([{{"name": n}} for n in state["m"]]))
elif a[:3] == ["plugin", "marketplace", "add"]:
    state["m"].append(os.environ.get("MKT_NAME") or a[3].rsplit("/", 1)[-1])
elif a[:2] == ["plugin", "list"]:
    print(json.dumps([{{"id": i, "enabled": False}} for i in state["p"]]))
elif a[:2] == ["plugin", "install"]:
    if not os.environ.get("NOOP_INSTALL"):
        state["p"].append(a[2])
json.dump(state, open(state_p, "w"))
"""


@pytest.fixture
def claude(tmp_path: Path) -> Path:
    p = tmp_path / "claude"
    p.write_text(FAKE.format(py=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def calls(cfg: Path) -> list[list[str]]:
    f = cfg / "calls.log"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def run_cfg(tmp_path: Path, mkt_dir: Path | None = None, **kw) -> PluginsConfig:
    src = str(mkt_dir) if mkt_dir else "owner/repo"
    return PluginsConfig.model_validate(
        {
            "marketplaces": [{"name": "wb", "source": src}],
            "install": [{"plugin": "workbench@wb", **kw}],
        }
    )


async def go(cfg, claude, tmp_path, env=None):
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    return await install_plugins(
        cfg, claude_binary=str(claude), config_dir=conf, workdir=tmp_path, env=env or {}
    ), conf


async def test_order_and_idempotent(claude, tmp_path):
    mk = tmp_path / "wb"
    mk.mkdir()
    cfg = run_cfg(tmp_path, mk)
    report, conf = await go(cfg, claude, tmp_path, {"MKT_NAME": "wb"})
    assert report.installed == ["workbench@wb"]
    assert [" ".join(c[:3]) for c in calls(conf)] == [
        "plugin marketplace list",
        "plugin marketplace add",
        "plugin list --json",
        "plugin install workbench@wb",
        "plugin list --json",
    ]
    assert calls(conf)[1] == ["plugin", "marketplace", "add", str(mk), "--scope", "user", "--json"]
    assert calls(conf)[3] == ["plugin", "install", "workbench@wb", "-s", "user", "-y", "--json"]
    assert [c[:2] for c in calls(conf)].count(["plugin", "install"]) == 1
    n = len(calls(conf))
    report2, _ = await go(cfg, claude, tmp_path, {"MKT_NAME": "wb"})
    assert report2.installed == [] and report2.already_present == ["workbench@wb"]
    new = calls(conf)[n:]
    assert new and all(
        c[:2] == ["plugin", "list"] or c[:3] == ["plugin", "marketplace", "list"] for c in new
    )


async def test_missing_local_dir_raises_before_running(claude, tmp_path):
    cfg = run_cfg(tmp_path, tmp_path / "nope")
    with pytest.raises(PluginInstallError):
        await go(cfg, claude, tmp_path)
    assert calls(tmp_path / "conf") == []


async def test_required_failure_redacted(claude, tmp_path):
    cfg = run_cfg(tmp_path)
    with pytest.raises(PluginInstallError) as ei:
        await go(cfg, claude, tmp_path, {"FAIL_ON": "plugin install", "SECRET": "topsecretvalue1"})
    e = ei.value
    assert e.plugin_id == "workbench@wb" and e.exit_code == 3
    assert "topsecretvalue1" not in e.output_tail and "boom" in e.output_tail


async def test_optional_failure_warns(claude, tmp_path):
    cfg = run_cfg(tmp_path, required=False)
    report, _ = await go(cfg, claude, tmp_path, {"FAIL_ON": "plugin install"})
    assert report.installed == [] and len(report.warnings) == 1


async def test_required_missing_after_install_raises(claude, tmp_path):
    cfg = run_cfg(tmp_path)
    with pytest.raises(PluginInstallError):
        await go(cfg, claude, tmp_path, {"NOOP_INSTALL": "1"})


async def test_timeout(claude, tmp_path):
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    cfg2 = PluginsConfig.model_validate(
        {"marketplaces": [{"name": "m", "source": "o/r"}], "timeout_seconds": 0.2}
    )
    with pytest.raises(PluginInstallError):
        await install_plugins(
            cfg2, claude_binary=str(slow), config_dir=tmp_path, workdir=tmp_path, env={}
        )
