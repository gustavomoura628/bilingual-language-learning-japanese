# BUILD REPORT: Issue #8 — Behavioral test coverage for `bll/db.py` and `bll/timeline.py`

> Repo: `Future-Gadgets-AI/bilingual-language-learning` (checkout at this run) · Branch: `test/db-timeline-behavioral`
> Design: `.claude/sdd/features/DESIGN_ISSUE_8_BEHAVIORAL_TEST_COVERAGE.md`

## Summary

| Metric | Value |
|--------|-------|
| Tasks | 2/2 files completed |
| Files Created | 2 (`tests/test_db.py`, `tests/test_timeline.py`) |
| Test functions added | 14 (7 per file, per the design's file manifest) |
| Collected pytest items added | 16 (9 from `test_db.py` incl. the x3 parametrize expansion, 7 from `test_timeline.py`) |
| Agents Used | 0 (manifest specified `@test-generator`; executed directly against the execution-verified design — see Autonomous Decisions #1) |

## Tasks with Attribution

| Task | Agent | Status | Notes |
|------|-------|--------|-------|
| `tests/test_db.py` | (direct) | ✅ | 7 test functions (AT1–AT6, one parametrized x3) transcribed from Decisions 1–3 of the design |
| `tests/test_timeline.py` | (direct) | ✅ | 7 test functions (AT7–AT10) transcribed from Decisions 4–8 of the design, incl. the white-box ghost-branch test |

## Verification

| Check | Result |
|-------|--------|
| `./.venv/bin/pytest tests/ -v` (full suite, final run) | ✅ exit 0 — **23 passed in 0.31s** |
| Pre-existing tests still pass | ✅ all 7 (`test_backend_default.py` x2, `test_backend_gate.py` x1, `test_heteronym_reading.py` x2, `test_process_e2e_smoke.py` x2) |
| New tests | ✅ all 16 collected items across the 2 new files |
| First-run pass rate | 23/23 on the first execution — no transcription fixes needed (design's execution-verified assertions transcribed faithfully, including the tricky ones: `episode_no` backfilling `"e04.ja.srt"` → `"4"` not `"04"`, and the white-box ghost-branch registry injection) |
| ruff / mypy | Skipped — not configured in this repo (`.venv/bin/ruff` absent, no `[tool.ruff]` in `pyproject.toml`), same precedent as `BUILD_REPORT_ISSUE_6` |
| `git status` (no unintended changes) | ✅ only `tests/test_db.py` and `tests/test_timeline.py` created; no other files touched |

### Full pytest output (final run)

```text
============================= test session starts ==============================
platform darwin -- Python 3.14.6, pytest-9.1.1, pluggy-1.6.0 -- .venv/bin/python3.14
cachedir: .pytest_cache
rootdir: <repo root>
configfile: pyproject.toml
collecting ... collected 23 items

tests/test_backend_default.py::test_cli_process_defaults_backend_to_ollama PASSED [  4%]
tests/test_backend_default.py::test_gloss_and_align_defaults_backend_to_ollama PASSED [  8%]
tests/test_backend_gate.py::test_backend_gate_stops_on_unreachable_ollama PASSED [ 13%]
tests/test_db.py::test_set_status_transitions_update_row_and_return_rowcount_1[known] PASSED [ 17%]
tests/test_db.py::test_set_status_transitions_update_row_and_return_rowcount_1[ignored] PASSED [ 21%]
tests/test_db.py::test_set_status_transitions_update_row_and_return_rowcount_1[learning] PASSED [ 26%]
tests/test_db.py::test_set_status_unknown_lemma_is_noop PASSED           [ 30%]
tests/test_db.py::test_record_sighting_accumulates_exposures_and_upserts_sightings PASSED [ 34%]
tests/test_db.py::test_stamp_learned_is_idempotent PASSED                [ 39%]
tests/test_db.py::test_clock_sums_episode_tokens PASSED                  [ 43%]
tests/test_db.py::test_touch_last_seen_updates_position PASSED           [ 47%]
tests/test_db.py::test_connect_migrates_and_backfills_legacy_schema PASSED [ 52%]
tests/test_heteronym_reading.py::test_heteronym_sense_first_reading PASSED [ 56%]
tests/test_heteronym_reading.py::test_apply_sense_first_plan_mutation PASSED [ 60%]
tests/test_process_e2e_smoke.py::test_process_e2e_smoke_fixture PASSED   [ 65%]
tests/test_process_e2e_smoke.py::test_process_e2e_two_clean_runs_byte_identical PASSED [ 69%]
tests/test_timeline.py::test_init_idempotent_then_record_snapshots_new_episode_then_noop PASSED [ 73%]
tests/test_timeline.py::test_branch_at_snapshotted_position_succeeds_and_keeps_source_branch PASSED [ 78%]
tests/test_timeline.py::test_branch_at_position_without_snapshot_raises_value_error PASSED [ 82%]
tests/test_timeline.py::test_switch_to_existing_branch_with_snapshot_succeeds PASSED [ 86%]
tests/test_timeline.py::test_switch_to_unknown_branch_raises_value_error PASSED [ 91%]
tests/test_timeline.py::test_switch_to_branch_without_snapshot_raises_value_error PASSED [ 95%]
tests/test_timeline.py::test_state_reports_branches_and_seekable_positions PASSED [100%]

============================== 23 passed in 0.31s ===============================
exit=0
```

## Per-acceptance-test verification

| AT | Test function(s) | File | Result |
|----|-------------------|------|--------|
| AT1 | `test_set_status_transitions_update_row_and_return_rowcount_1` (parametrized: known/ignored/learning) | `test_db.py` | ✅ PASS (3/3) |
| AT2 | `test_set_status_unknown_lemma_is_noop` | `test_db.py` | ✅ PASS |
| AT3 | `test_record_sighting_accumulates_exposures_and_upserts_sightings` | `test_db.py` | ✅ PASS |
| AT4 | `test_stamp_learned_is_idempotent` | `test_db.py` | ✅ PASS |
| AT5 | `test_clock_sums_episode_tokens`, `test_touch_last_seen_updates_position` | `test_db.py` | ✅ PASS (2/2) |
| AT6 | `test_connect_migrates_and_backfills_legacy_schema` | `test_db.py` | ✅ PASS |
| AT7 | `test_init_idempotent_then_record_snapshots_new_episode_then_noop` | `test_timeline.py` | ✅ PASS |
| AT8 | `test_branch_at_snapshotted_position_succeeds_and_keeps_source_branch`, `test_branch_at_position_without_snapshot_raises_value_error` | `test_timeline.py` | ✅ PASS (2/2) |
| AT9 | `test_switch_to_existing_branch_with_snapshot_succeeds`, `test_switch_to_unknown_branch_raises_value_error`, `test_switch_to_branch_without_snapshot_raises_value_error` | `test_timeline.py` | ✅ PASS (3/3) |
| AT10 | `test_state_reports_branches_and_seekable_positions` | `test_timeline.py` | ✅ PASS |

**10/10 acceptance tests covered, all passing.**

## Deviations from the design

**None.** Every execution-verified assertion in the design (including the two flagged tricky
points — AT6's `episode_no` capturing `"4"` not `"04"`, and AT9's white-box ghost-branch registry
injection) was transcribed faithfully and passed on the first run against the real modules. No
design assertion contradicted real module behavior.

One clarification, not a deviation: the design's own overview text says "14 tests" (counting test
*functions*, matching its File manifest table: "7 test functions (1 parametrized x3)" +
"7 test functions" = 14 functions). The AT1 parametrize expands to 3 collected pytest items, so the
actual collected-item count contributed by this build is 16, not 14 — fully consistent with the
design's own manifest, just stated here in collected-item terms for precision.

## Autonomous Decisions

| # | Decision Point | Options Considered | Chose | Rationale |
|---|----------------|--------------------|-------|-----------|
| 1 | Design manifest specifies `@test-generator` for both files, but the design body already contains near-complete, execution-verified test code (not a spec to interpret) | (a) Delegate to `test-generator` subagent vs. (b) transcribe the design's verified code directly | (b) The design explicitly states its values were "executed against the real modules... not guesses" and instructs faithful transcription; delegating would add a synthesis step with no upside and a risk of the subagent "fixing" the deliberately-tricky values (e.g. re-deriving `"04"` from the docstring instead of the verified `"4"`). Direct transcription is the smallest-correct-change path that preserves exact-execution fidelity |
| 2 | AT6's raw legacy-DB seed code: the design gives the DDL, seed-data tuples, and final assertions verbatim, but describes the INSERT/id-translation "build order" narratively, not as literal Python | (a) Hardcode literal ids (覚える=1, 迷う=2, e01=1, e02=2, e04=3) as the design's prose states vs. (b) build id-lookup dicts (`word_ids`, `ep_ids`) from each `INSERT`'s own `lastrowid` | (b) Produces the exact same ids the design calls out (insertion order is unchanged) while being robust to any accidental reordering of the seed lists — smallest-correct glue code, not a deviation in behavior or asserted values |
| 3 | Import ordering for the new `import pytest` line (no existing file in this repo imports pytest, so there's no direct precedent) | (a) Group with stdlib imports vs. (b) stdlib imports, blank line, `import pytest`, blank line, `sys.path.insert` + `from bll import ...` | (b) Standard isort/PEP8 grouping (stdlib → third-party → local); keeps the existing `sys.path.insert` + `from bll import` pairing intact exactly as established by `test_process_e2e_smoke.py`/`test_backend_gate.py` |
| 4 | Semicolon-joined same-line statements in `test_timeline.py` (e.g. `_add_episode(path, "e01.ja.srt"); timeline.record(path)`), copied verbatim from the design | (a) Split onto separate lines (more conventional Python style) vs. (b) transcribe verbatim | (b) The build brief explicitly instructs faithful transcription of the design's code and this repo has no ruff/lint gate enforcing statement-per-line style (confirmed: ruff absent from `.venv`, no `[tool.ruff]` config) — not worth deviating from the design's exact, execution-verified text for a cosmetic style preference |
| 5 | ruff/mypy verification (build-agent's standard Capability 3 step) | (a) Attempt to run/install linters vs. (b) skip, matching `BUILD_REPORT_ISSUE_6`'s established precedent | (b) Confirmed no `ruff`/`mypy` in `.venv/bin`, no `[tool.ruff]`/`[tool.mypy]` in `pyproject.toml`; this build brief also does not require it. Consistent with prior precedent in this exact repo |

## Files Created/Modified

- Created: `tests/test_db.py`, `tests/test_timeline.py`
- Modified: none
- No git operations performed (no add/commit/branch/push) — left for the pipeline's composer phase.

## Total suite runtime

**0.31s** for all 23 tests (in-process SQLite / `:memory:` + small `tmp_path` file copies only; no sleeps, no network) — well within the "a second or two" budget noted in the design.

## Status: ✅ COMPLETE
