# DESIGN: Issue #11 — Unit coverage for the CLI cue-overlap helper

> Technical design for implementing ISSUE_11_CUE_OVERLAP. **Scope note:**
> this is one pure function and one new test file — most of the standard
> DESIGN_TEMPLATE sections (Pipeline Architecture, Integration Points,
> Observability, Security Considerations, Data Quality Results,
> Configuration) do not apply and are marked N/A rather than padded out,
> per the DEFINE doc's explicit "keep this proportionate" scope note.

## Metadata

| Attribute | Value |
|-----------|-------|
| **Feature** | ISSUE_11_CUE_OVERLAP |
| **Date** | 2026-07-06 |
| **Author** | design-agent (agentspec:workflow:design, invoked via implement skill) |
| **DEFINE** | [DEFINE_ISSUE_11_CUE_OVERLAP.md](../_synthesized/DEFINE_ISSUE_11_CUE_OVERLAP.md) |
| **Status** | Ready for Build |

---

## Architecture Overview

```text
┌──────────────────────────────────────────────────────────┐
│ tests/test_cue_overlap.py  (NEW, test-only)               │
│                                                            │
│  ev(start, end) -> SimpleNamespace   # tiny local stand-in │
│         │                                                  │
│         ▼                                                  │
│  test_* functions build inline en_events lists            │
│         │                                                  │
│         ▼                                                  │
│  bll.cli.overlapping(en_events, start, end, slack=1200)    │  <- UNCHANGED, imported read-only
│         │                                                  │
│         ▼                                                  │
│  assert on returned index list                             │
└──────────────────────────────────────────────────────────┘
```

No runtime component changes. `bll/cli.py` is imported and called, never
edited. No file I/O, no `pysubs2`, no network — everything is inline `int`
millisecond tuples fed into a local `ev()` helper.

---

## Components

| Component | Purpose | Technology |
|-----------|---------|------------|
| `ev(start, end)` helper (new, test-file-local) | Minimal stand-in for a `pysubs2` event — only `.start`/`.end` are read by `overlapping()` | `types.SimpleNamespace` |
| `test_cue_overlap.py` test functions | Exercise `overlapping()`'s two branches (`hits` / slack-fallback) and their boundaries | `pytest`, plain `assert` |
| `bll.cli.overlapping()` (existing, untouched) | System under test | Pure function, already implemented |

---

## Key Decisions

### Decision 1: Stand-in object type for `en_events` items

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** `overlapping()` only reads `.start`/`.end` off each `en_events`
item; the issue explicitly forbids `pysubs2` objects and file I/O in these
tests.

**Choice:** A tiny module-level helper `def ev(start, end): return SimpleNamespace(start=start, end=end)`, used to build inline lists per test.

**Rationale:** Zero new dependencies (`types` is stdlib), reads exactly like
the attribute access the function performs, and keeps each test's fixture
data a one-line list literal — matching the issue's "inline cue tuples"
framing better than a dataclass would (no class boilerplate to scroll past).

**Alternatives Rejected:**
1. A tiny `@dataclass` — rejected: strictly more ceremony than `SimpleNamespace` for a two-field read-only stand-in, no validation benefit here.
2. `collections.namedtuple` — rejected: fine too, but positional `(start, end)` tuples read less clearly at call sites than `ev(1000, 2000)`; keyword clarity wins for a file whose whole point is boundary-precision readability.

**Consequences:**
- Every test constructs its own `en_events` list explicitly (no shared fixture) — intentional, keeps each boundary case self-contained and independently readable.
- No production code touched to support the tests.

---

### Decision 2: Concrete millisecond fixtures per acceptance test

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** The four ACs need concrete numbers that unambiguously land on
one side of each boundary (overlap vs. not; inside vs. outside slack; tie
vs. no tie), traceable back to the hand-traced semantics in the issue.

**Choice:** (`hits` = `[i for i, ev in enumerate(en_events) if max(ev.start, start) < min(ev.end, end)]`; fallback gap = `max(ev.start - end, start - ev.end)`, `slack` default 1200.)

