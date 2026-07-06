# BUILD REPORT — Issue #10: Exercise the dictionary-veto path in the e2e smoke via a committed offline fixture

> Input: `.claude/sdd/features/DESIGN_ISSUE_10_DICT_VETO_E2E_FIXTURE.md`. Implemented literally —
> every code block, comment, docstring, and help-text wording in the design was copied verbatim
> into the working tree, then the full gate (sync, ruff check, ruff format, mypy, pytest x2) was
> run for real from this checkout. No hand-waving; exact command output is quoted below.

## Status

**Complete.** All four manifested files touched exactly as designed; all five gate commands green;
pytest 25/25 passed on both runs (23 pre-existing + 2 new).

## Files touched

| File | What / why |
|------|------------|
| `bll/jmdict.py` | `load_merged` gains `fixture_path=None`; when set, short-circuits to a direct `open()`/`json.load()` of the fixture file and returns it as-is, bypassing `load()`/`load_compounds()` (and therefore all network access) entirely. |
| `bll/cli.py` | `cmd_process`'s `jm = jmdict.load_merged()` call-site now threads `fixture_path=args.dict_json`; new `--dict-json PATH` argparse flag added immediately after `--no-dict` on the `process` subparser (help text worded to avoid embedded double quotes, per the design's ruff-format note). |
| `tests/fixtures/dict_promise_secret.json` | New committed, hand-authored, license-clean 2-entry JMdict fixture (約束→promise/vow, 秘密→secret) in `load_merged()`'s native shape — the offline seam's payload. |
| `tests/test_process_e2e_smoke.py` | Stale `--no-dict` docstring bullet corrected to point at the two new tests; `DICT_FIXTURE` + `GLOSS_JSON_DICT_VETO` module constants added; `run_process` helper gains `dict_json_path=None` (backward compatible, existing call sites unaffected); two new test functions appended: `test_process_e2e_dict_veto_fixture` and `test_process_e2e_dict_veto_two_clean_runs_byte_identical`. |

## Autonomous decisions

None — implemented literally from design. Every code block, comment wording, docstring, help
string, and constant/test placement matched the design's exact text; no fork was hit that required
a judgment call. Pre-edit inspection of `bll/jmdict.py` (`load_merged` at the time, line 222),
`bll/cli.py` (call-site at line ~370, `--no-dict` argparse block at line ~1266, `cmd_render`'s
untouched `load_merged()` call at line ~996), and `tests/test_process_e2e_smoke.py`'s full contents
confirmed the design's line-number and context assumptions were accurate, so no adaptation was
needed.

## Blockers

None.

## Acceptance-test verification

Issue #10's three checkable acceptance criteria (from `.claude/sdd/_synthesized/DEFINE_ISSUE_10_DICT_VETO_E2E_FIXTURE.md`):

| AC | Criterion | Evidence |
|----|-----------|----------|
| AC1 | Veto step runs inside an automated e2e test, no network access, no live Ollama/Claude dependency | Both new tests call `block_network(monkeypatch)` (same guard as the two pre-existing e2e tests) and use `--gloss-json` (bypasses the aligner backend, so no Ollama-reachability check or Claude CLI spawn). Both passed under this guard: `tests/test_process_e2e_smoke.py::test_process_e2e_dict_veto_fixture PASSED`, `tests/test_process_e2e_smoke.py::test_process_e2e_dict_veto_two_clean_runs_byte_identical PASSED`. No `AssertionError` from the guard's `blocked()`/`guarded_run()` was raised in either run. |
| AC2 | At least one veto decision on the fixture data is asserted (vetoed or explicitly passed-through) | `test_process_e2e_dict_veto_fixture` asserts `"Dictionary veto: 約束 -/-> special" in captured.out` (the vetoed occurrence, id=1) **and** asserts `秘密 (ひみつ)` / `約束 (やくそく)` in the kana layer plus `special 約束` in the plain layer with `promise`/`secret` absent (the pass-through occurrences) — both directions of the conjunction check exercised on one fixture. Test passed. |
| AC3 | Deterministic across two clean runs (byte-identical outputs) | `test_process_e2e_dict_veto_two_clean_runs_byte_identical` runs `run_process` twice with `--dict-json` active and asserts all four rendered layers (`adaptive`/`plain`/`kana`/`answers`) plus `plan.json` are byte-identical (`read_bytes() == read_bytes()`) between the two runs. Test passed on both full-suite executions (run 1 and run 2 below), confirming stability, not just a single lucky pass. |

