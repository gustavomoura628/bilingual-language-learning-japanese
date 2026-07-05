# DESIGN — Issue #6: Add GitHub Actions CI to run the test suite on every pull request

> Input: `.claude/sdd/_synthesized/DEFINE_ISSUE_6_CI_PYTEST_WORKFLOW.md`. This is a small,
> well-scoped CI/test-mechanics change — no data-engineering KB domain applies, so no KB pattern
> loading was performed; the design below follows standard GitHub Actions / pytest conventions,
> verified directly against this repo's `pyproject.toml`, `uv.lock`, and the three existing test
> files.

## Architecture overview

```text
┌──────────────────────────────────────────────────────────────────────┐
│  pull_request targeting main                                         │
└───────────────────────────────┬──────────────────────────────────────┘
                                 │ triggers
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│  .github/workflows/tests.yml  ::  job "test"                         │
│                                                                        │
│  checkout → setup-python 3.12 → setup-uv → uv sync --locked --dev    │
│                                                        │              │
│                                                        ▼              │
│                                          uv run pytest tests/         │
│                        (3 files collected as real def test_*() items)│
└───────────────────────────────┬──────────────────────────────────────┘
                                 │
                    ┌────────────┴────────────┐
                    ▼                         ▼
             all asserts pass           any assert fails
             exit 0 → green check       exit 1 → red check, pytest-
                                         reported (not a crashed job)
```

---

## Decision 1 — Workflow file: path, trigger, steps

**File:** `.github/workflows/tests.yml`

**Naming rationale:** `tests.yml`, not `ci.yml` — the name stays scoped to what the job actually
does (run the test suite). A future, separately-scoped lint workflow (explicitly out of scope here,
see Scope boundaries) can be added later as its own file without renaming this one.

**Trigger:** `pull_request` targeting `main` only — matches DEFINE success criterion 1 exactly; no
`push` trigger added (not requested).

