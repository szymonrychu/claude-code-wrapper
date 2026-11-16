import logging
import stat
from pathlib import Path

import pytest

from claude_code_wrapper.bootstrap import ensure_config_dir, link_shared_dirs
from claude_code_wrapper.config import PathsConfig

NAMES = ("skills", "rules")


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    cd, shared = tmp_path / "cfg", tmp_path / "claude"
    ensure_config_dir(cd)
    return cd, shared


def test_links_created_on_create(tmp_path):
    cd, shared = _setup(tmp_path)
    links = link_shared_dirs(cd, shared)
    assert links == [cd / "skills", cd / "rules"]
    for name in NAMES:
        assert (cd / name).is_symlink()
        assert (cd / name).resolve() == (shared / name).resolve()
        assert (shared / name).is_dir()
        assert stat.S_IMODE((shared / name).stat().st_mode) == 0o700


def test_scripts_not_linked(tmp_path):
    cd, shared = _setup(tmp_path)
    link_shared_dirs(cd, shared)
    assert not (cd / "scripts").exists()


def test_existing_dir_moved_once(tmp_path):
    cd, shared = _setup(tmp_path)
    skill = cd / "skills" / "a" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("body")
    link_shared_dirs(cd, shared)
    assert (shared / "skills" / "a" / "SKILL.md").read_text() == "body"
    assert (cd / "skills").is_symlink()
    assert (cd / "skills" / "a" / "SKILL.md").read_text() == "body"
    before = sorted(p.relative_to(shared) for p in shared.rglob("*"))
    link_shared_dirs(cd, shared)
    assert sorted(p.relative_to(shared) for p in shared.rglob("*")) == before
    assert (shared / "skills" / "a" / "SKILL.md").read_text() == "body"


def test_target_entry_wins_on_clash(tmp_path, caplog):
    cd, shared = _setup(tmp_path)
    (shared / "skills" / "a").mkdir(parents=True)
    (shared / "skills" / "a" / "SKILL.md").write_text("shared")
    (cd / "skills" / "a").mkdir(parents=True)
    (cd / "skills" / "a" / "SKILL.md").write_text("local")
    (cd / "skills" / "b").mkdir()
    (cd / "skills" / "b" / "SKILL.md").write_text("only-local")
    with caplog.at_level(logging.WARNING):
        link_shared_dirs(cd, shared)
    assert (shared / "skills" / "a" / "SKILL.md").read_text() == "shared"
    assert (shared / "skills" / "b" / "SKILL.md").read_text() == "only-local"
    assert (cd / "skills").is_symlink()
    assert any("a" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_resume_relinks(tmp_path):
    cd, shared = _setup(tmp_path)
    link_shared_dirs(cd, shared)
    (shared / "rules" / "r.md").write_text("rule")
    (cd / "rules").unlink()
    link_shared_dirs(cd, shared)
    assert (cd / "rules").is_symlink()
    assert (cd / "rules" / "r.md").read_text() == "rule"


def test_wrong_symlink_is_repointed(tmp_path):
    cd, shared = _setup(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (cd / "skills").symlink_to(other)
    link_shared_dirs(cd, shared)
    assert (cd / "skills").resolve() == (shared / "skills").resolve()


def test_no_shared_dir_keeps_plain_skills_dir(tmp_path):
    cd, _ = _setup(tmp_path)
    skill = cd / "skills" / "a" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("body")
    assert link_shared_dirs(cd, None) == [cd / "skills"]
    assert not (cd / "skills").is_symlink()
    assert skill.read_text() == "body"
    assert not (cd / "rules").exists()


def test_no_shared_dir_creates_skills_dir(tmp_path):
    cd, _ = _setup(tmp_path)
    link_shared_dirs(cd, None)
    assert (cd / "skills").is_dir() and not (cd / "skills").is_symlink()
    assert stat.S_IMODE((cd / "skills").stat().st_mode) == 0o700


def test_config_accepts_shared_claude_dir():
    assert PathsConfig(shared_claude_dir="/claude").shared_claude_dir == "/claude"
    assert PathsConfig().shared_claude_dir is None


def test_config_rejects_relative_shared_claude_dir():
    with pytest.raises(ValueError):
        PathsConfig(shared_claude_dir="claude")
