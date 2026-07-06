# DESIGN — Issue #15: Type hints on public function signatures

> Input: `.claude/sdd/_synthesized/DEFINE_ISSUE_15_TYPE_HINTS.md`. Small, mechanical
> annotation+config change — no data-engineering KB domain applies. Grounded directly against
> `bll/db.py`, `bll/timeline.py`, `bll/gloss.py`, `bll/cli.py`, `pyproject.toml`,
> `.github/workflows/tests.yml` (full reads), and mypy's own docs via Context7 (`/python/mypy`:
> `config_file.md`, `runtime_troubles.md`, `type_inference_and_annotations.md`,
> `common_issues.md`, `more_types.md`) for three load-bearing semantics that are easy to get
> wrong from memory (below). Current mypy release confirmed via web search: 2.1.0 (PyPI).

## Architecture overview

```text
┌───────────────────────────────────────────────────────────────────────────┐
│  pyproject.toml                                                          │
│  + "mypy>=1.13" in [dependency-groups].dev                               │
│  + [tool.mypy]  global: ignore_missing_imports = true                    │
│                  (deliberately NO python_version — see Decision 2)       │
│  + [[tool.mypy.overrides]] disallow_untyped_defs = true                  │
│        module = ["bll.db", "bll.timeline", "bll.gloss"]   <- NOT bll.cli │
│                        │                                                  │
│                        ▼ uv lock                                         │
│                  uv.lock (regenerated: mypy + transitives resolved)      │
└───────────────────────────────┬───────────────────────────────────────────┘
                                 │ uv sync --locked --dev
                                 ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  bll/db.py        18/18 defs annotated  ─┐                                │
│  bll/timeline.py  18/18 defs annotated  ─┼─ hard-gated, each file gains  │
│  bll/gloss.py      6/6  defs annotated  ─┘  `from __future__ import      │
│                                              annotations` (Decision 1)    │
│                                                                           │
│  bll/cli.py        9/~27 defs annotated (main + 8 cmd_*) — best-effort,  │
│                     NOT gated (Decision 3). ~18 helpers (injectable_     │
│                     counts, render_plan, apply_sense_first, ...) stay    │
│                     untouched. Also gains the future-import (main's      │
│                     `argv` uses `| None`) + one local fix (Decision 4).  │
└───────────────────────────────┬───────────────────────────────────────────┘
                                 │
                                 ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  .github/workflows/tests.yml  ::  job "test"                            │
│  ruff check . → ruff format --check . → uv run mypy bll/ (NEW) →        │
│  uv run pytest tests/                                                    │
└───────────────────────────────────────────────────────────────────────────┘
```

mypy runs after both ruff steps and before pytest — cheapest static checks fail fastest,
matching the DEFINE doc's own ordering and issue #14's precedent for this same slot.

---

## 1. Per-module annotation plan

### `bll/db.py` (18/18 defs — hard-gated)

No `Any` needed anywhere in this file — every signature resolves to a concrete type. Needs
one new import line: `from __future__ import annotations` (right after the module docstring,
before `import os`) — see Decision 1 for why.