## Gate transcript

All five commands run from the working directory, in order. Output trimmed to the meaningful lines.

**1. `uv sync --locked --dev`**
```
Resolved 67 packages in 2ms
Checked 32 packages in 0.75ms
```

**2. `uv run ruff check .`**
```
All checks passed!
```

**3. `uv run ruff format --check .`**
```
15 files already formatted
```

**4. `uv run mypy bll/`**
```
Success: no issues found in 9 source files
```

**5. `uv run pytest tests/` — run 1 of 2**
```
collecting ... collected 25 items

tests/test_backend_default.py::test_cli_process_defaults_backend_to_ollama PASSED [  4%]
tests/test_backend_default.py::test_gloss_and_align_defaults_backend_to_ollama PASSED [  8%]
tests/test_backend_gate.py::test_backend_gate_stops_on_unreachable_ollama PASSED [ 12%]
tests/test_db.py::test_set_status_transitions_update_row_and_return_rowcount_1[known] PASSED [ 16%]
tests/test_db.py::test_set_status_transitions_update_row_and_return_rowcount_1[ignored] PASSED [ 20%]
tests/test_db.py::test_set_status_transitions_update_row_and_return_rowcount_1[learning] PASSED [ 24%]
tests/test_db.py::test_set_status_unknown_lemma_is_noop PASSED           [ 28%]
tests/test_db.py::test_record_sighting_accumulates_exposures_and_upserts_sightings PASSED [ 32%]
tests/test_db.py::test_stamp_learned_is_idempotent PASSED                [ 36%]
tests/test_db.py::test_clock_sums_episode_tokens PASSED                  [ 40%]
tests/test_db.py::test_touch_last_seen_updates_position PASSED           [ 44%]
tests/test_db.py::test_connect_migrates_and_backfills_legacy_schema PASSED [ 48%]
tests/test_heteronym_reading.py::test_heteronym_sense_first_reading PASSED [ 52%]
tests/test_heteronym_reading.py::test_apply_sense_first_plan_mutation PASSED [ 56%]
tests/test_process_e2e_smoke.py::test_process_e2e_smoke_fixture PASSED   [ 60%]
tests/test_process_e2e_smoke.py::test_process_e2e_two_clean_runs_byte_identical PASSED [ 64%]
tests/test_process_e2e_smoke.py::test_process_e2e_dict_veto_fixture PASSED [ 68%]
tests/test_process_e2e_smoke.py::test_process_e2e_dict_veto_two_clean_runs_byte_identical PASSED [ 72%]
tests/test_timeline.py::test_init_idempotent_then_record_snapshots_new_episode_then_noop PASSED [ 76%]
tests/test_timeline.py::test_branch_at_snapshotted_position_succeeds_and_keeps_source_branch PASSED [ 80%]
tests/test_timeline.py::test_branch_at_position_without_snapshot_raises_value_error PASSED [ 84%]
tests/test_timeline.py::test_switch_to_existing_branch_with_snapshot_succeeds PASSED [ 88%]
tests/test_timeline.py::test_switch_to_unknown_branch_raises_value_error PASSED [ 92%]
tests/test_timeline.py::test_switch_to_branch_without_snapshot_raises_value_error PASSED [ 96%]
tests/test_timeline.py::test_state_reports_branches_and_seekable_positions PASSED [100%]

============================== 25 passed in 0.37s ==============================
```

**5. `uv run pytest tests/` — run 2 of 2 (determinism stability re-check)**
```
collecting ... collected 25 items
[... same 25 tests, same order ...]
tests/test_process_e2e_smoke.py::test_process_e2e_dict_veto_fixture PASSED [ 68%]
tests/test_process_e2e_smoke.py::test_process_e2e_dict_veto_two_clean_runs_byte_identical PASSED [ 72%]
[...]
============================== 25 passed in 0.40s ==============================
```

Both runs: **25 passed, 0 failed** (23 pre-existing + 2 new: `test_process_e2e_dict_veto_fixture`,
`test_process_e2e_dict_veto_two_clean_runs_byte_identical`).

## Post-hoc sanity check (not part of the mandated gate)

`uv run bll process --help` confirms the new flag renders correctly and matches the design's
exact wording:
```
--dict-json DICT_JSON
                    load the JMdict lookup from this JSON file instead of
                    downloading (offline/deterministic testing; must match
                    load_merged()'s surface -> [{g: glosses, r: readings}]
                    shape). Ignored if --no-dict is set.
```
