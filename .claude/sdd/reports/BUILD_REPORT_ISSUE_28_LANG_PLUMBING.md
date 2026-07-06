# BUILD REPORT — Issue #28: zh epic slice A — lang plumbing through `words` call-sites

> Repo: `Future-Gadgets-AI/bilingual-language-learning` · Branch: `feat/lang-plumbing-words-callsites`
> Design: `.claude/sdd/features/DESIGN_ISSUE_28_LANG_PLUMBING.md`

## Metadata

| Attribute | Value |
|-----------|-------|
| **Feature** | ISSUE_28_LANG_PLUMBING |
| **Date** | 2026-07-06 |
| **Author** | build phase (`agentspec:workflow:build`), driven headlessly by the `implement` skill |
| **DESIGN** | [DESIGN_ISSUE_28_LANG_PLUMBING.md](../features/DESIGN_ISSUE_28_LANG_PLUMBING.md) |
| **Issue** | Future-Gadgets-AI/bilingual-language-learning#28 |
| **Status** | Complete |

## Summary

| Metric | Value |
|---|---|
| Files modified | 4 (`bll/db.py`, `bll/web.py`, `bll/cli.py`, `tests/test_db.py`) |
| Files created | 0 (this report only) |
| New parameters | `lang: str = "ja"` on `all_words`, `upsert_word`, `set_status`, `set_note` (db.py); `lang: str = "ja"` on `words`, `word_history` (web.py, query param); `lang` read from `body.get("lang", "ja")` in `update_word` (web.py); `--lang` (default `"ja"`, no `choices=`) on the `process` CLI subcommand |
| Test suite | 32 → 35 passed (3 new tests appended to `tests/test_db.py`; zero existing tests modified) |
| CI gates | ruff check / ruff format --check / mypy bll/ / pytest — all exit 0 |
| Agents used | 0 — `agentspec:workflow:build` (Skill tool) returned its own reference/instruction text rather than executing a build agent (same behavior precedent as BUILD_REPORT_ISSUE_17); executed directly against the design, which was fully prescriptive down to exact SQL and signatures |

## Tasks with Attribution

