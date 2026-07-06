# BUILD REPORT — Issue #17: Database structure review + migration plan

> Repo: `Future-Gadgets-AI/bilingual-language-learning` · Branch: `feat/db-versioned-migrations`
> Design: `.claude/sdd/features/DESIGN_ISSUE_17_DB_VERSIONED_MIGRATIONS.md`

## Summary

| Metric | Value |
|---|---|
| Files modified | 2 (`bll/db.py`, `tests/test_db.py`) |
| Files created | 1 (`docs/DB_AUDIT.md`) |
| New public/module symbols in `bll/db.py` | `MIGRATIONS`, `SCHEMA_VERSION`, `_apply_migrations`, `_migration_v2_lang_keying`, `_rebuild_words_table_add_lang` |
| Renamed | `_migrate` → `_migration_v1_baseline` (body unchanged; folded in as migration step v0→v1) |
| Test suite | 31 → 32 passed (1 new test function; the legacy-migration test was extended in place, not counted twice) |
| CI gates | ruff check / ruff format --check / mypy bll/ / pytest — all exit 0 |
| Agents used | 0 — the `agentspec:workflow:build` skill returned its own instruction text rather than executing (see Autonomous Decisions #1); executed directly against the design, which was fully prescriptive |

## Tasks with Attribution

| Task | Agent | Status | Notes |
|---|---|---|---|
| `bll/db.py`: new `Callable` import (Decision 6) | (direct) | done | `from collections.abc import Callable`, alphabetically placed before `from datetime import datetime` |
| `bll/db.py`: `SCHEMA`'s `words` table gains `lang` + `UNIQUE(lemma, lang)` (Decision 2) | (direct) | done | Existing inline column comments preserved; only the column list/constraint changed |
| `bll/db.py`: rename `_migrate` → `_migration_v1_baseline` (Decision 1) | (direct) | done | Body byte-for-byte unchanged |
| `bll/db.py`: `_backfill_episode_meta` docstring fix (Decision 4) | (direct) | done | `"e04.ja.srt" -> "04"` corrected to `"4"`; regex/behavior untouched |
| `bll/db.py`: `_migration_v2_lang_keying` + `_rebuild_words_table_add_lang` (Decision 2) | (direct) | done | Idempotency guard on `lang` column presence; explicit column lists throughout, never `SELECT *` |
| `bll/db.py`: `MIGRATIONS` list + `SCHEMA_VERSION` (Decision 1) | (direct) | done | `SCHEMA_VERSION = len(MIGRATIONS) == 2` |
| `bll/db.py`: `_apply_migrations` (Decision 1) | (direct) | done | Whole-walk `BEGIN`/`COMMIT`/rollback; backup gated on `pre_existing`, not `current == 0` |
| `bll/db.py`: `connect()` updated (Decision 1) | (direct) | done | Signature unchanged; computes `pre_existing` before `sqlite3.connect()` creates the file |
| `tests/test_db.py`: extend `test_connect_migrates_and_backfills_legacy_schema` | (direct) | done | Added `PRAGMA user_version == SCHEMA_VERSION` + `lang == "ja"` spot-check |
| `tests/test_db.py`: new `test_connect_migrates_words_lang_with_no_data_loss` | (direct) | done | `PRE_LANG_SCHEMA` fixture spanning words/episodes/sightings/variants; asserts id-preservation and cross-table resolution post-rebuild |
| `tests/test_db.py`: stale `_migrate` comment references | (direct) | done | See Autonomous Decisions #2 |
| `docs/DB_AUDIT.md` created | (direct) | done | Content copied verbatim from the design's "full content" section |

## Verification

All five required CI commands, final clean run from a fully edited working tree:

```text
$ uv sync --locked --dev
Resolved 67 packages in 3ms
Checked 32 packages in 4ms
exit: 0

$ uv run ruff check .
All checks passed!
exit: 0

$ uv run ruff format --check .
16 files already formatted
exit: 0

$ uv run mypy bll/
Success: no issues found in 9 source files
exit: 0

$ uv run pytest tests/
============================= test session starts ==============================
platform darwin -- Python 3.13.13, pytest-9.1.1, pluggy-1.6.0
rootdir: <repo root>
configfile: pyproject.toml
collected 32 items

tests/test_backend_default.py ..                                         [  6%]
tests/test_backend_gate.py .                                             [  9%]
tests/test_cue_overlap.py ......                                         [ 28%]
tests/test_db.py ..........                                              [ 59%]
tests/test_heteronym_reading.py ..                                       [ 65%]
tests/test_process_e2e_smoke.py ....                                     [ 78%]
tests/test_timeline.py .......                                           [100%]

============================== 32 passed in 0.41s ==============================
exit: 0
```

| Check | Result |
|---|---|
| Baseline (before this build) | 31 passed |
| Final (after this build) | **32 passed** (> 31 floor, as required) |
| Pre-existing tests | All pass unmodified, except `test_connect_migrates_and_backfills_legacy_schema` which the design explicitly extends |
| `ruff check` / `ruff format --check` / `mypy bll/` | All exit 0, zero findings |
| `git status --short` | `M bll/db.py`, `M tests/test_db.py`, `?? docs/` (new dir) — no file outside the manifest touched |
| `git diff --stat` | `bll/db.py \| 113 ++++++++++++++++++++++++++++++++++++++++++++++++++++---` / `tests/test_db.py \| 106 +++++++++++++++++++++++++++++++++++++++++++++++++--` / `2 files changed, 211 insertions(+), 8 deletions(-)` |

### Additional manual verification (beyond the design's test plan)

Ran a standalone script (three scenarios, outside the repo, deleted after) directly exercising the
acceptance-relevant runtime behavior the pytest suite doesn't explicitly assert on (backup-file
creation), to independently confirm Decision 1 / Decision 5 rather than trust the design's reasoning
alone:

