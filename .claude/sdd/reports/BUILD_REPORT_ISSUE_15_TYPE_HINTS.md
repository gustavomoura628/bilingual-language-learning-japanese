# BUILD REPORT — Issue #15: Type hints on public function signatures

**Status: Complete**

Implements issue #15. Design artifact: `.claude/sdd/features/DESIGN_ISSUE_15_TYPE_HINTS.md`.

## Files changed

`git diff --stat` (working tree vs. branch base, uncommitted — composer commits):

```
 .github/workflows/tests.yml |   3 +
 bll/cli.py                  |  25 ++--
 bll/db.py                   |  62 ++++++---
 bll/gloss.py                |  33 +++--
 bll/timeline.py             |  48 ++++---
 pyproject.toml              |  19 +++
 uv.lock                     | 321 ++++++++++++++++++++++++++++++++++++++++++--
 7 files changed, 440 insertions(+), 71 deletions(-)
```

No files outside the allowed set were touched. `.claude/sdd/features/DESIGN_ISSUE_15_TYPE_HINTS.md`
was present (untracked) before this build started and was only read, never modified.

## Verification transcript

All five loop commands plus the sanity check, final clean run:

```
$ uv sync --locked --dev
Resolved 67 packages in 34ms
Checked 32 packages in 8ms
exit: 0

$ uv run ruff check .
All checks passed!
exit: 0

$ uv run ruff format --check .
15 files already formatted
exit: 0

$ uv run mypy bll/
Success: no issues found in 9 source files
exit: 0

$ uv run pytest tests/
tests/test_backend_default.py ..                                         [  8%]
tests/test_backend_gate.py .                                             [ 13%]
tests/test_db.py .........                                               [ 52%]
tests/test_heteronym_reading.py ..                                       [ 60%]
tests/test_process_e2e_smoke.py ..                                       [ 69%]
tests/test_timeline.py .......                                           [100%]
============================== 23 passed in 0.94s ==============================
exit: 0

$ uv run bll --help
usage: bll [-h] [--db DB]
           {process,render,words,known,ignore,learning,note,serve,bootstrap,stats} ...
...
exit: 0
```

`uv lock` was run once up front (after the `pyproject.toml` edit) and resolved cleanly: added
`mypy==2.1.0` (satisfies the `>=1.13` floor; current PyPI release per the design doc) plus its
transitive deps `ast-serialize`, `librt`, `mypy-extensions`, `pathspec`, `typing-extensions`.

## Annotation coverage before -> after

| Module | Before | After |
|---|---|---|
| `bll/db.py` | 0/18 | 18/18 |
| `bll/timeline.py` | 0/18 | 18/18 |
| `bll/gloss.py` | 0/6 | 6/6 |
| `bll/cli.py` | 0/27 | 9/27 (`main` + the 8 `cmd_*`; the other 18 helpers are deliberately out of scope per the design) |

Counted by grepping each file for `def ` vs. `) -> ` (return-annotation presence); db.py/timeline.py/
gloss.py/cli.py entry-surface counts all verified to match the design doc's own per-module tallies
exactly (gloss.py's raw `") -> "` grep hit 8 due to two unrelated comment/f-string substrings
containing that literal text — the real signature count, checked by line, is 6/6).

## Autonomous decisions