**Steps** (satisfies DEFINE criteria 1–3): checkout → set up Python 3.12 (matches
`deploy/Dockerfile`'s pin; `requires-python = ">=3.9"` allows it, no matrix per constraints) →
install `uv` → `uv sync --locked --dev` (installs the package + its runtime dependencies **and**
the `dev` group containing pytest, in one command, from the committed lock file — see Decision 2)
→ `uv run pytest tests/`.

```yaml
name: Tests

on:
  pull_request:
    branches: [main]

jobs:
  test:
    name: pytest
    runs-on: ubuntu-latest
    steps:
      - name: Check out repository
        uses: actions/checkout@v6

      - name: Set up Python 3.12
        uses: actions/setup-python@v6
        with:
          python-version: "3.12"

      - name: Install uv
        uses: astral-sh/setup-uv@v8.2.0

      - name: Install package, runtime deps, and test tooling
        run: uv sync --locked --dev

      - name: Run tests
        run: uv run pytest tests/
```

Notes:

- `--locked` makes CI fail fast if `pyproject.toml` and the committed `uv.lock` ever drift (e.g. a
  future PR edits one without the other), instead of silently re-resolving. It works here because
  this same change also regenerates `uv.lock` (see Decision 2 / file manifest) so the lock committed
  on this branch already reflects the new `dev` group.
- No `--all-extras`: the `web` extra (`fastapi`/`uvicorn`) is not needed by any of the three tests
  (verified in DEFINE's constraints) — installing it would be unrequested scope.
- Action versions (`checkout@v6`, `setup-python@v6`, `setup-uv@v8.2.0`) are current majors/latest
  release as of this design; confirm nothing newer landed by the time this is actually built.
- No `permissions:` block added — the job only reads the repo and runs tests, which fits the
  default `GITHUB_TOKEN` read-only permissions.

---

## Decision 2 — Where `pytest` becomes available

DEFINE poses this explicitly as a choice. Options considered:

| Option | Declared where | CI install | Local/CI parity | Reproducible (pinned) |
|--------|---------------|------------|------------------|------------------------|
| A. CI-step-only `pip install pytest` | Nowhere in the repo | Extra ad-hoc step | No — a contributor running tests locally has to separately remember/install pytest | No, unless the version is hardcoded a second time directly in the YAML |
| B. `[dependency-groups] dev = [...]` (PEP 735, uv-native) | `pyproject.toml` + `uv.lock` | Folded into the existing `uv sync` install step (`--dev`) | Yes — `uv sync --dev` locally installs the exact same pytest as CI | Yes — pinned via `uv.lock` |
| C. `[project.optional-dependencies] dev = [...]` | `pyproject.toml` + `uv.lock` | `uv sync --extra dev` | Yes | Yes |

**Chosen: Option B — `[dependency-groups] dev = ["pytest>=8.0"]`, consumed via `uv sync --dev`.**

Rationale:

- The repo already commits `uv.lock` as the single source of truth for dependency resolution;
  Option B keeps pytest in that same system (pinned, hash-locked, reproducible) instead of an
  unpinned, CI-only side channel (Option A).
- `[dependency-groups]` (PEP 735) is semantically the right table for this: entries there are
  **never published** as installable extras of the `bll` distribution. `[project.optional-
  dependencies]` (Option C), by contrast, **is** published metadata — `web` already legitimately
  lives there because it's a real optional runtime feature (the operator console) an end user might
  install. Putting a dev-only test tool in the same table as a real user-facing extra would blur
  that distinction; `dependency-groups` exists precisely to keep them separate.
- `dev` is uv's special-cased default group — `uv sync` alone would already include it — but the
  workflow spells out `--dev` explicitly for readability (a CI file is executed documentation; it
  shouldn't rely on an implicit uv default a future reader may not know about).
- Single tool for everything (`uv` for both the project's own deps and its dev tooling), rather than
  mixing `uv sync` for the package with a separate untracked `pip install pytest`.

**Build-phase note (mechanical, not hand-edited):** after adding the `dev` group to
`pyproject.toml`, run `uv lock` (or a plain `uv sync`, without `--locked`) once locally to
regenerate `uv.lock` with pytest's resolved version and hashes, then commit the updated lock file
alongside `pyproject.toml`. The CI workflow's `--locked` flag is what enforces that this lock stays
current going forward — CI itself must never be the place the lock gets (re)generated.

```toml
# pyproject.toml — add this table (placement: after [project.optional-dependencies])
[dependency-groups]
dev = [
    "pytest>=8.0",
]
```

---

## Decision 3 — Converting the three test files to real pytest tests

### The general pattern

Every one of the three files has the same shape today, and the same shape after conversion:

| | Before | After |
|---|--------|-------|
| Accumulator | module-level `fails = []` + `check(name, got, want)` helper | removed entirely |
| Checks | `check("name", got, want)` calls at **module level** (run at import time) | each becomes a **bare `assert`** inside a `def test_*():` function |
| Failure signal | `if fails: ...; sys.exit(1)` at module level | removed — an `assert` failure raises `AssertionError`, which pytest reports per-test; no `SystemExit` during collection |
| Usage docstring | `Run: python tests/test_x.py (exits non-zero on any failure)` | `Run: pytest tests/test_x.py` |
| Imports / `sys.path.insert(...)` shim | present | **unchanged** — harmless once `bll` is installed via `uv sync`; left alone to keep the diff to only the accumulator→function/assert conversion |
| Raw monkeypatching (`module.attr = fake`) | present in 2 of the 3 files, never restored | converted to pytest's built-in `monkeypatch` fixture — **not just style**, see callout below |

**Why the mechanical failure modes described in DEFINE happen:** with everything at module level,
pytest's collector imports the file to discover tests; it finds zero `def test_*` items (exit 5,
"no tests collected") when all checks pass, or the module-level `sys.exit(1)` fires *during the
import itself* when any check fails, which pytest's collector doesn't expect from a plain import
(INTERNALERROR, exit 3). Moving every check inside a `def test_*():` function is what fixes both:
pytest now has real items to collect, and a failure becomes a normal, per-test `AssertionError`
rather than a `SystemExit` escaping collection.

### Design callout — `monkeypatch` fixture is required, not just idiomatic

`tests/test_backend_default.py` does `cli.cmd_process = _fake_process` and
`tests/test_backend_gate.py` does `bootstrap.reachable = ...` / `gloss.gloss_and_align = ...` —
**raw, unrestored** module-attribute reassignments. Today that's safe because each file runs as its
own OS process (`python tests/test_X.py` run separately). Once `pytest tests/` collects and executes
all three files **in one process**, an unrestored patch from one file leaks into the next:

- `bll/cli.py`'s `main()` rebuilds its argparse parser on every call and binds
  `set_defaults(func=cmd_process)` by looking up the **current** module-global `cmd_process` each
  time (confirmed by reading `bll/cli.py`) — which is exactly why the original test's patch-before-
  call trick works at all. Collected in file order (`test_backend_default.py` before
  `test_backend_gate.py`), an unrestored `cli.cmd_process = _fake_process` from the first file would
  make the second file's `cli.main([..., "process", ...])` dispatch to the stale fake instead of the
  real `cmd_process` — so the gate's reachability check and `return 1` never run, `rc` comes back
  `0`, and `test_backend_gate`'s assertion fails for a reason that has nothing to do with its own
  logic.
- Symmetrically, an unrestored `gloss.gloss_and_align = lambda *a, **k: ...` from
  `test_backend_gate.py` would make `test_backend_default.py`'s
  `inspect.signature(gloss.gloss_and_align)` introspect the fake lambda instead of the real function
  — `sig.parameters["backend"]` would `KeyError`, a crash, not a clean assertion failure.

Using pytest's built-in `monkeypatch` fixture (`monkeypatch.setattr(...)`) instead of a raw
assignment auto-restores the original attribute at the end of each test function, which eliminates
this cross-file pollution risk entirely. This is exactly what "same monkeypatching... as-needed"
(DEFINE's resolved assumption) authorizes: same target, same fake behavior, same call — only the
teardown mechanics change, which is squarely a collection/execution-mechanics fix, not a check
change. (`tests/test_heteronym_reading.py` does no monkeypatching at all — nothing to convert there.)

### Worked example — `tests/test_backend_default.py` (before → after)

**Before** (current file, 2 checks, both run at import time):

```python
#!/usr/bin/env python3
"""Regression test: the alignment backend must never silently default to the
paid `claude` path. ...
Run: python tests/test_backend_default.py   (exits non-zero on any failure)
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bll import cli, gloss

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")


captured = {}


def _fake_process(args):
    captured["backend"] = args.backend
    return 0


cli.cmd_process = _fake_process
cli.main(["process", "a.ja.srt", "b.en.srt"])
check("cli --backend default", captured.get("backend"), "ollama")

sig = inspect.signature(gloss.gloss_and_align)
check("gloss_and_align backend default",
      sig.parameters["backend"].default, "ollama")

if fails:
    print("FAIL:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("PASS: backend never silently defaults to the paid claude path "
      "(cli + gloss_and_align both default to ollama)")
```

**After** (2 `def test_*():` functions, bare `assert`, `monkeypatch` fixture):

```python
"""Regression test: the alignment backend must never silently default to the
paid `claude` path.

`bll process` with no `--backend` used to default to `claude`, which shells out
to the Claude CLI and spends the user's tokens with no warning. Both the CLI
argument and the `gloss_and_align()` signature now default to the local, no-cost
`ollama` path; `claude` is an explicit, disclaimed opt-in (see the backend gate
in `cli.cmd_process`).

Run: pytest tests/test_backend_default.py
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bll import cli, gloss


def test_cli_process_defaults_backend_to_ollama(monkeypatch):
    # The CLI `process` command must default --backend to ollama. This is the
    # active bug: it defaulted to claude, so a bare `bll process` spent tokens.
    # Patch the dispatch target (bound via set_defaults when main() builds the
    # parser) to capture the resolved args without running a real process.
    captured = {}

    def _fake_process(args):
        captured["backend"] = args.backend
        return 0

    monkeypatch.setattr(cli, "cmd_process", _fake_process)
    cli.main(["process", "a.ja.srt", "b.en.srt"])
    assert captured.get("backend") == "ollama"


def test_gloss_and_align_defaults_backend_to_ollama():
    # The library entry point must not silently default to claude either --
    # defense in depth for any caller that omits the backend.
    sig = inspect.signature(gloss.gloss_and_align)
    assert sig.parameters["backend"].default == "ollama"
```

What changed and what didn't: dropped the now-meaningless `#!/usr/bin/env python3` shebang (the
file is no longer executed directly); updated the docstring's `Run:` line; removed `fails`/`check`;
`captured = {}` moved from module scope to the one test function that uses it; the two checks became
two test functions (independent concerns — CLI arg default vs. library signature default — so
"per-test reporting" reads naturally as two separate pass/fail items); `cli.cmd_process = ...`
became `monkeypatch.setattr(cli, "cmd_process", ...)`. **Same two comparisons, same expected value
(`"ollama"`) both times.** The `sys.path.insert` line and both imports are untouched.

### `tests/test_backend_gate.py` — after (apply the same pattern)

Kept as **one** test function: both assertions check facets of the same single action (one
`cli.main([...])` call), so splitting would mean duplicating the whole tempdir/subtitle-content
setup for no benefit. `tempfile.TemporaryDirectory()` becomes pytest's built-in `tmp_path` fixture
(same behavior: an isolated, auto-cleaned per-test directory) — an in-scope "fixture-as-needed"
swap, not a check change.

```python
"""Regression test: the backend gate must STOP when the local (ollama) path is
selected but unreachable -- it must never silently fall through to the paid
`claude` backend.

`test_backend_default.py` locks the default *values*; this locks the gate
*behavior*. A future refactor that drops the reachability pre-flight, or routes
an unreachable-ollama run to claude, fails here -- which is the exact regression
class of the original bug.

Run: pytest tests/test_backend_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bll import bootstrap, cli, gloss

JA = ("1\n00:00:01,000 --> 00:00:03,000\n世界を変える約束だ。\n\n"
      "2\n00:00:04,000 --> 00:00:06,000\nその約束は世界の希望だ。\n")
EN = ("1\n00:00:01,000 --> 00:00:03,000\nA promise to change the world.\n\n"
      "2\n00:00:04,000 --> 00:00:06,000\nThat promise is the world's hope.\n")


def test_backend_gate_stops_on_unreachable_ollama(monkeypatch, tmp_path):
    # Simulate "ollama is not running", and trap any call into the aligner so
    # we can prove the run stopped at the gate before reaching either backend.
    monkeypatch.setattr(bootstrap, "reachable", lambda *a, **k: False)
    align_calls = []
    monkeypatch.setattr(
        gloss, "gloss_and_align",
        lambda *a, **k: align_calls.append(k.get("backend", "?")) or {},
    )

    ja, en = tmp_path / "ep.ja.srt", tmp_path / "ep.en.srt"
    ja.write_text(JA)
    en.write_text(EN)

    # No --backend -> defaults to ollama; ollama is "unreachable" (patched above).
    rc = cli.main([
        "--db", str(tmp_path / "t.db"), "process", str(ja), str(en),
        "-o", str(tmp_path / "o.ass"), "--no-dict", "--min-count", "1",
    ])

    assert rc == 1               # no-backend run stops when ollama is unreachable
    assert align_calls == []     # aligner never invoked on the default path
```

Same two comparisons (`rc == 1`, `align_calls == []`), same setup content (JA/EN text, same CLI
args), same patched targets and same fake behaviors.

### `tests/test_heteronym_reading.py` — after (apply the same pattern)

Splits into 2 functions along the file's own existing section boundaries (comment `# 1.`/`# 2.` vs.
`# 3.`): one for the `reading()` lookup-table checks, one for the `apply_sense_first` end-to-end
mutation. No monkeypatching in this file (none in the original either — `conn=None` is passed
through unchanged). `lk = jmdict.load()` stays at module level, unchanged: that's already exactly
how it runs today (module-level, computed once, before any check), so leaving it there is zero
behavior change.

```python
"""Regression tests for heteronym per-injection readings.
... (docstring body unchanged)
Run: pytest tests/test_heteronym_reading.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bll import cli, jmdict

lk = jmdict.load()


def reading(lemma, en):
    e = jmdict.entry_for_en(lemma, lk.get(lemma), en)
    return e["r"][0] if e else None


def test_heteronym_sense_first_reading():
    # The core case: 角 reads つの over horn (and its plural), but かど/かく for
    # the other senses -- and ties / no-match return None so the caller keeps
    # the stored reading.
    assert reading("角", "horn") == "つの"
    assert reading("角", "horns") == "つの"        # plural via lemmatizer
    assert reading("角", "angle") == "かく"
    assert reading("角", "edge") == "かど"
    assert reading("角", "corner") is None          # かど/すみ tie -> keep fallback
    assert reading("方", "way") is None             # かた/ほう tie -> keep fallback
    assert reading("方", "person") == "かた"

    # The "horn" trap: entry かく has 'Chinese "horn" constellation' (quoted,
    # not parenthesised) so a naive token match would tie. Exact-gloss-match on
    # the つの entry must still win uniquely.
    assert reading("角", "horn") == "つの"


def test_apply_sense_first_plan_mutation():
    # apply_sense_first end-to-end: only flips genuine heteronyms whose
    # reading actually changes; everything else is a byte-for-byte no-op.
    plan = [
        {"cue": 0, "en_word": "horn", "lemma": "角", "reading": "かど",
         "exposure_before": 0, "recency_gap": 0},
        {"cue": 1, "en_word": "corner", "lemma": "角", "reading": "かど",
         "exposure_before": 0, "recency_gap": 0},
        {"cue": 2, "en_word": "today", "lemma": "今日", "reading": "きょう",
         "exposure_before": 0, "recency_gap": 0},
    ]
    changed = cli.apply_sense_first(plan, lk, conn=None, use_romaji=True)

    assert changed == 1
    assert plan[0]["reading"] == "つの"       # horn injection -> つの
    assert plan[0].get("note") == "角 (tsuno) = horn; antler"
    assert plan[1]["reading"] == "かど"       # corner injection unchanged
    assert plan[1].get("note") is None
    assert plan[2]["reading"] == "きょう"     # 今日 single-reading untouched
    assert plan[2].get("note") is None
```

All 8 checks from section 1+2 and all 7 from section 3 of the original are present with identical
values; only `plan`'s construction and the `apply_sense_first` call moved from module scope into
the one test function that uses them (test-local state, not shared — a pure mechanics move).

---

## File manifest

No specialist sub-agents apply to this change — it is a solo CI/test-mechanics task, not a domain
area (e.g. no data pipeline, no infra-as-code, no frontend). Every file is `(general)`.

| File | Action | Purpose | Agent | Rationale |
|------|--------|---------|-------|-----------|
| `.github/workflows/tests.yml` | Create | Runs `pytest tests/` on every PR to `main` | (general) | New GitHub Actions workflow; no specialist for CI YAML |
| `pyproject.toml` | Modify | Add `[dependency-groups] dev = ["pytest>=8.0"]` | (general) | Declares pytest as a dev dependency (Decision 2) |
| `uv.lock` | Modify (mechanical — regenerate, don't hand-edit) | Pins pytest's resolved version/hashes after the `pyproject.toml` change | (general) | Keeps `--locked` CI installs reproducible |
| `tests/test_backend_default.py` | Modify | Convert accumulator pattern → 2 `def test_*():` functions | (general) | Collection-mechanics conversion (Decision 3) |
| `tests/test_backend_gate.py` | Modify | Convert accumulator pattern → 1 `def test_*():` function | (general) | Collection-mechanics conversion (Decision 3) |
| `tests/test_heteronym_reading.py` | Modify | Convert accumulator pattern → 2 `def test_*():` functions | (general) | Collection-mechanics conversion (Decision 3) |

---

## Scope boundaries (explicit — confirmed against DEFINE's constraints)

- **No ruff / lint gate.** No lint configuration exists anywhere in the repo today; none is added by
  this workflow or this change.
- **No Python version matrix.** Single pinned version, 3.12, matching `deploy/Dockerfile`;
  `requires-python = ">=3.9"` is satisfied without testing every supported version.
- **No coverage reporting or badges.** Not part of this workflow.
- **No change to any test's actual assertions or values.** Every `check(name, got, want)` call maps
  to exactly one `assert got == want` (or `is None`/`is not None` where the original compared
  against `None`) with the same operands — verified above for all three files. Only collection
  mechanics (accumulator → functions, `sys.exit` → `assert`, raw monkeypatching →
  `monkeypatch`/`tmp_path` fixtures) change.
- **No new pytest configuration file / `conftest.py` / `tests/__init__.py`.** The workflow invokes
  `pytest tests/` with an explicit path (matching DEFINE's literal wording), so discovery
  configuration isn't needed; the existing per-file `sys.path.insert` shim is left in place and is
  harmless once `bll` is installed into the CI environment.
- **No `git`/`gh` actions taken as part of this design step** — branch, commits, and PR are a later
  phase's job.

---

## Traceability — DEFINE success criteria → design element

| # | Success criterion | Satisfied by |
|---|--------------------|--------------|
| 1 | Workflow triggers on `pull_request` targeting `main` | `on.pull_request.branches: [main]` (Decision 1) |
| 2 | Job installs the package + runtime deps before testing | `uv sync --locked --dev` step (Decisions 1–2) |
| 3 | `pytest` actually collects and executes all three files as test items | Accumulator → `def test_*():`/`assert` conversion (Decision 3) removes both failure modes (exit 5 "no tests collected", exit 3 `INTERNALERROR`) |
| 4 | No-op PR shows green | All converted asserts pass → `pytest` exits 0 |
| 5 | Reintroduced failing assertion shows red, pytest-reported (not crashed) | `assert` failures raise `AssertionError`, reported per-test by pytest; no module-level `sys.exit` remains to crash collection |