```python
def _migrate(conn):                                                        -> def _migrate(conn: sqlite3.Connection) -> None:
def _backfill_episode_meta(conn):                                          -> def _backfill_episode_meta(conn: sqlite3.Connection) -> None:
def _backfill_learned(conn, threshold=10):                                 -> def _backfill_learned(conn: sqlite3.Connection, threshold: int = 10) -> None:
def connect(path=None):                                                    -> def connect(path: str | None = None) -> sqlite3.Connection:
def clock(conn):                                                           -> def clock(conn: sqlite3.Connection) -> int:
def backup(path=None, keep=10):                                            -> def backup(path: str | None = None, keep: int = 10) -> str | None:
def all_words(conn):                                                       -> def all_words(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
def add_episode(conn, name, new_words, replacements, tokens=0,             -> def add_episode(conn: sqlite3.Connection, name: str, new_words: int, replacements: int,
                 show=None, episode_no=None):                                              tokens: int = 0, show: str | None = None, episode_no: str | None = None) -> int | None:
def stamp_learned(conn, word_id, episode_name, threshold):                 -> def stamp_learned(conn: sqlite3.Connection, word_id: int, episode_name: str, threshold: int) -> None:
def touch_last_seen(conn, word_id, pos):                                   -> def touch_last_seen(conn: sqlite3.Connection, word_id: int, pos: int) -> None:
def upsert_word(conn, lemma, reading, romaji, gloss, pos, episode_name):   -> def upsert_word(conn: sqlite3.Connection, lemma: str, reading: str | None, romaji: str | None,
                                                                                                gloss: str | None, pos: str | None, episode_name: str) -> int | None:
def record_sighting(conn, word_id, episode_id, occurrences, replacements): -> def record_sighting(conn: sqlite3.Connection, word_id: int, episode_id: int, occurrences: int,
                                                                                                   replacements: int) -> None:
def record_variant(conn, word_id, en_word):                                -> def record_variant(conn: sqlite3.Connection, word_id: int, en_word: str) -> None:
def word_variants(conn, word_id):                                          -> def word_variants(conn: sqlite3.Connection, word_id: int) -> dict[str, int]:
def cache_get(conn, ja_line, en_text, lemma):                              -> def cache_get(conn: sqlite3.Connection, ja_line: str, en_text: str, lemma: str) -> sqlite3.Row | None:
def cache_put(conn, ja_line, en_text, lemma, en_word):                     -> def cache_put(conn: sqlite3.Connection, ja_line: str, en_text: str, lemma: str, en_word: str | None) -> None:
def set_status(conn, lemma, status):                                       -> def set_status(conn: sqlite3.Connection, lemma: str, status: str) -> int:
def set_note(conn, lemma, note):                                            -> def set_note(conn: sqlite3.Connection, lemma: str, note: str | None) -> int:
```

Non-obvious calls:
- `add_episode`/`upsert_word` return `int | None`, not bare `int`: both end in `return
  cur.lastrowid`, and `sqlite3.Cursor.lastrowid` is typed `int | None` in typeshed (not
  guaranteed non-None even though these are always post-INSERT in practice). Declaring the
  wider `int | None` is the safe direction if I'm mis-remembering the exact stub shape — a
  function returning concrete `int` still satisfies a declared `int | None` return.
- `set_status`/`set_note` return plain `int`: both end in `return cur.rowcount`, which typeshed
  types as non-optional `int` (defaults to `-1` when not applicable, but never `None`).
- `reading`/`romaji`/`gloss`/`pos` on `upsert_word` are all `str | None` — every one is a
  nullable `TEXT` column per `SCHEMA`.

### `bll/timeline.py` (18/18 defs — hard-gated)

Needs `from __future__ import annotations` **and** `from typing import Any` (new from-import,
sorted after `from datetime import datetime`, matching this file's existing straight-imports-
then-from-imports ordering). The registry (`branches`/`snaps` dict, loaded from
`registry.json`) is a genuinely dynamic, nested JSON blob with no schema enforced in code —
every registry-shaped value below is typed `dict[str, Any]` rather than a hand-invented
`TypedDict`, per the DEFINE doc's own guidance.

```python
def _now():                                        -> def _now() -> str:
def tdir(db):                                      -> def tdir(db: str) -> str:
def _snaps_dir(db):                                -> def _snaps_dir(db: str) -> str:
def _registry_path(db):                            -> def _registry_path(db: str) -> str:
def _episode_count(db):                            -> def _episode_count(db: str) -> int:
def _last_episode_name(db):                        -> def _last_episode_name(db: str) -> str | None:
def _load(db):                                      -> def _load(db: str) -> dict[str, Any]:
def _save(db, reg):                                 -> def _save(db: str, reg: dict[str, Any]) -> None:
def _take(db, reg, branch, position, episode,       -> def _take(db: str, reg: dict[str, Any], branch: str, position: int, episode: str | None,
           label=None):                                           label: str | None = None) -> dict[str, Any]:
def _import_backups(db, reg):                       -> def _import_backups(db: str, reg: dict[str, Any]) -> None:
def init(db):                                        -> def init(db: str) -> dict[str, Any]:
def record(db):                                      -> def record(db: str) -> dict[str, Any]:
def _snap_at(reg, branch, position):                 -> def _snap_at(reg: dict[str, Any], branch: str, position: int) -> dict[str, Any] | None:
def snapshot_path(db, position):                     -> def snapshot_path(db: str, position: int | str) -> str | None:
def _head_snap(reg, branch):                         -> def _head_snap(reg: dict[str, Any], branch: str) -> dict[str, Any] | None:
def state(db):                                       -> def state(db: str) -> dict[str, Any]:
def branch(db, position, name):                      -> def branch(db: str, position: int, name: str) -> str:
def switch(db, branch_id):                           -> def switch(db: str, branch_id: str) -> dict[str, Any]:
```