1. **Scoped `ruff format` to the four touched files instead of `.`.** The design doc's build
   instructions say to run `uv run ruff format .` and let it settle wrapping. I ran
   `uv run ruff format bll/db.py bll/timeline.py bll/gloss.py bll/cli.py` instead, to make it
   structurally impossible to touch a file outside the allowed set even if some unrelated file in
   the repo had turned out not to be format-clean. Result was a no-op either way (all four files'
   manual wrapping already matched ruff's preferred style), and a full-repo
   `ruff format --check .` afterward confirms zero reflow anywhere else ("15 files already
   formatted").

2. **`@typing.no_type_check` on `cmd_process`, added `import typing` to `bll/cli.py`.** This is
   the design doc's own pre-sanctioned fallback (section 5's risk table and the "time-box" note),
   and it triggered: annotating `cmd_process`'s signature turned on full body-checking and mypy
   raised 17 errors, all inside that one 620-line function, none in the other 8 entry-surface
   functions. Root causes, traced before reaching for the fallback:
   - `seen` is used as a plain `bool` flag earlier in the function (season-lookahead block:
     `after, seen = [], False`) and reused as an unrelated `set()` later (occurrence-dedup loop) —
     a genuine local-variable-name collision across two disjoint regions of the same function
     scope. Harmless at runtime (the two uses never overlap), but it is exactly why the design's
     hand-trace prediction of "none in cmd_process despite its size" didn't hold up against mypy.
   - Similarly, a `row` binding is reused across two different types (`sqlite3.Row` from a
     `for` loop vs. `Row | None` from a `.get()` call) in two other places.
   - Several bare `{}`/`[]` locals needed "wait and see" annotations mypy couldn't resolve on its
     own once the function body was checked (`future`, `future_traj`, `claims`, `tiered`, `left`,
     `vetoed`, `kept`, `per_cue`, `replaced`).
   - Four call sites pass `wid = dbm.upsert_word(...)` (correctly typed `int | None` per the
     design, since `sqlite3.Cursor.lastrowid` is nullable in typeshed) straight into
     `record_sighting`/`record_variant`/`touch_last_seen`/`stamp_learned`, all of which require
     plain `int`.
   None of this is a hard rule for "quick fix" — properly resolving it would mean renaming
   shadowed locals and/or adding `assert wid is not None` guards, i.e. actual code edits beyond
   "annotations + config only," and the assert would be a genuine (if inert) new runtime
   statement, which the hard rule on zero behavior change forbids. `@typing.no_type_check` keeps
   the signature as accurate, checked-by-callers documentation while opting the body out, with
   zero behavior change — exactly the trade the design pre-authorized.

3. **Added a second `[[tool.mypy.overrides]]` block** — `module = ["bll.jmdict", "bll.web"]`,
   `ignore_errors = true` — beyond the design doc's literal section-3 snippet. Triggered by the
   build task's own verification-loop instructions, which explicitly anticipate mypy raising
   errors in unannotated out-of-scope files "purely from your config making those files newly
   walked" and directs "the least invasive resolution consistent with the design... should mostly
   be skipped/permissive." `bll/jmdict.py` and `bll/web.py` both hit this: `ignore_missing_imports`
   doesn't silence module-level type inference on untyped containers, and `bll/web.py` in
   particular has several pre-existing, partially-annotated FastAPI route handlers whose old
   implicit-Optional defaults (`at: int = None`, etc.) are no longer accepted by mypy's current
   default (`no_implicit_optional=True`). Neither file is touchable under the allowed-files list,
   so a per-module override was the only compliant lever. `bll/jp.py` and `bll/bootstrap.py` are
   also unannotated and now walked, but produced zero errors, so they were deliberately left
   without an override (no reason to widen the blast radius further than the evidence required).

## Deviations from design

- The design doc predicted "none [of the mypy body-check friction] in `cmd_process` despite its
  size, because nearly everything it touches resolves to `Any`" (section 2, risk-table row 2).
  That did not hold in practice — see autonomous decision 2 above. The design doc's own section 5
  explicitly pre-authorized exactly this fallback for exactly this situation, so this is logged as
  a deviation from the doc's *prediction*, not from its *sanctioned plan*.
- One extra `[[tool.mypy.overrides]]` block beyond section 3's literal snippet (autonomous
  decision 3 above), authorized by the build task's own verification-loop contingency instructions
  rather than by section 3 itself.

## Proposed follow-ups

- `bll/cli.py::cmd_process`: the local name `seen` is reused for two unrelated purposes (a `bool`
  "have we passed the current file yet" flag in the season-lookahead block, and later a `set()`
  for de-duplicating overlapping-EN-cue tuples). Not a runtime bug — the two uses are in disjoint
  code paths — but a legibility smell worth a rename in a future pass. Left untouched here since
  renaming an identifier is a code change beyond this issue's "annotations + config only" scope.
- `bll/cli.py::cmd_process`: `wid = dbm.upsert_word(...)` (`int | None`) flows directly into four
  calls expecting plain `int` (`record_sighting`, `record_variant`, `touch_last_seen`,
  `stamp_learned`). Always a real `int` in practice (post-INSERT/lookup), but an explicit
  `assert wid is not None` would be the principled fix once `cmd_process` is ever un-suppressed.
  Not added here — it would be a new runtime statement, which the zero-behavior-change rule for
  this issue forbids.
- `bll/web.py`: several FastAPI route handlers (`stats`, `words`, `browse`, `bootstrap`) use the
  old implicit-Optional pattern (`at: int = None`, `dir: str = None`, `body: dict = None`) that
  PEP 484 disallows and current mypy no longer infers automatically, plus one `params = ()` later
  reassigned to a 1-element tuple — the identical pattern fixed via a local annotation in
  `cli.py::cmd_words`. Currently suppressed wholesale via `ignore_errors` since `bll.web` is out of
  scope for this issue; worth a real annotation pass if `bll.web` is ever brought under the mypy
  gate.
- `bll/jmdict.py`: module-level `_gloss_token_cache = {}` has no annotation (harmless, currently
  suppressed by the same override). Nothing else notable turned up there.

## Blockers

None.
