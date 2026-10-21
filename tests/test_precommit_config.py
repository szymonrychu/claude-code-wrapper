"""The pre-commit gate: config shape and the mise task that runs it."""

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())

REQUIRED_IDS = {
    "trailing-whitespace",
    "end-of-file-fixer",
    "check-yaml",
    "check-added-large-files",
    "check-merge-conflict",
    "ruff",
    "ruff-format",
    "shellcheck",
    "conventional-pre-commit",
    "uv-lockfile",
}


def _hooks():
    return [h for repo in CONFIG["repos"] for h in repo["hooks"]]


def test_config_hooks_and_pinned_revs():
    test_every_rev_is_a_pinned_tag()
    test_required_hook_ids_present()
    test_ruff_rev_matches_uv_lock()
    test_uv_lockfile_hook_and_commit_msg_stage()


def test_every_rev_is_a_pinned_tag():
    for repo in CONFIG["repos"]:
        if repo["repo"] == "local":
            continue
        rev = str(repo["rev"])
        assert rev not in {"main", "master", "HEAD"}, repo["repo"]
        assert rev[0] in "v0123456789", repo["repo"]


def test_required_hook_ids_present():
    assert {h["id"] for h in _hooks()} >= REQUIRED_IDS


def test_ruff_rev_matches_uv_lock():
    lock = (ROOT / "uv.lock").read_text()
    version = lock.split('name = "ruff"\nversion = "')[1].split('"')[0]
    ruff = next(r for r in CONFIG["repos"] if "ruff-pre-commit" in r["repo"])
    assert ruff["rev"] == f"v{version}"


def test_uv_lockfile_hook_and_commit_msg_stage():
    hooks = {h["id"]: h for h in _hooks()}
    lock = hooks["uv-lockfile"]
    assert lock["entry"] == "uv lock --check"
    assert lock["language"] == "system"
    assert lock["pass_filenames"] is False
    assert "commit-msg" in hooks["conventional-pre-commit"]["stages"]


def test_mise_precommit_task():
    mise = tomllib.loads((ROOT / "mise.toml").read_text())
    assert "pre-commit run" in mise["tasks"]["precommit"]["run"]
