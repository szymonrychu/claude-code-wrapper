import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_package_version():
    import claude_code_wrapper

    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert claude_code_wrapper.__version__ == data["project"]["version"]


@pytest.mark.parametrize(
    "path",
    [".venv/x", "docs/plans/facts.md", ".claude/scheduled_tasks.lock", "config.yaml", ".env"],
)
def test_gitignore_covers_runtime_files(path):
    result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
    assert result.returncode == 0, f"{path} is not ignored"
