"""Pins the GitHub Actions pipeline, release flow and renovate config."""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
IMAGE = "harbor.szymonrichert.pl/containers/claude-code-wrapper"
SHA_USES = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _triggers(wf: dict) -> dict:
    # YAML 1.1 loads the bare `on` key as boolean True.
    return wf["on"] if "on" in wf else wf[True]


def _all_steps(wf: dict):
    for job in wf["jobs"].values():
        yield from job["steps"]


def _needs(job: dict) -> list:
    n = job.get("needs", [])
    return [n] if isinstance(n, str) else list(n)


def test_job_chain():
    jobs = _load("build.yaml")["jobs"]
    assert list(jobs) == ["pre-commit", "unit", "integration", "build-push"]
    assert _needs(jobs["pre-commit"]) == []
    assert _needs(jobs["unit"]) == ["pre-commit"]
    assert _needs(jobs["integration"]) == ["unit"]
    assert _needs(jobs["build-push"]) == ["integration"]
    for job in jobs.values():
        assert "timeout-minutes" in job
    assert jobs["integration"]["timeout-minutes"] == 20


def test_build_triggers_and_settings():
    wf = _load("build.yaml")
    assert wf["name"] == "main"
    trig = _triggers(wf)
    assert set(trig) == {"push", "pull_request", "workflow_dispatch"}
    assert trig["push"]["branches"] == ["main"]
    assert trig["pull_request"]["branches"] == ["main"]
    assert wf["permissions"] == {"contents": "read"}
    assert wf["env"]["HARBOR_URL"] == "harbor.szymonrichert.pl"
    assert wf["env"]["CONTAINER_NAME"] == "claude-code-wrapper"
    conc = wf["concurrency"]
    assert conc["group"] == "${{ github.workflow }}-${{ github.ref }}"
    assert "pull_request" in str(conc["cancel-in-progress"])


def test_stage_commands():
    jobs = _load("build.yaml")["jobs"]

    def runs(job):
        return [s["run"] for s in jobs[job]["steps"] if "run" in s]

    assert "mise run precommit" in runs("pre-commit")
    assert "mise run install" in runs("pre-commit")
    assert "mise run lint" in runs("unit")
    assert "mise run test" in runs("unit")
    assert "mise run test:integration" in runs("integration")


def test_push_and_login_gated_on_main_push():
    steps = _load("build.yaml")["jobs"]["build-push"]["steps"]
    gated = [
        s for s in steps if "docker login" in s.get("run", "") or "docker push" in s.get("run", "")
    ]
    assert len(gated) >= 2
    for s in gated:
        cond = s.get("if", "")
        assert "github.event_name == 'push'" in cond
        assert "refs/heads/main" in cond
    login = next(s for s in gated if "docker login" in s["run"])
    assert "--password-stdin" in login["run"]
    assert "secrets.HARBOR_USERNAME" in json.dumps(login)
    assert "secrets.HARBOR_PASSWORD" in json.dumps(login)
    builds = [s for s in steps if "docker build" in s.get("run", "")]
    assert builds and all("if" not in s for s in builds)
    assert "docker build" in builds[0]["run"]


def test_every_uses_is_pinned_with_version_comment():
    count = 0
    for path in WORKFLOWS.glob("*.y*ml"):
        for line in path.read_text().splitlines():
            m = re.match(r"\s*(?:-\s*)?uses:\s*(\S+)\s*(#\s*v\d+\S*)?\s*$", line)
            if m:
                count += 1
                assert SHA_USES.match(m.group(1)), line
                assert m.group(2), f"missing version comment: {line}"
    assert count >= 3


def test_image_refs_use_harbor_containers():
    for name in ("build.yaml", "release.yaml"):
        text = (WORKFLOWS / name).read_text()
        refs = re.findall(r"\S*/containers/\S+", text)
        assert refs
        for ref in refs:
            assert ref.startswith(("$HARBOR_URL/containers/$CONTAINER_NAME", IMAGE)), ref
    assert "HARBOR_URL" in _load("release.yaml")["env"]


def test_permissions_read_only():
    for name in ("build.yaml", "release.yaml"):
        wf = _load(name)
        assert wf["permissions"] == {"contents": "read"}
        for job in wf["jobs"].values():
            assert "permissions" not in job


def test_no_forbidden_registries_anywhere():
    for path in (ROOT / ".github").rglob("*"):
        if path.is_file():
            text = path.read_text().lower()
            for bad in ("docker.io", "hub.docker.com", "pypi", "twine", "ghcr.io"):
                assert bad not in text, f"{bad} in {path}"


def test_release_workflow():
    wf = _load("release.yaml")
    trig = _triggers(wf)
    assert list(trig) == ["push"]
    assert trig["push"] == {"tags": ["v[0-9]+.[0-9]+.[0-9]+"]}
    assert len(wf["jobs"]) == 1
    text = (WORKFLOWS / "release.yaml").read_text()
    assert "docker pull" in text
    assert ":latest" in text
    assert "${VERSION}" in text or "$VERSION" in text
    assert "::error::" in text
    assert "git rev-parse --short=7" in text
    assert text.count("docker push") == 2


def test_renovate_pins_digests():
    cfg = json.loads((ROOT / "renovate.json").read_text())
    assert cfg["pinDigests"] is True
    assert "config:recommended" in cfg["extends"]


def test_readme_ci_section():
    readme = (ROOT / "README.md").read_text()
    assert "\n## CI\n" in readme
    assert "### Bootstrap (operator steps)" in readme
    assert "build.yaml/badge.svg" in readme
    assert (
        "gh secret set HARBOR_USERNAME --body 'robot$gitlab' -R szymonrychu/claude-code-wrapper"
        in readme
    )
