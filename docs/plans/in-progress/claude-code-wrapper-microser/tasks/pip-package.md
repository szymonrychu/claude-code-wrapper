---
title: Pip package
status: in-progress
repo: claude-code-wrapper
value: "Install the service with pip and run ccw from PATH, in a Dockerfile or on a containerless host (D25)"
files: [pyproject.toml, src/claude_code_wrapper/cli.py, src/claude_code_wrapper/bootstrap/settings.py, src/claude_code_wrapper/data/__init__.py, Dockerfile, README.md, tests/test_packaging.py]
verify: "uv run pytest tests/test_packaging.py -q && mise run lint"
depends_on: [service-main, container, readme-config]
owns: []
uses: []
model: ""
estimate: 60
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--pip-package
branch: claude-code-wrapper-microser--pip-package
verified_head: ""
run: ""
provides: []
---

# Pip package

## Requirement

Make the service installable with pip as a normal Python package that puts a command on PATH (user decision D25, approval A19): usable both in a Dockerfile and on an existing containerless host.

- `pyproject.toml`: complete package metadata (readme = README.md, license, authors, classifiers, `requires-python`, project urls optional); the wheel must contain everything the service needs at runtime - the `claude_code_wrapper` package, `config.example.yaml` and `examples/workbench.config.yaml` as package data (e.g. under `claude_code_wrapper/data/`), and any hook relay entry point. Console scripts `ccw` (service CLI) and `ccw-hook` (claude hook relay) stay in `[project.scripts]`. The relay command written into rendered claude `settings.json` must resolve to the installed `ccw-hook` (absolute path from `shutil.which` or `sys.executable -m claude_code_wrapper.hook_relay`), never a repo-relative path, so a pip install anywhere works.
- `ccw --version` prints the package version; `ccw config example` (or equivalent subcommand) prints the bundled example config so a containerless user can bootstrap `config.yaml`.
- Containerless defaults: every container-specific path (workdir root, sessions/CLAUDE_CONFIG_DIR root, mise data dir) comes from config with documented defaults and works when run as a normal user outside Docker; `ccw serve` checks at startup that `claude` (configured binary) and `git` are on PATH and fails with a clear message if not.
- Dockerfile: install the service with pip from a wheel built in a builder stage (`uv build` / `pip wheel`), `pip install /tmp/*.whl` into the runtime image, no copied source tree or `.venv`; `CMD ["ccw", "serve"]` keeps working.
- README.md: an "Installation" section covering `pip install claude-code-wrapper` / `pip install git+<url>` / `pip install .`, `pipx`/`uv tool install`, containerless prerequisites (claude CLI, git, optional mise) and a minimal containerless run (`ccw config example > config.yaml && ccw serve --config config.yaml`), plus the Dockerfile snippet for installing it in a custom image.

Tests (`tests/test_packaging.py`): build the wheel with `uv build --wheel` into tmp_path, create a fresh venv, `pip install` the wheel, then assert `ccw --version` matches pyproject version, `ccw config example` output parses as a valid config, `ccw-hook` is on the venv PATH, and the wheel contains the data files. Mark nothing as skip.

## Acceptance conditions

- AC1: Wheel builds and installs into a fresh venv; `ccw --version` prints the pyproject version [evidence: test:tests/test_packaging.py::test_wheel_installs_and_ccw_version]
- AC2: Installed `ccw config example` prints the bundled example config and it validates [evidence: test:tests/test_packaging.py::test_config_example_from_installed_wheel]
- AC3: Installed `ccw-hook` is on the venv PATH and rendered settings reference the installed relay, not a repo path [evidence: test:tests/test_packaging.py::test_hook_relay_installed_and_referenced]
- AC4: Wheel contains config.example.yaml and the workbench example as package data [evidence: test:tests/test_packaging.py::test_wheel_contains_data_files]
- AC5: `ccw serve` fails fast with a clear message when the claude binary or git is missing from PATH [evidence: test:tests/test_packaging.py::test_serve_preflight_missing_binaries]
- AC6: Dockerfile installs the service from a built wheel via pip (no copied .venv/source) and CMD ccw serve still works [evidence: test:tests/test_packaging.py::test_dockerfile_installs_wheel_via_pip]
- AC7: README has an Installation section covering pip/pipx/uv tool install, containerless prerequisites and a containerless run example [evidence: test:tests/test_packaging.py::test_readme_installation_section]

## Files

- `pyproject.toml`
- `src/claude_code_wrapper/cli.py`
- `src/claude_code_wrapper/bootstrap/settings.py`
- `src/claude_code_wrapper/data/__init__.py`
- `Dockerfile`
- `README.md`
- `tests/test_packaging.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

`pip install` of the built wheel into any venv gives working `ccw` and `ccw-hook` commands; the Docker image installs the same wheel; a containerless host can bootstrap config with `ccw config example` and run `ccw serve`.

What is true and observable when the task is done.

## Steps

1. Red: write tests/test_packaging.py (wheel build + fresh venv install + CLI checks + Dockerfile/README assertions) and see it fail.
2. Green: package data, `--version`, `config example`, preflight, relay path resolution, Dockerfile wheel install, README Installation section.
3. Refactor with the suite green; run `mise run test` and `mise run lint`.

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
