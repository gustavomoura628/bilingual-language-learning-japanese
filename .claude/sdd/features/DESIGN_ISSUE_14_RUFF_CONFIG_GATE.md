# DESIGN — Issue #14: Add ruff config + formatting/lint gate

> Input: `.claude/sdd/_synthesized/DEFINE_ISSUE_14_RUFF_CONFIG_GATE.md`. Small, mechanical
> config+CI change — no data-engineering KB domain applies. Design kept proportional to the
> task: this is adoption of an already-precedented org standard (`second-brain/pyproject.toml`),
> not new architecture. Verified directly against this repo's `pyproject.toml`, `uv.lock`, and
> `.github/workflows/tests.yml`.

## Architecture overview

```text
┌───────────────────────────────────────────────────────────────────────────┐
│  pyproject.toml                                                          │
│  + [tool.ruff] / [tool.ruff.lint]        (Decision 1)                    │
│  + "ruff>=0.8" in [dependency-groups].dev                                │
│                        │                                                  │
│                        ▼ uv lock                                         │
│                  uv.lock (regenerated, ruff pinned + resolved)            │
└───────────────────────────────┬───────────────────────────────────────────┘
                                 │ uv sync --locked --dev
                                 ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  .github/workflows/tests.yml  ::  job "test"                             │
│                                                                            │
│  checkout → setup-python <3.12|3.13> → setup-uv → uv sync --locked --dev │
│         │                                    │                           │
│         │                                    ▼                           │
│         │                       uv run ruff check .        (NEW)         │
│         │                       uv run ruff format --check .  (NEW)      │
│         │                                    │                           │
│         │                                    ▼                           │
│         └──────────────────────────▶ uv run pytest tests/  (unchanged)   │
└───────────────────────────────┬───────────────────────────────────────────┘
                                 │
                    ┌────────────┴────────────┐
                    ▼                          ▼
          lint+format+tests all green    any step fails
          → green check                  → red check, first failing
                                            step identifies the gate
```

Lint/format run **before** pytest — cheaper failure surfaces first, and it matches the issue's
own step ordering ("add ruff steps ... before pytest").

---

## Decision 1 — Ruff config block (values, not open questions)

**Choice:** adopt the DoR-audit's pre-resolved, org-standard block verbatim into `pyproject.toml`:

```toml
[tool.ruff]
line-length = 100
target-version = "py313"   # or "py312" — see Decision 2

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
```

**Rationale:** precedented at `second-brain/pyproject.toml`; DoR-audit assumption #1 already
closed this as a non-decision for this run — design doesn't relitigate it. Flat layout
(`bll/` package, no `src/`) needs no `tool.ruff.src` setting; ruff's default file discovery
(everything under the project root, minus its own excludes) already covers `bll/` and `tests/`.

**Dependency wiring:** add `"ruff>=0.8"` to `[dependency-groups].dev`, alongside the existing
`"pytest>=8.0"`:

```toml
[dependency-groups]
dev = [
    "pytest>=8.0",
    "ruff>=0.8",
]
```

Then `uv lock` regenerates `uv.lock` so CI's `uv sync --locked --dev` installs the exact locked
ruff version and `uv run ruff ...` is reproducible — no environment-supplied ruff, per the
issue's own wiring requirement.

**Alternative rejected:** pinning an exact ruff version (e.g. `"ruff==0.8.0"`) — rejected because
the lockfile already pins the resolved version; a floor (`>=0.8`) is enough to express "reasonably
modern," matching how `pytest>=8.0` is already expressed.

---

## Decision 2 — py313 vs. py312 target: procedure, not a guess