| AT | en_events | JA window (start,end) | Expected | Why |
|----|-----------|------------------------|----------|-----|
| AT-1 | `[ev(1000,2000), ev(5000,6000)]` | (1500,1800) | `[0]` | JA window fully inside event 0; event 1 far away |
| AT-2 | `[ev(1000,2000)]` | (2100,2300) | `[0]` | no overlap (2100 ≥ 2000); gap = max(1000-2300, 2100-2000) = 100 ≤ 1200 |
| AT-3 | `[ev(1000,2000)]` | (4000,4500) | `[]` | gap = max(1000-4500, 4000-2000) = 2000 > 1200 |
| AT-4 | `[ev(1000,2000), ev(3000,4000)]` | (2400,2600) | `[0]` | both gaps = 400 (tie); event 0 encountered first, strict `<` never lets event 1 overwrite |
| AT-5 (bonus) | `[ev(1000,2000), ev(1500,2500)]` | (1400,2100) | `[0, 1]` | both events genuinely overlap the window → `hits` returns both indices |
| AT-6 (bonus) | `[ev(1000,2000)]` | (3200,3300) then (3201,3301) | `[0]` then `[]` | gap==1200 (==slack) still matches (`0 <= gap < best_gap` with `best_gap` initialised to `slack+1`); gap==1201 does not |

**Rationale:** Each row isolates exactly one boundary condition with the
smallest possible fixture — no test depends on another's data, and every
expected value is derivable by hand from the function body quoted above (no
guessing, no need to run the code first to know what "should" happen).

**Alternatives Rejected:**
1. One large shared `en_events` fixture reused across tests via a `pytest.fixture` — rejected: cross-test coupling makes each boundary harder to read in isolation, and the issue's "no fixtures beyond inline cue tuples" line is a direct steer away from this.
2. Property-based testing (`hypothesis`) — rejected: not a project dependency, and the issue asks for the four concrete named boundary scenarios, not generative coverage.

**Consequences:**
- Six focused test functions instead of one parametrized mega-test — matches the existing suite's style (`test_heteronym_reading.py`, `test_backend_gate.py` both use small, independent, plainly-named `def test_*()` functions, no `@pytest.mark.parametrize`).
- AT-6 covers both sides of the `slack`/`slack+1` boundary in one test function (two related assertions), since they're the same fixture event with only the window shifted by 1ms — splitting them would duplicate the `en_events` setup for no readability gain.

---

### Decision 3: Pre-existing-bug handling policy (carried from the issue's HARD CONSTRAINT)

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** `overlapping()` must not be modified under any circumstance in
this PR, even if a test reveals behavior that looks like a bug.

**Choice:** If build encounters behavior that looks surprising relative to
a naive expectation, it asserts the CURRENT behavior (with an inline comment
flagging the surprise) and separately logs a "Possible pre-existing issue"
note in the BUILD_REPORT — it does not touch `bll/cli.py`.

**Rationale:** Directly mandated by the issue's Constraints section and the
composer's HARD CONSTRAINT; a test-only PR that quietly patches production
code would violate its own stated scope.

**Alternatives Rejected:**
1. Fix the bug inline "since it's obviously small" — rejected: explicitly forbidden by the issue; scope creep into a second, unreviewed concern.

**Consequences:**
- Any such finding surfaces in the human-facing report instead of the diff.

**Pre-emptive check against the six fixtures above:** hand-tracing all six
against the quoted function body during DEFINE synthesis found no surprising
behavior — the tie-break (AT-4) and the inclusive boundary (AT-6) both look
unusual at first glance but are both fully explained by the strict `<` in
`gap < best_gap`, which is ordinary and consistent, not a bug. Build should
still re-verify by hand as it writes each assertion, not assume this design
doc's trace is infallible.

---

## File Manifest

| # | File | Action | Purpose | Agent | Dependencies |
|---|------|--------|---------|-------|--------------|
| 1 | `tests/test_cue_overlap.py` | Create | Unit coverage for `bll.cli.overlapping()` per the six fixtures in Decision 2 | (direct — no specialist agent matched; see below) | None |

**Total Files:** 1

---

## Agent Assignment Rationale

This repo (`bilingual-language-learning`) has no project-local sub-agents
registered for build to delegate to via the Task tool; the environment's
general specialist agents (e.g. a generic `test-generator`) are not
namespaced to this project's conventions and would need the same
style-matching context this design doc already carries. Build should write
the file directly ("(direct)" in build-report terms), following Decision 1
and Decision 2 verbatim, plus the style excerpt in Code Patterns below.

| Agent | Files Assigned | Why This Agent |
|-------|----------------|-----------------|
| (direct) | 1 | No project-local specialist test-writing agent found; single small file, design is already fully specified below |

---

## Code Patterns

### Pattern 1: Module header and import boilerplate (match `test_heteronym_reading.py` / `test_backend_gate.py`)