Non-obvious calls:
- `snapshot_path`'s `position: int | str` (not plain `int`, unlike `branch`/`_snap_at`): its
  body explicitly does `int(position)` before comparing — the only function in this module
  that defensively casts — implying its caller (the web UI, out of scope) may pass a raw
  string. `branch`/`_snap_at` receive already-int positions from internal call sites, so they
  stay plain `int`.
- One companion **local** annotation is needed inside `state()`'s body once its signature is
  annotated (mypy then checks the body — see Decision 3 for why this matters everywhere):
  `branches: list[dict[str, Any]] = []` before the `for bid, b in reg["branches"].items():`
  loop. Without it, mypy's inference for the bare `[]` literal is ambiguous once several
  differently-shaped dict literals get `.append()`-ed into it. Pure annotation, zero behavior
  change.

### `bll/gloss.py` (6/6 defs — hard-gated)

Needs `from __future__ import annotations` and `from typing import Any` (new from-import,
this file currently has no `from` imports, so it lands after the four straight imports). The
LLM prompt-entry / parsed-alignment structures are the same "loosely-structured JSON blob"
case the DEFINE doc calls out as a legitimate `Any` use — a raw JSON object of unknown shape,
sometimes user-supplied via `--gloss-json`.

```python
def build_prompt(entries, header=None):                              -> def build_prompt(entries: list[dict[str, Any]], header: str | None = None) -> str:
def _extract_json(text):                                              -> def _extract_json(text: str) -> Any:
def run_claude(prompt, model=None, timeout=600):                      -> def run_claude(prompt: str, model: str | None = None, timeout: int = 600) -> str:
def run_ollama(prompt, model, url="http://localhost:11434",          -> def run_ollama(prompt: str, model: str | None, url: str = "http://localhost:11434",
                timeout=120, think=False, stats=None, temperature=0):              timeout: int = 120, think: bool = False, stats: list[dict[str, Any]] | None = None,
                                                                                     temperature: float = 0) -> str:
def gloss_and_align(entries, model=None, backend="ollama",           -> def gloss_and_align(entries: list[dict[str, Any]], model: str | None = None, backend: str = "ollama",
                     ollama_url="http://localhost:11434", think=False):            ollama_url: str = "http://localhost:11434", think: bool = False) -> dict[str, dict[str, Any]]:
def parse_result(data):                                               -> def parse_result(data: Any) -> dict[str, dict[str, Any]]:
```

Non-obvious calls:
- `_extract_json` returns `Any`, not `dict[str, Any]`: it's the raw output of
  `json.JSONDecoder().raw_decode(...)`, which could in principle deserialize to any JSON type,
  not just an object — `Any` is the honest type, matching the DEFINE doc's own "fine to use
  `Any`" guidance for a dynamic JSON blob.
- `run_claude` needs no cast for `-> str`: `subprocess.run(..., text=True)` selects typeshed's
  `CompletedProcess[str]` overload (a literal `True` keyword resolves the overload cleanly), so
  `res.stdout` is already `str`.