**Context:** the issue names py313 as the goal (matching the org's 3.13+ standard) but
pre-authorizes a py312 fallback if 3.13 breaks dependency resolution — this repo has real
native/CJK-tooling dependencies (`fugashi`, `pykakasi`, `wordfreq[cjk]`, `simplemma`) where a new
Python version is a real compatibility risk, not a formality.

**Procedure (build phase executes this, doesn't assume the answer):**

```bash
uv python install 3.13
uv sync --locked --dev --python 3.13
uv run --python 3.13 pytest tests/
```

**Decision rule:**
- Both commands succeed **and** pytest reports the same `23 passed` → commit to py313:
  `target-version = "py313"` in `pyproject.toml`; `tests.yml`'s `python-version: "3.13"`.
- Either step fails for a dependency reason (resolution conflict, native-build failure, a test
  regression traceable to the interpreter bump) → fall back: `target-version = "py312"`; leave
  `tests.yml` at `"3.12"`. This is not a blocker — the issue's own acceptance criteria and
  DoR-audit assumption #2 pre-authorize it. Log the fallback plainly in the build report and (by
  the composer, later) the PR body — never silently.

**Either branch:** re-run `uv lock` + `uv sync --locked --dev` once the target is finalized, so
`uv.lock` reflects whichever `python-version` CI will actually use — a lockfile generated against
the wrong assumed target is exactly the kind of silent drift this issue exists to prevent.

**Alternative rejected:** adding a 3.12/3.13 build matrix to hedge — rejected as scope creep; the
issue asks for a single bumped version with a logged fallback, not a matrix, and a matrix would
double CI cost for a repo whose own acceptance criteria don't ask for cross-version coverage.

---

## Decision 3 — Three-way commit split (for the composer, not made here)

Design doesn't commit anything, but the edits should stay cleanly separable along the same lines
the issue implies, so the composer can split them without re-deriving intent:

| Commit | Contents | Why separate |
|---|---|---|
| 1. Config + lock | `pyproject.toml` (`[tool.ruff]` block + dev dep), `uv.lock` | The gate's *definition* — reviewable on its own, no code churn noise |
| 2. Mechanical reformat | Whatever `ruff format .` / `ruff check --fix` touch under `bll/`, `tests/` | Zero-behavior-change diff; reviewers can skim for "is this really just formatting" without config noise mixed in |
| 3. CI workflow | `.github/workflows/tests.yml` | The enforcement step — logically last, since it depends on 1 and 2 already being green |

Build should keep these edits mechanically separable (i.e. don't interleave, e.g., a `noqa`
comment edit into the same conceptual bucket as a pure `ruff format` whitespace change) even
though build itself does not commit.

---

## File manifest

| # | File | Action | Purpose | Dependencies |
|---|------|--------|---------|--------------|
| 1 | `pyproject.toml` | Edit | Add `[tool.ruff]` + `[tool.ruff.lint]`, add `ruff` to dev group | None |
| 2 | `uv.lock` | Regenerate (`uv lock`) | Lock ruff + reflect final py313/py312 target | 1 |
| 3 | `bll/*.py`, `tests/*.py` (subset — exact list determined by the tool, not predicted here) | Edit (mechanical) | `ruff format .` + `ruff check --fix` (safe fixes only) | 1, 2 |
| 4 | `.github/workflows/tests.yml` | Edit | Bump `python-version` per Decision 2; insert `ruff check` / `ruff format --check` steps before pytest | 1, 2, 3 (must be green first) |

**Total files:** 4 categories (exact count of #3 depends on what ruff's safe fixes touch —
reported in the build report, not guessed here).

---

## Testing strategy / acceptance mapping

| Acceptance test (from DEFINE) | Verification |
|---|---|
| AC1 — `ruff check .` exits 0 | Run locally in build; re-verified as a CI step once `tests.yml` is edited |
| AC2 — `ruff format --check .` exits 0 | Same |
| AC3 — zero behavior change, suite stays 23 passing | `uv run pytest tests/` before *and* after the ruff pass; both must read `23 passed` |
| AC4 — CI on py313, else logged py312 fallback | Decision 2's procedure output, captured verbatim in the build report |
| AC5 — PR states effective runtime floor; `requires-python` untouched | Build report must flag any UP-rule fix whose syntax needs newer-than-3.9 at runtime, file-by-file, so the composer can write it into the PR body; `git diff` must show no change to the `requires-python` line |

No new runtime code is added by this change, so there is no new unit/integration/E2E surface
beyond "the existing 23 tests still pass" — this is a tooling/gate change, not a feature.

---

## Out of scope (carried from DEFINE, not re-litigated)

New features, unrelated refactors, behavior changes, editing `requires-python`, a version
build-matrix, any git/gh operations (build phase is local-tree-only; branching/commit/PR already
happened or happen outside this run).

## Next step

**Ready for:** `/build .claude/sdd/features/DESIGN_ISSUE_14_RUFF_CONFIG_GATE.md`
