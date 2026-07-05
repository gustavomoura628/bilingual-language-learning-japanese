# BUILD REPORT: Issue #6 — Add GitHub Actions CI to run the test suite on every pull request

> Repo: `Future-Gadgets-AI/bilingual-language-learning` · Branch: `feat/ci-pytest-workflow`
> Design: `.claude/sdd/features/DESIGN_ISSUE_6_CI_PYTEST_WORKFLOW.md`

## Summary

| Metric | Value |
|--------|-------|
| Tasks | 6/6 completed |
| Files Created | 1 (`.github/workflows/tests.yml`) |
| Files Modified | 5 (`pyproject.toml`, `uv.lock`, 3 test files) |
| Agents Used | 0 (all manifest entries `(general)`, executed directly) |

## Tasks with Attribution

| Task | Agent | Status | Notes |
|------|-------|--------|-------|
| `.github/workflows/tests.yml` | (direct) | ✅ | Created verbatim per Decision 1's YAML block: `pull_request` → `main` trigger, checkout@v6 → setup-python@v6 (3.12) → setup-uv@v8.2.0 → `uv sync --locked --dev` → `uv run pytest tests/` |
| `pyproject.toml` | (direct) | ✅ | Added `[dependency-groups]` / `dev = ["pytest>=8.0"]` after `[project.optional-dependencies]`, before `[project.scripts]`, per Decision 2 |
| `uv.lock` | (direct, mechanical regen) | ✅ | Regenerated via `uv lock` (not hand-edited) — resolved pytest 9.1.1 + 6 new transitive deps (iniconfig, packaging, pluggy, pygments, tomli, and pytest itself) |
| `tests/test_backend_default.py` | (direct) | ✅ | Accumulator → 2 `def test_*():` functions; `cli.cmd_process = ...` → `monkeypatch.setattr(cli, "cmd_process", ...)` |
| `tests/test_backend_gate.py` | (direct) | ✅ | Accumulator → 1 `def test_*():` function; both raw patches → `monkeypatch.setattr(...)`; `tempfile.TemporaryDirectory()` → `tmp_path` fixture |
| `tests/test_heteronym_reading.py` | (direct) | ✅ | Accumulator → 2 `def test_*():` functions (split along original `# 1./2.` vs `# 3.` comment boundary); `lk = jmdict.load()` left at module level unchanged (no monkeypatching in this file) |

Every converted `assert` was checked 1:1 against the original `check(name, got, want)` call — same operands, same expected values (`"ollama"`, `"つの"`, `かく`/`かど`/`None`, `rc == 1`, `align_calls == []`, `changed == 1`, the two `note` strings, etc.). No check was added, removed, or altered.

## Verification

| Check | Result |
|-------|--------|
| `uv lock` (regenerate after `pyproject.toml` edit) | ✅ exit 0 — resolved 60 packages, added pytest 8.4.2/9.1.1 + iniconfig/packaging/pluggy/pygments/tomli |
| `uv sync --locked --dev` | ✅ exit 0 — 25 packages installed into `.venv`, including `pytest==9.1.1`; no drift between `pyproject.toml` and `uv.lock` |
| `uv run pytest tests/ -v` (full suite) | ✅ exit 0 — **5 passed in 0.26s** |
| Per-file collection | `test_backend_default.py` → 2 items PASSED · `test_backend_gate.py` → 1 item PASSED · `test_heteronym_reading.py` → 2 items PASSED (5 total, all 3 files collected as real test items — DEFINE criterion 3) |
| Negative-path check (DEFINE criterion 5) | ✅ Temporarily broke one assertion's expected value (`"ollama"` → `"BROKEN_ON_PURPOSE"`) in a scratch copy-and-restore cycle: pytest reported a clean `AssertionError`, `1 failed, 4 passed in 0.26s`, exit 1 — **not** an `INTERNALERROR`/crash. File restored byte-for-byte (verified via `diff`, zero output) before finishing |
| Workflow YAML parses | ✅ `yaml.safe_load(open('.github/workflows/tests.yml'))` → `yaml ok` (run in a throwaway venv outside the project; PyYAML was NOT added to the project's own deps, per scope) |
| ruff / mypy | Skipped — not configured in this repo; excluded from DESIGN scope, not introduced |

### Full pytest output (final run)

```text
============================= test session starts ==============================
platform darwin -- Python 3.13.13, pytest-9.1.1, pluggy-1.6.0
rootdir: <repo root>
configfile: pyproject.toml
collecting ... collected 5 items

tests/test_backend_default.py::test_cli_process_defaults_backend_to_ollama PASSED [ 20%]
tests/test_backend_default.py::test_gloss_and_align_defaults_backend_to_ollama PASSED [ 40%]
tests/test_backend_gate.py::test_backend_gate_stops_on_unreachable_ollama PASSED [ 60%]
tests/test_heteronym_reading.py::test_heteronym_sense_first_reading PASSED [ 80%]
tests/test_heteronym_reading.py::test_apply_sense_first_plan_mutation PASSED [100%]

============================== 5 passed in 0.26s ===============================
exit=0
```

## Autonomous Decisions

| # | Decision Point | Options Considered | Chose | Rationale |
|---|----------------|--------------------|-------|-----------|
| 1 | `pytest`'s "configfile: pyproject.toml" line in the run header — is this an accidental pytest-config addition (out of DESIGN scope)? | (a) Investigate/suppress it as unintended scope creep vs. (b) treat as pytest 9.x's normal rootdir-anchor reporting | (b) Verified `pyproject.toml` contains no `[tool.pytest.ini_options]` section (only the intended `[dependency-groups]` addition) — the label is pytest reporting which file anchored `rootdir` discovery, not evidence of an injected config. No action taken; smallest-correct-change is to not touch it |
| 2 | PyYAML sanity check tooling — project venv lacks PyYAML (correctly, per scope) | (a) `python3 -m venv` + `pip install pyyaml` vs. (b) `uv venv` + `uv pip install --python` in the scratchpad dir | (b) `uv` was already on `PATH` and is the project's own toolchain; using it for a fully-disposable, outside-the-repo venv is consistent tooling with zero footprint on the project itself. Venv deleted immediately after the one-shot check | 
| 3 | Depth of verification for DEFINE criterion 5 ("reintroduced failing assertion shows red, not crashed") — the build instructions only required the positive-path `pytest tests/ -v` run | (a) Rely solely on code inspection (assert semantics vs. `sys.exit`) vs. (b) empirically prove it with a temporary, immediately-reverted assertion break | (b) Cheap and safe (single-line edit, single pytest run, restored from a backup copy and diff-verified identical before proceeding) and it directly substantiates a DESIGN traceability row rather than asserting it by inspection alone |
| 4 | Pre-existing `.gitignore` modification and `.claude/` untracked content present before this build started | (a) Investigate/revert as out-of-manifest changes vs. (b) leave untouched, they predate this task | (b) Confirmed via the initial `git status` (run before any edits) that `.gitignore` was already modified and `.claude/` already untracked prior to this build — not created by this work. Left exactly as found; not in the DESIGN's file manifest, not this build's responsibility |

## Files Created/Modified

- Created: `.github/workflows/tests.yml`
- Modified: `pyproject.toml`, `uv.lock`, `tests/test_backend_default.py`, `tests/test_backend_gate.py`, `tests/test_heteronym_reading.py`
- Not touched (pre-existing, out of manifest): `.gitignore`, `.claude/` (present before this build started)
- No git operations performed (no add/commit/branch/push) — left for the composer phase.

## Status: ✅ COMPLETE