```python
"""Unit coverage for bll.cli.overlapping() -- the helper that aligns JA/EN
subtitle cues by time overlap, falling back to the nearest EN event within
a slack window when none overlap directly (issue #11, deferred out of #8's
timeline.py-focused suite since this helper lives in the CLI module).

Covers: overlapping-cue match (single + multiple), non-overlapping-but-
within-slack fallback, outside-slack no-match, tie-breaking between
equidistant candidates, and the slack/slack+1 inclusive boundary.

Fully offline: en_events are built from a local SimpleNamespace stand-in,
no pysubs2 objects, no file I/O, no network.

Run: pytest tests/test_cue_overlap.py
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bll import cli


def ev(start, end):
    return SimpleNamespace(start=start, end=end)
```

### Pattern 2: One boundary test per AC row (Decision 2's table maps 1:1 to a `def test_*()`)

```python
def test_overlapping_cue_match():
    en_events = [ev(1000, 2000), ev(5000, 6000)]
    assert cli.overlapping(en_events, 1500, 1800) == [0]


def test_fallback_within_slack():
    en_events = [ev(1000, 2000)]
    # gap = max(1000 - 2300, 2100 - 2000) = 100 <= default slack (1200)
    assert cli.overlapping(en_events, 2100, 2300) == [0]


def test_outside_slack_no_match():
    en_events = [ev(1000, 2000)]
    # gap = max(1000 - 4500, 4000 - 2000) = 2000 > default slack (1200)
    assert cli.overlapping(en_events, 4000, 4500) == []


def test_tie_break_first_index_wins():
    en_events = [ev(1000, 2000), ev(3000, 4000)]
    # both gaps == 400 (tie); strict `gap < best_gap` means event 0
    # (encountered first) keeps the win over event 1's equal gap.
    assert cli.overlapping(en_events, 2400, 2600) == [0]


def test_multiple_simultaneous_overlaps():
    en_events = [ev(1000, 2000), ev(1500, 2500)]
    assert cli.overlapping(en_events, 1400, 2100) == [0, 1]


def test_slack_boundary_inclusive_vs_exclusive():
    en_events = [ev(1000, 2000)]
    # gap == slack (1200) still matches: best_gap starts at slack + 1.
    assert cli.overlapping(en_events, 3200, 3300) == [0]
    # gap == slack + 1 (1201) no longer matches.
    assert cli.overlapping(en_events, 3201, 3301) == []
```

**Note to build:** the gap arithmetic in each comment above must be
re-verified against the live `bll/cli.py` at build time (not merely copied
from this doc) — the composer's brief flagged the same care. If drift is
found, recompute the fixture numbers so the *comment*, the *fixture*, and
the *live function* all agree; do not adjust `bll/cli.py`.

---

## Data Flow

```text
1. Test module import time: `ev()` helper defined, `bll.cli` imported.
2. Each test builds its own small `en_events` list inline.
3. Test calls `cli.overlapping(en_events, start, end[, slack])`.
4. Test asserts the exact returned list against the hand-computed expectation.
```

---

## Integration Points

N/A — no external systems; the only integration is the plain Python import
`from bll import cli`, already covered above.

---

## Testing Strategy

| Test Type | Scope | Files | Tools | Coverage Goal |
|-----------|-------|-------|-------|----------------|
| Unit | `bll.cli.overlapping()` boundary behavior | `tests/test_cue_overlap.py` | pytest | All 4 required ACs + 2 bonus boundary cases |

No integration or e2e tier applies — this is a pure-function unit-test
addition; `tests/test_process_e2e_smoke.py` already covers `overlapping()`
implicitly end-to-end and is explicitly out of scope (owned by a different,
concurrent branch per the composer's brief).

---

## Error Handling

N/A — `overlapping()` has no error paths (no exceptions raised, no invalid
input handling in scope); the issue's ACs are purely about which indices
come back under which timing configurations.

---

## Configuration

N/A — no new configuration surface. `slack` is an existing parameter of
`overlapping()` with a default of `1200`; tests exercise both the default
and (for AT-6) values right at its boundary, but do not add configuration.

---

## Security Considerations

N/A — offline pure-function tests, no secrets, no network, no user input.

---

## Observability

N/A — test-only change; nothing to log, trace, or monitor at runtime.

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-07-06 | design-agent | Initial version, scoped to issue #11's 4 ACs + 2 cheap bonus boundary cases |

---

## Next Step

**Ready for:** `/build .claude/sdd/features/DESIGN_ISSUE_11_CUE_OVERLAP.md`