- `run_ollama`'s `model: str | None` (not plain `str`, even though it's a required positional):
  `gloss_and_align`'s own `model` param defaults to `None` and passes straight through to
  `run_ollama` on the ollama path without a substitution step (that substitution — defaulting
  to `"bll-align"` — happens one layer up, in `cli.py`'s `cmd_process`), so `None` is a real,
  reachable value here.
- Two companion **local** annotations, needed once `gloss_and_align`'s signature is annotated
  (same body-checking mechanism as `timeline.state` above): `stats: list[dict[str, Any]] = []`
  and `out: dict[str, dict[str, Any]] = {}` at the top of the function, replacing the bare
  `stats = []` / `out = {}`. The `merged`/`part`/`info` variables in the same function's
  chunk-merge loop rely on mypy's well-supported "assign `None`, then conditionally reassign
  to a concrete type in a later branch" inference — expected to type-check cleanly once
  `parse_result`'s return type is concrete, but flagged as a spot worth a second look if mypy
  disagrees; fallback is an explicit `merged: dict[str, Any] | None = None`.

---

## 2. CLI entry-surface annotation plan

**`bll.cli` does NOT get `disallow_untyped_defs`.** Two independent reasons, either one
sufficient on its own:

1. **Definitional.** The override is all-or-nothing per module (confirmed against mypy's own
   docs). Only 9 of `cli.py`'s ~27 defs are in scope here (`main` + the 8 `cmd_*`); the
   DEFINE doc explicitly keeps the other ~18 (`injectable_counts`, `render_plan`,
   `apply_sense_first`, `resolve_notes`, `replace_in_event`, `parse_show_episode`,
   `default_note`, `load_notes`, `overlapping`, `plaintext`, `fmt_injection`, `layer_paths`,
   `_note_width`, `_wrap_note`, `_en_counterpart`, and `main`'s own subparser wiring is fine,
   but those helpers manipulate `pysubs2` `SSAEvent`/`SSAFile` objects and ad-hoc JMdict entry
   lists) out of scope. A module can't carry the override while 2/3 of its defs are
   unannotated by design.
2. **Even the annotated 9 aren't risk-free enough to additionally chase full-module rigor.**
   See Decision 3 below — annotating a function's signature at all switches mypy into fully
   checking its body, and `cmd_process` alone is ~620 dynamic lines. Reaching for the module
   gate on top of that would mean also hand-verifying the ~18 out-of-scope helpers to the same
   standard — explicitly the "do not let it block the gate" case the DEFINE doc warns against.

This matches the DEFINE doc's own framing of this exact outcome as "a perfectly acceptable,
and probably the safer, outcome here."

### Signatures

```python
def main(argv=None):                     -> def main(argv: list[str] | None = None) -> int:
def cmd_process(args):                   -> def cmd_process(args: argparse.Namespace) -> int:
def cmd_render(args):                    -> def cmd_render(args: argparse.Namespace) -> int:
def cmd_words(args):                     -> def cmd_words(args: argparse.Namespace) -> int:
def cmd_mark(args, status):              -> def cmd_mark(args: argparse.Namespace, status: str) -> int:
def cmd_note(args):                      -> def cmd_note(args: argparse.Namespace) -> int:
def cmd_serve(args):                     -> def cmd_serve(args: argparse.Namespace) -> int:
def cmd_bootstrap(args):                 -> def cmd_bootstrap(args: argparse.Namespace) -> int:
def cmd_stats(args):                     -> def cmd_stats(args: argparse.Namespace) -> int:
```

`argparse.Namespace` needs no new import (`argparse` is already imported). `main`'s `argv:
list[str] | None` is the only entry-surface signature using union syntax, but it still forces
`cli.py` to gain the same `from __future__ import annotations` + `from typing import Any` as
the other three files (the latter for one local fix below — insert both right after the
module docstring / among the stdlib imports, see the exact snippet in section 3's sibling
note below).

One **required** local fix, found by tracing `cmd_words`'s body (see Decision 3 for why this
is checked at all): it does `params = ()` then conditionally `params = (args.status,)`. mypy
infers a bare `()` literal as the precise *empty*-tuple type `tuple[()]`, which is genuinely
incompatible with the later 1-element `tuple[Any]` reassignment — unlike `[]`/`{}`, tuples have
no "wait and see" inference, so this one is a real, not hypothetical, error once the signature
makes the body checked. Fix: `params: tuple[Any, ...] = ()`. Pure annotation, zero behavior
change; this is the one spot in the whole plan I'd bet on being a real mypy error if skipped.

I did not find another instance of this specific pattern (bare `()` reassigned to a
differently-shaped tuple) anywhere else across the 9 functions, including the 620-line
`cmd_process` — that function uses `[]`/`{}`/`None` for all its ambiguous containers, which
mypy's inference does accommodate.

---

## 3. `pyproject.toml` exact diff plan

```toml
[dependency-groups]
dev = [
    "pytest>=8.0",
    "ruff>=0.8",
    "mypy>=1.13",
]

[tool.mypy]
ignore_missing_imports = true

[[tool.mypy.overrides]]
module = ["bll.db", "bll.timeline", "bll.gloss"]
disallow_untyped_defs = true
```

**Version floor — `mypy>=1.13`:** current PyPI release is 2.1.0 (web search, since this is a
time-sensitive fact). Deliberately not floored at the bleeding edge, matching the existing
`pytest>=8.0`/`ruff>=0.8` style of "a known-good minimum, not today's exact release" — 1.13 is
a stable, long-available version where this file's entire config surface
(`ignore_missing_imports`, per-module `disallow_untyped_defs`) has existed unchanged for
years, so it's a safe floor without forcing the newest major (2.x) as a hard requirement for
this first adoption. `uv sync` will still resolve to whatever's newest either way (no upper
bound), so this floor is documentation of the minimum, not a pin.

**No `python_version` setting — deliberate, not an oversight.** Confirmed via mypy's own docs
that `python_version` (a) defaults to the interpreter actually running mypy (3.13 in this
repo's CI) if unset, and (b) can **only** be set globally, never per-module. Setting it to
`"3.9"` (the packaging floor) would make mypy *itself* verify 3.9-runtime-safety of `X | None`
syntax — tempting, since I'm relying on that syntax throughout — but it would apply to every
file mypy walks, including `jp.py` (out of scope, "already 2/10 annotated" per the DEFINE
doc's own facts) and `jmdict.py`/`bootstrap.py`/`web.py`, any of which might already contain
3.10+-only syntax I haven't audited. Leaving it unset keeps the blast radius to exactly the
files this issue touches.

**This connects to a real precedent, not a new tension I invented:** issue #14's own DESIGN
doc (`DESIGN_ISSUE_14_RUFF_CONFIG_GATE.md`, read for convention/precedent) flagged the exact
same `requires-python >=3.9` vs. modern-syntax gap as its own AC5 ("build report must flag any
UP-rule fix whose syntax needs newer-than-3.9 at runtime") and resolved it via **disclosure**
(flag it in the PR body), not a code fix — because that issue's scope was ruff's mechanical
`--fix` sweeping the *existing* codebase. This issue's scope is different: every new signature
here is hand-authored, so the stronger, zero-cost fix (`from __future__ import annotations`
per touched file, confirmed via mypy's own docs as exactly what this future-import exists for)
is available and strictly better than disclosure alone — it makes the "runtime floor honored"
claim actually true instead of "true modulo a flagged caveat." Doesn't contradict #14's
approach, just has more room to do better given a hand-written diff instead of an autofix.

### Import-block additions (exact placement, ruff-isort-safe)

`db.py` (docstring stays first; no new `Any` needed anywhere in this file):
```python
"""SQLite word database."""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime
```

`timeline.py`:
```python
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime
from typing import Any
```

`gloss.py`:
```python
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any
```

`cli.py` (preserves the existing stdlib / third-party / first-party group split):
```python
"""bll command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

import pysubs2

from . import db as dbm
from . import gloss as glossm
from . import jmdict, jp
```

---

## 4. CI step exact YAML

Insert after "Check formatting with ruff", before "Run tests":

```yaml
      - name: Type check with mypy
        run: uv run mypy bll/
```

Full resulting step order: checkout → setup-python 3.13 → setup-uv → `uv sync --locked --dev`
→ `ruff check .` → `ruff format --check .` → **`mypy bll/`** → `pytest tests/`.

---

## 5. Risk assessment

| Risk | Why it's real | Mitigation |
|---|---|---|
| `X \| None` syntax needs Python 3.10+ at runtime; `requires-python` stays `>=3.9` | Confirmed via mypy docs: PEP 604 union syntax raises `TypeError` at import time on 3.9 unless deferred | `from __future__ import annotations` in all 4 touched files (Decision 1) |
| Annotating a signature turns on full body-checking for that function, independent of `disallow_untyped_defs` | Confirmed via mypy docs ("functions without annotations are not checked... add annotations to enable type checking") — this applies per-function, not per-module-override | Traced every one of the 9 CLI entry-surface bodies by hand; found exactly one real issue (below) and none in `cmd_process` despite its size, because nearly everything it touches resolves to `Any` (see next row). Sanctioned fallback if the build phase finds something this read-only pass missed: `@typing.no_type_check` on that one function — confirmed via mypy docs as a real, documented way to keep the signature as readable documentation while explicitly opting out of body verification, preferable to stripping the annotation back to bare |
| `cmd_words`'s `params = ()` then `params = (args.status,)` | Bare `()` infers as the precise empty-tuple type `tuple[()]`; reassigning to a 1-element `tuple[Any]` is a genuine incompatible-type error, unlike the "wait and see" inference mypy gives empty `[]`/`{}` | `params: tuple[Any, ...] = ()` (local annotation, zero behavior change) |
| Third-party libs without stubs (`fugashi`, `pysubs2`, `wordfreq`, `pykakasi`, `simplemma`, and untyped-until-verified `uvicorn`/`fastapi` in `cmd_serve`'s optional import) | Would otherwise fail on missing stubs | Single global `ignore_missing_imports = true` — no per-library special-casing, no `# type: ignore[import]` anywhere |
| Calls from the newly-annotated CLI entry surface into `jp.py`/`jmdict.py`/`bootstrap.py` (deliberately unannotated, out of scope) and into `cli.py`'s own untouched helpers | Could look risky since those modules/functions carry no signatures at all | This is actually the main reason the CLI surface is *safe* to annotate: calling an unannotated function from checked code is itself unchecked and its result is treated permissively (confirmed via mypy docs) — nearly all of `cmd_process`'s dynamic logic funnels through one of these two escape hatches or through `argparse.Namespace`'s `__getattr__ -> Any` |
| `sqlite3.Row`/`Cursor` stub subtleties (`lastrowid` nullability, `rowcount`, generic row typing on dict comprehensions) | Genuinely easy to get backwards from memory | Chose the *wider*, more permissive type wherever uncertain (`int \| None` for `lastrowid`-derived returns) — a narrower actual stub type still satisfies a wider declared one, so this is safe in either direction |
| `argparse.Namespace` attribute access (`args.foo`) | Would be a hard error against a stricter stub | Typeshed explicitly stubs `Namespace.__getattr__(self, name: str) -> Any` for exactly this dynamic-attribute pattern — confirmed consistent with how `set_defaults`/parsed args are used throughout `cli.py` |

**Net result of this plan: zero `# type: ignore` comments anywhere**, by construction —
every genuinely dynamic boundary gets `Any` on purpose rather than a suppressed error, and the
one real edge case found (`cmd_words`) gets a real local annotation, not a suppression.

**Time-box, at the per-function granularity, not just the whole-CI-step granularity the
DEFINE doc's fallback describes:** if the build phase hits mypy friction inside `cmd_process`
specifically (the one function too large to fully hand-verify here with certainty) that isn't
resolved quickly, apply `@typing.no_type_check` to just that function and move on, rather than
spending the DEFINE doc's ~30-minute budget chasing errors through 620 dynamic lines. Log
which function (if any) needed it, in the PR body, same as the whole-step fallback would be
logged.

---

## 6. Commit plan

Confirms the DEFINE doc's suggested 3-commit shape, with exact file lists:

| Commit | Contents | Notes |
|---|---|---|
| 1. Config + lock | `pyproject.toml` (dev-dep + `[tool.mypy]` block), `uv.lock` | `uv run mypy bll/` will **not** pass yet at this commit in isolation (the override requires annotations that don't exist until commit 2) — expected and harmless, since the CI gate itself doesn't exist until commit 3, and `pytest`/`ruff` (the only gates live at this point) are untouched |
| 2. Annotations | `bll/db.py`, `bll/timeline.py`, `bll/gloss.py`, `bll/cli.py` | All four files in one commit: the three hard-gated modules plus `cli.py`'s entry surface + its one local fix. First commit where `uv run mypy bll/` is meaningfully checkable end-to-end — run it locally here, plus re-run `ruff check .` / `ruff format --check .` / `pytest tests/` to confirm zero behavior change before pushing |
| 3. CI step | `.github/workflows/tests.yml` | Turns mypy into an enforced PR gate; should be a no-op on CI since commit 2 already verified it passes locally |

Optional finer split if commit 2's diff feels large for one review pass: 2a = `db.py` +
`timeline.py` + `gloss.py` (the hard-gated trio — mutually independent per the DEFINE doc's
own facts, so genuinely atomic together), 2b = `cli.py` alone (keeps "the gated modules" and
"the best-effort CLI surface" separately legible in history). Not required; the 3-commit
shape above is the recommended default.

---

## Out of scope (carried from DEFINE, not re-litigated)

Full strict-mode mypy repo-wide; annotating `bootstrap.py`/`jmdict.py`/`jp.py`/`web.py` or the
rest of `cli.py` beyond its entry surface; any behavior change (a type-check surfacing a real
bug is logged as a follow-up issue draft, never fixed here); editing `requires-python`; any
git/gh operations (this phase is read-only planning; build executes it).

## Next step

**Ready for:** `/build .claude/sdd/features/DESIGN_ISSUE_15_TYPE_HINTS.md`
