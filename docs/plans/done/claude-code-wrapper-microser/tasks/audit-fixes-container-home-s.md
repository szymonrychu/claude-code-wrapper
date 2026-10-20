---
title: Audit fixes container home-seed
status: done
repo: claude-code-wrapper
value: "Image ships a valid example config again and home-seed ACs have their evidence tests (A2, A5, A6)"
files: [Dockerfile, tests/test_container.py, tests/test_bootstrap_home.py]
verify: "uv run pytest tests/test_container.py tests/test_bootstrap_home.py -q && mise run lint"
depends_on: [container, pip-package]
owns: []
uses: []
model: ""
estimate: 45
actual: ""
review: ""
worktree: /Users/szymonri/Documents/claude-code-wrapper/.claude/worktrees/claude-code-wrapper-microser--audit-fixes-container-home-s
branch: claude-code-wrapper-microser--audit-fixes-container-home-s
verified_head: 648ce095431c4b161a9d48637238473b7b4f7cc8
run: claude-code-wrapper-microser
provides: []
acs_for_audit: []
process_gaps: []
---

# Audit fixes container home-seed

## Requirement

Epic-review remediation (scope audit gaps, A2/A5/A6). Two gaps found by the auditOnly run on main cdc33c2:

1. Container AC10 regressed when pip-package switched the image to a wheel install: the image no longer has `/app/config.example.yaml`, so `docker run claude-code-wrapper:test ccw check-config --config /app/config.example.yaml` fails ("cannot read config: No such file or directory"). Restore it: the runtime stage writes the bundled example into `/app/config.example.yaml` (e.g. `RUN mkdir -p /app && ccw config example > /app/config.example.yaml`, owned by agent), and keep the wheel-based install. Add a test in tests/test_container.py that builds the image (docker is available on this machine and is mandatory for these tests, as for the other container ACs) and runs exactly the AC10 command successfully.
2. home-seed AC evidence names are missing: tests/test_bootstrap_home.py must contain tests named exactly test_seed_creates_onboarding_and_trust, test_trust_uses_resolved_path, test_seed_merges_existing_keys, test_api_key_suffix_approved_once, test_bypass_accepted_only_in_bypass_mode, test_mcp_servers_rendered, test_overlay_deep_merge, test_file_mode_0600, test_instructions_concat_global_and_append, test_instructions_absent_when_empty, test_config_files_copied_and_escape_rejected, test_workdir_untouched. Rename the existing equivalents (do not weaken any assertion), and add dedicated tests for AC8 (written files are mode 0600) and AC12 (seeding never modifies the workdir) which today are only asserted inside test_seed_basic.

## Acceptance conditions

- AC1: The built image contains /app/config.example.yaml and `ccw check-config --config /app/config.example.yaml` exits 0 inside it [evidence: test:tests/test_container.py::test_example_config_validates_in_image]
- AC2: All 12 home-seed AC evidence test names exist in tests/test_bootstrap_home.py and pass, with no assertion removed [evidence: test:tests/test_bootstrap_home.py::test_seed_creates_onboarding_and_trust]
- AC3: Seeded files are mode 0600 in a dedicated test [evidence: test:tests/test_bootstrap_home.py::test_file_mode_0600]
- AC4: Seeding never modifies the workdir in a dedicated test [evidence: test:tests/test_bootstrap_home.py::test_workdir_untouched]

## Files

- `Dockerfile`
- `tests/test_container.py`
- `tests/test_bootstrap_home.py`

## Interfaces

Signatures, schemas and contracts this task builds or must keep, with file:symbol.

## Expected outcome

Every acceptance condition above passes on main after merge; full `mise run test` and `mise run lint` stay green.

What is true and observable when the task is done.

## Steps

1. Red: add/rename the evidence tests and see the new ones fail for the stated reason.
2. Green: smallest change that passes them.
3. Refactor with the suite green; run `mise run test` and `mise run lint`.

Test first. One step per bullet, with the exact command and the output to expect.
A retirement test asserts on `git ls-files <path>`, never on the filesystem.

## Notes

Context the implementer needs: pitfalls, shared parts to reuse, decisions already made.