```text
OK: fresh DB -> version 2, no backup created
OK: pre-existing DB -> backed up once, version 2, lang backfilled
OK: re-opening an already-migrated DB does not create a second backup (fast path)
```

Confirms: (1) a brand-new DB walks both migration steps and lands on `user_version == 2` with zero
I/O side effects (no `backups/` dir created); (2) a DB that pre-exists on disk before `connect()`
gets exactly one backup snapshot, migrates cleanly, and backfills `lang='ja'`; (3) re-opening an
already-migrated DB hits the `current >= SCHEMA_VERSION` fast path and does not create a second
backup.

## Autonomous Decisions

| # | Decision Point | Options Considered | Chose | Rationale |
|---|---|---|---|---|
| 1 | `agentspec:workflow:build` invoked via the Skill tool returned its own reference/instruction text (the command's markdown documentation) rather than executing against the design file | (a) Treat as unusable and implement directly vs. (b) manually follow the returned instructions in-thread | (b), converging on the same outcome as (a) | The returned text *is* the intended process (parse manifest → order by dependency → execute → validate → report), so I followed it directly: read DESIGN fully, executed the 3-file manifest in dependency order (`db.py` before `test_db.py` before `docs/DB_AUDIT.md`), ran the full validation loop, and wrote this report — exactly per the skill's own "Process" section. No subagent delegation was warranted since the design is fully prescriptive (exact SQL, exact signatures) and touches one cohesive module + its test file. |
| 2 | `tests/test_db.py` has three pre-existing comments (lines ~114, ~117, ~221) narrating the legacy-migration test that name `_migrate` by its old name — not mentioned in the design's test-plan diff, but now stale since Decision 1 renames that exact function | (a) Leave the stale comments (design's test plan doesn't mention them) vs. (b) update the three references to `_migration_v1_baseline` | (b) | These comments describe the very function this issue renames, in the one file the manifest already authorizes modifying; leaving them would misdocument the code they sit next to. Directly caused by (not unrelated to) this build's own rename — consistent with Decision 4's own precedent of fixing exactly this class of doc/behavior drift. Comment-only change, zero behavior risk, re-verified green after. |
| 3 | `SCHEMA`'s `words` table: the design's Decision 2 code block shows the target column list without the file's existing inline documentation comments (e.g. on `note`, `status`, `exposures`, `last_seen_pos`, the forgetting-clock comment block) | (a) Replace the whole `CREATE TABLE` block verbatim per the design snippet (losing those comments) vs. (b) apply only the structural delta (insert `lang` column + its own comment, move `UNIQUE` to a table-level constraint) and keep every pre-existing comment intact | (b) | The design's prose says `lang`/the widened constraint are added "directly" to `SCHEMA` — describing the structural delta, not instructing comment removal. Option (b) satisfies that delta exactly (verified byte-for-byte against `_rebuild_words_table_add_lang`'s target shape) while following the global guardrail against unrelated refactoring / accidental deletion of existing documentation. |

## Deviations from Design

None of substance. The three items above are additive precision (in-scope comment consistency, doc-comment preservation) or a process-following clarification, not departures from any decision the design settled. All exact SQL (words `CREATE TABLE`, `words_new` rebuild, `INSERT...SELECT`), all exact signatures (§ "Type annotations"), the whole-walk transaction shape, the `pre_existing`-gated backup, and the docstring fix were transcribed as specified and pass on first execution.

## Files Changed

- Modified: `bll/db.py` — new `Callable` import; `words` schema gains `lang`/`UNIQUE(lemma, lang)`; `_migrate` renamed to `_migration_v1_baseline`; new `_migration_v2_lang_keying` + `_rebuild_words_table_add_lang`; new `MIGRATIONS`/`SCHEMA_VERSION`; new `_apply_migrations`; `connect()` updated to compute `pre_existing` and call `_apply_migrations`; `_backfill_episode_meta` docstring fix.
- Modified: `tests/test_db.py` — extended `test_connect_migrates_and_backfills_legacy_schema` with a `PRAGMA user_version` + `lang` assertion; added `PRE_LANG_SCHEMA` fixture and `test_connect_migrates_words_lang_with_no_data_loss`; fixed three stale `_migrate` comment references.
- Created: `docs/DB_AUDIT.md` — per-table schema audit (verbatim from the design).
- No git operations performed (no add/commit/branch/push) — left for the pipeline's composer phase.

## Blockers

None.

## Status: Complete