| Task | Agent | Status | Notes |
|---|---|---|---|
| `bll/db.py`: `all_words(conn, lang="ja")` — `SELECT * FROM words WHERE lang=?` (Decision 1) | (direct) | done | Docstring updated to "scoped to one language" |
| `bll/db.py`: `upsert_word(..., lang="ja")` — lookup gains `AND lang=?`; INSERT explicitly lists `lang` column (Decisions 1 & 4) | (direct) | done | No longer relies on the schema `DEFAULT 'ja'` for inserts |
| `bll/db.py`: `set_status(conn, lemma, status, lang="ja")` — `UPDATE ... WHERE lemma=? AND lang=?` (Decision 1) | (direct) | done | — |
| `bll/db.py`: `set_note(conn, lemma, note, lang="ja")` — `UPDATE ... WHERE lemma=? AND lang=?` (Decision 1) | (direct) | done | — |
| `bll/web.py`: `GET /api/words` gains `lang: str = "ja"` next to `status`; query scoped by `lang=?` (+ optional `AND status=?`) (Decision 2) | (direct) | done | Matches file's existing no-Pydantic, plain-typed-param convention |
| `bll/web.py`: `GET /api/word/{lemma}/history` gains `lang: str = "ja"`; lookup `WHERE lemma=? AND lang=?` (Decision 2) | (direct) | done | — |
| `bll/web.py`: `POST /api/word/{lemma}` reads `lang = body.get("lang", "ja")`; threads into existence check + `set_status`/`set_note` (Decision 2) | (direct) | done | Signature unchanged (still `(lemma, body: dict)`) |
| `bll/cli.py`: `--lang` added to `process` subparser after `--episode`, `default="ja"`, no `choices=` (Decision 3) | (direct) | done | Help text points at issue #16 |
| `bll/cli.py`: `cmd_process` — first statement rejects `args.lang != "ja"` (stderr message naming #16, `return 1`), before `dbm.backup()`/`dbm.connect()` (Decision 3) | (direct) | done | Verified: fails before any file I/O (see Additional manual verification) |
| `bll/cli.py`: `dbm.all_words(conn, lang=args.lang)` at the `db_words = ...` call-site (~line 374 pre-edit) | (direct) | done | — |
| `bll/cli.py`: `dbm.upsert_word(..., lang=args.lang)` at the per-word upsert call-site (~line 942 pre-edit) | (direct) | done | `ruff format` reflowed the call across multiple lines (line-length), no logic change |
| `bll/cli.py`: `cmd_mark`/`cmd_note`/`cmd_words` left untouched (Decision 5) | (direct) | done | Confirmed not modified — `git diff` touches only `process`'s subparser + `cmd_process` |
| `tests/test_db.py`: 3 new tests appended (coexistence, `set_status` isolation, `set_note` isolation) | (direct) | done | Exact assertions from DESIGN's test sketches, with two line-length trims (see Autonomous Decisions) |

## Verification

All required commands, run from the fully edited working tree (uv 0.11.7, per repo convention — never pip):

```text
$ uv run ruff check .
All checks passed!
exit: 0

$ uv run ruff format --check .          # initial pass, before fixing
Would reformat: bll/cli.py
Would reformat: bll/db.py
Would reformat: tests/test_db.py
3 files would be reformatted, 13 files already formatted
exit: 1

$ uv run ruff format bll/cli.py bll/db.py bll/web.py tests/test_db.py
3 files reformatted, 1 file left unchanged
exit: 0

$ uv run ruff format --check .          # re-verify
16 files already formatted
exit: 0

$ uv run mypy bll/
Success: no issues found in 9 source files
exit: 0

$ uv run pytest tests/ -q
...................................                                      [100%]
35 passed in 0.40s
exit: 0
```

| Check | Result |
|---|---|
| Baseline (before this build) | 32 passed |
| Final (after this build) | **35 passed** (32 existing + 3 new, exactly as required) |
| Pre-existing tests | All pass unmodified — no edits to any test above the new appended block |
| `ruff check` | Exit 0, zero findings |
| `ruff format --check` | Exit 0 after formatting only the 4 touched files (never ran a blind repo-wide `ruff format .`) |
| `mypy bll/` | Exit 0, zero findings (9 source files) |
| `git status --short` | `M bll/cli.py`, `M bll/db.py`, `M bll/web.py`, `M tests/test_db.py` — no file outside the manifest touched |
| `git diff --stat` | `bll/cli.py \| 25 +++++++++++++++++++++++--` / `bll/db.py \| 22 ++++++++++++----------` / `bll/web.py \| 20 +++++++++++---------` / `tests/test_db.py \| 48 ++++++++++++++++++++++++++++++++++++++++++++++++` / `4 files changed, 94 insertions(+), 21 deletions(-)` |

### Additional manual verification (beyond the design's test plan)

Ran two structural smokes of the CLI surface (Decision 3), which the design marks as optional/light coverage rather than a gate:

```text
$ uv run python -m bll.cli process --help | grep -A3 -- --lang
  --lang LANG           language track to process (default ja). Other values
                        are accepted here but not yet supported end-to-end --
                        see issue #16.

$ uv run python -m bll.cli process nope.ja.srt nope.en.srt --lang zh-TW
--lang 'zh-TW' is not supported yet -- zh-TW ingestion lands in a later slice
of issue #16 (this change only plumbs the seam). Use --lang ja, or omit
--lang, for now.
exit: 1
```

Confirms: (1) `--lang` shows as a plain `LANG`-valued flag in `--help` — not `--lang {ja}` — so argparse genuinely accepts any string value (no `choices=` fence), satisfying the "zh-TW must be syntactically accepted" requirement; (2) passing nonexistent `.srt` paths alongside `--lang zh-TW` still exits 1 on the lang message rather than a `FileNotFoundError` from `pysubs2.load`, proving the rejection really is the first statement in `cmd_process`, ahead of `dbm.backup()`, `dbm.connect()`, and subtitle loading — a bad `--lang` value cannot leave a half-written backup or DB state (Decision 3's stated consequence).

The byte-invariance oracle itself (`tests/test_process_e2e_smoke.py`, run with no `--lang` flag) is part of the 35-passed pytest run above and was not modified by this build — its unchanged pass is the direct AT5 evidence.

## Autonomous Decisions

| # | Decision Point | Options Considered | Chose | Rationale |
|---|---|---|---|---|
| 1 | `agentspec:workflow:build`, invoked via the Skill tool, returned its own reference/instruction text (the build command's markdown documentation) rather than executing a build agent against the design | (a) Treat as unusable and implement directly vs. (b) manually follow the returned instructions in-thread | (b) | Same fork already resolved and logged in `BUILD_REPORT_ISSUE_17_DB_VERSIONED_MIGRATIONS.md` Decision #1 for this repo — the returned text *is* the intended process (parse manifest → order by dependency → execute → validate → report). Followed it directly: read DESIGN in full, executed the 4-file manifest in dependency order (`db.py` before `web.py`/`cli.py` before `tests/test_db.py`), ran the full validation loop, wrote this report. No specialist delegation was warranted — the design's own "Agent Assignment Rationale" section already reasoned this is a single coherent seam change too small to split. |
| 2 | Two of the DESIGN's own literal test-sketch lines (in the new `test_upsert_word_same_lemma_different_lang_coexist` and `test_set_status_scoped_by_lang_leaves_other_lang_row_untouched`) were 104 characters — over this repo's `ruff` `line-length = 100` gate — because of trailing inline comments (`# default == explicit "ja" (byte-invariance)` and `# untouched (schema default)`) | (a) Keep the DESIGN's exact comment text and let `ruff check` fail vs. (b) shorten only the trailing comment text on those two lines, keeping every assertion and all other lines byte-identical to the DESIGN's sketch | (b) | The DESIGN doc itself designates these as "test sketches" (SKILL.md/DESIGN's own testing-strategy note: "If the DESIGN doc's test sketches differ slightly in assertions, follow the DESIGN doc" — this is a comment-length fix, not an assertion change). Faking a green `ruff check` or silently leaving a failing gate were both worse than a two-word comment trim; no assertion, SQL, or behavior changed. Re-verified green after (`ruff check` exit 0, `pytest` still 35 passed). |
| 3 | `ruff format` (no path) would reformat the whole repo; the task instructions allow a full `ruff format .` "since the repo is already clean" but ask to verify scope afterward | (a) Run bare `ruff format .` vs. (b) run `ruff format` scoped to exactly the 4 touched files | (b) | Scoping to the known-touched files and then re-running `ruff format --check .` (which passed clean across all 16 files) gives the same end state with a strictly smaller blast radius and no reliance on an assumption about repo cleanliness — belt-and-suspenders for the "don't touch anything outside the file manifest" hard rule. |

## Deviations from Design

| Deviation | Reason | Impact |
|-----------|--------|--------|
| Two trailing test comments shortened (see Autonomous Decision #2) | `ruff` line-length gate (100 cols); DESIGN's sketch lines were 104 cols with the full comment text | None on behavior — assertions, SQL, and control flow are byte-identical to the DESIGN's sketches; only comment wording shrank |
| `bll/cli.py`'s `dbm.upsert_word(...)` call-site reflowed across multiple lines by `ruff format` | Adding the `lang=args.lang` keyword argument pushed the call past the line-length limit on one line | Purely cosmetic (formatter-driven line wrap); no argument order or value changed |

No other deviations. All exact SQL (`WHERE lang=?` scoping, the INSERT's explicit `lang` column), all exact signatures (`lang: str = "ja"` trailing on all four db.py functions, matching web.py/cli.py shapes from Decision 2/3), the `cmd_process` rejection-as-first-statement placement (Decision 3), and the INSERT-lists-lang choice (Decision 4) were transcribed as specified and passed verification on first execution (aside from the two line-length trims above).

## Files Changed

- Modified: `bll/db.py` — `all_words`, `upsert_word`, `set_status`, `set_note` each gain a trailing `lang: str = "ja"` parameter; all four SQL statements scoped by `lang`; `upsert_word`'s INSERT explicitly lists and binds `lang` instead of relying on the schema default.
- Modified: `bll/web.py` — `GET /api/words` and `GET /api/word/{lemma}/history` gain a `lang: str = "ja"` query parameter; `POST /api/word/{lemma}` reads `lang` from the existing bare-`dict` body (`body.get("lang", "ja")`) and threads it into the existence check and the `set_status`/`set_note` calls.
- Modified: `bll/cli.py` — `process` subparser gains `--lang` (default `"ja"`, no `choices=`); `cmd_process` rejects any non-`"ja"` value as its first statement (stderr message naming issue #16, exit 1, before `dbm.backup()`/`dbm.connect()`); the two existing `dbm.all_words(conn)` / `dbm.upsert_word(...)` call-sites inside `cmd_process` now pass `lang=args.lang`. `cmd_mark`, `cmd_note`, `cmd_words` untouched (Decision 5 — explicit non-goal).
- Modified: `tests/test_db.py` — appended 3 new tests (`test_upsert_word_same_lemma_different_lang_coexist`, `test_set_status_scoped_by_lang_leaves_other_lang_row_untouched`, `test_set_note_scoped_by_lang_leaves_other_lang_row_untouched`); zero existing tests edited.
- No git operations performed (no add/commit/branch/push) and no `gh` calls — left for the pipeline's composer phase.

## Blockers

None.

## Acceptance Test Verification

(Mapped to the DESIGN's own Testing Strategy table and Invariant section — issue #28 body itself was not re-fetched via `gh`, per this run's no-GitHub-writes/no-`gh`-calls constraint; the DESIGN already assembled and restated the issue's acceptance criteria as AT1–AT5.)

| ID | Scenario | Status | Evidence |
|----|----------|--------|----------|
| AT1 | `upsert_word` keeps same-lemma, different-`lang` rows distinct (no cross-lang upsert-over) | Pass | `test_upsert_word_same_lemma_different_lang_coexist` — passing |
| AT2 | `set_status`/`set_note` are `lang`-scoped and leave other-`lang` rows untouched | Pass | `test_set_status_scoped_by_lang_leaves_other_lang_row_untouched`, `test_set_note_scoped_by_lang_leaves_other_lang_row_untouched` — passing |
| AT3 | `--lang` is syntactically accepted for any value (no `choices=` fence) | Pass | Manual `--help` smoke: flag renders as `--lang LANG`, not `--lang {ja}` |
| AT4 | Non-`"ja"` `--lang` is rejected at runtime with a message pointing at issue #16, exit 1, before any DB write | Pass | Manual smoke: nonexistent-file run exits 1 on the lang message, not a file-I/O error |
| AT5 | JA byte-invariance: no `--lang` flag / default `lang="ja"` produces behavior identical to `main` | Pass | Full unmodified pre-existing suite (32 tests, incl. `tests/test_process_e2e_smoke.py`) passes unchanged as part of the 35-passed run; every pre-existing `tests/test_db.py` call keeps its original argument count and keeps passing |

## Status: Complete
