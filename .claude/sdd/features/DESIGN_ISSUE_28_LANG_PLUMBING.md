# DESIGN: Issue #28 — zh epic slice A: lang plumbing through `words` call-sites

> Technical design for plumbing an explicit `lang` parameter (default `"ja"`) through the
> `words` call-sites in `bll/db.py`, `bll/web.py`, and `bll/cli.py` — pure seam work, byte-invariant
> for existing JA data. Slice A of epic #16 (zh-TW support).

## Metadata

| Attribute | Value |
|-----------|-------|
| **Feature** | ISSUE_28_LANG_PLUMBING |
| **Date** | 2026-07-06 |
| **Author** | design phase (agentspec:workflow:design), driven headlessly by the `implement` skill |
| **DEFINE** | [DEFINE_ISSUE_28_LANG_PLUMBING.md](../_synthesized/DEFINE_ISSUE_28_LANG_PLUMBING.md) |
| **Issue** | Future-Gadgets-AI/bilingual-language-learning#28 |
| **Status** | Ready for Build |

---

## Invariant — hard rail (non-negotiable, restated from the issue and DEFINE doc)

**JA behavior is byte-invariant.** Every `lang` parameter added by this design — in
`db.py`, `web.py`, and `cli.py` — defaults to `"ja"`. With no `--lang` flag and no `lang`
query/body param supplied, every existing code path must produce output identical to `main`,
because every existing row and every existing caller is implicitly `lang="ja"` today. Two
concrete tripwires this design is built to pass:
- `tests/test_process_e2e_smoke.py` run with no `--lang` flag must produce byte-identical
  output to a pre-change run (the direct AT5 oracle — not this design's or build's job to run,
  but every decision below is made so that run cannot change).
- Every pre-existing call in `tests/test_db.py` (unmodified by this issue) keeps its current
  argument count and must keep passing without edits — see Decision 1.

---

## Architecture Overview

```text
┌──────────────────────────────────────────────────────────────────────┐
│           lang PLUMBING SEAM  (issue #28 — epic #16, slice A)        │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│   CLI (bll/cli.py)                  Web API (bll/web.py)             │
│   ─────────────────                 ──────────────────               │
│   --lang, default "ja"              lang: str = "ja"                 │
│     │  runtime check in               on GET /api/words,             │
│     │  cmd_process:                   GET /api/word/{lemma}/history  │
│     │  non-"ja" -> print msg          body["lang"] on                │
│     │  pointing at #16, exit 1        POST /api/word/{lemma}         │
│     ▼  (before any DB write)                │                       │
│   cmd_process(args.lang="ja")                ▼                       │
│     │                              update_word / word_history / words │
│     └──────────────────┬──────────────────────┘                      │
│                         ▼                                            │
│              bll/db.py: all_words / upsert_word /                    │
│                         set_status / set_note                        │
│              each gains  lang: str = "ja"  (trailing, kwarg-default) │
│                         │                                             │
│                         ▼                                            │
│              SQL:  ... WHERE lemma=? AND lang=?                      │
│                         │                                             │
│                         ▼                                            │
│              words table — lang column + UNIQUE(lemma, lang)         │
│              (schema/migration already landed in PR #27)             │
│                                                                       │
└──────────────────────────────────────────────────────────────────────┘
```

Nothing upstream of this seam (JMdict alignment, subtitle parsing, the forgetting-clock model)
changes — this is purely a threading-through of one already-existing column into the four
read/write functions and their three call surfaces.

---

## Components

| Component | Purpose | Technology |
|-----------|---------|------------|
| `bll/db.py` — `all_words`, `upsert_word`, `set_status`, `set_note` | lang-scoped read/write of the `words` table | sqlite3 (stdlib) |
| `bll/web.py` — `words`, `update_word`, `word_history` | expose `lang` as an optional, `"ja"`-default API param | FastAPI |
| `bll/cli.py` — `process` subparser + `cmd_process` | `--lang` flag; syntactic accept / runtime reject of non-`"ja"` | argparse |
| `tests/test_db.py` (appended) | regression (unmodified existing tests) + 3 new coexistence/isolation tests | pytest |

---

## Key Decisions

### Decision 1: `lang` is a trailing, keyword-defaultable parameter on all four db.py functions

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** `tests/test_db.py` calls all four functions positionally today (e.g.
`db.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun", "e01.ja.srt")`,
`db.set_status(conn, lemma, status)`). The hard rail requires every existing code path —
tests included — to keep working unchanged.

**Choice:** Append `lang: str = "ja"` as the *last* parameter of each of the four signatures.
No existing parameter is reordered, renamed, or given a new default.

**Rationale:** Python resolves positional arguments left-to-right; since no existing caller
supplies more positional arguments than the pre-change signature had, every existing call
keeps binding exactly as before and `lang` silently takes its default. This is the only
placement that satisfies byte-invariance without touching a single existing call-site.

**Alternatives Rejected:**
1. Inserting `lang` earlier in the signature (e.g. right after `lemma`, mirroring the
   `UNIQUE(lemma, lang)` column order) — rejected: would shift every subsequent positional
   argument in every existing call, breaking `tests/test_db.py` and the cli.py/web.py
   call-sites that call positionally.
2. A `lang: str | None = None` sentinel meaning "use caller's default elsewhere" — rejected:
   adds an indirection layer for no benefit when a plain `"ja"` default already satisfies
   every requirement; `None` would also need its own translation step before hitting SQL.

**Consequences:**
- New callers that *do* want zh-TW behavior must pass `lang=` as a keyword (positionally it
  would work too, but keyword is clearer and is what this design's new tests use).
- Column order in the `CREATE TABLE` (`lemma, lang, ...`) and parameter order in Python
  signatures now intentionally differ (`lang` last) — acceptable, and worth a one-line
  comment in code since it's the kind of mismatch a future reader might "fix" by mistake.

---

### Decision 2: web.py — query param for GETs, body field for the POST

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** The DoR-audit comment logged this shape as design-child latitude: *"web.py lang
parameter shape (query param vs body field per endpoint) is design-child latitude; default
'ja' everywhere and JA-invariance are NOT negotiable."* `bll/web.py` has no Pydantic models
anywhere — GET params are plain typed function arguments FastAPI auto-binds as query params
(see `words(status: str = "all", kana_threshold=..., ...)`); the POST endpoint already takes
a bare `body: dict` read via `body["status"]` / `body["note"]`.

**Choice:**
- `GET /api/words` -> add `lang: str = "ja"` as another plain typed parameter next to
  `status`, exactly like the existing ones (FastAPI binds it as `?lang=zh-TW`).
- `GET /api/word/{lemma}/history` -> `def word_history(lemma: str, lang: str = "ja")` — same
  pattern.
- `POST /api/word/{lemma}` -> read `lang = body.get("lang", "ja")` from the existing `dict`
  body, alongside `body["status"]` / `body["note"]`. No Pydantic model introduced.

**Rationale:** Matches each endpoint's *existing* parameter-passing convention exactly — GET
endpoints have no body, so a query param is the only idiomatic option; the POST endpoint
already receives a body dict for the mutation payload, and `lang` is part of *which row* that
payload applies to, so it travels with the payload rather than introducing a second,
inconsistent convention (e.g. a query param bolted onto a POST that already has a body) on
the same endpoint.

**Alternatives Rejected:**
1. A `lang` path segment (`/api/word/{lemma}/{lang}`) — rejected: changes the URL shape of an
   existing, presumably-already-linked-from-the-frontend route; a query/body param is
   additive and non-breaking.
2. Introducing a Pydantic `BaseModel` for the POST body — rejected: `web.py` has zero Pydantic
   usage today; introducing one model for one new optional field is a bigger, unrelated shift
   in this file's conventions than this issue's scope calls for.

**Consequences:**
- All three endpoints are additive-only from an API-contract standpoint: omitting `lang`
  behaves exactly as today (only `"ja"` rows exist, so this is currently unobservable, but
  the code path is real and exercised by the new default value flowing into db.py).
- No frontend changes are required by this issue (none are in scope — the issue's checklist
  doesn't name any UI files).

---

### Decision 3: `--lang`'s non-`"ja"` rejection is a runtime check inside `cmd_process`, not `choices=`

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** `bll/cli.py` already uses argparse's `choices=[...]` elsewhere (`--backend`,
`choices=["claude", "ollama"]`). Using `choices=["ja"]` for `--lang` would make argparse itself
reject `--lang zh-TW` with a generic usage error before `cmd_process` ever runs.

**Choice:** `--lang` takes a plain `default="ja"` string argument (no `choices=`). The
rejection of non-`"ja"` values happens as the *first* statement inside `cmd_process`, printing
a message that names issue #16 and returns a nonzero exit code, before `dbm.backup()` or any
other side effect.

**Rationale:** The issue explicitly requires zh-TW to be *accepted syntactically* (so the flag
genuinely exists and takes a value — this is a seam for future slices, not a fence) and
rejected with a *friendly, issue-pointing message* — not argparse's generic
`invalid choice: 'zh-TW' (choose from 'ja')` usage error, which would look like the flag
doesn't support the value at all rather than "not implemented yet, see #16."

**Alternatives Rejected:**
1. `choices=["ja"]` — rejected: wrong error shape (argparse usage error, not issue-pointing
   message) and technically wrong (zh-TW must be a *valid* value the parser accepts, per the
   issue's own framing of this as a seam for later slices).
2. A custom argparse `type=` callable that raises `argparse.ArgumentTypeError` for non-`"ja"`
   — rejected: this still produces an argparse-flavored usage error at parse time, and (more
   importantly) it would reject the value *before* `dbm.backup()` would even be relevant to
   discuss, but it couples validation to argument parsing in a way that makes the "point at
   #16" message harder to phrase naturally and mixes a business rule into the parser
   definition. A plain runtime `if` at the top of `cmd_process` is simpler and keeps the rule
   next to the code it protects.

**Consequences:**
- `args.lang` is always a plain string by the time any other line of `cmd_process` runs after
  the check; everything below the check can assume `args.lang == "ja"` today (a later zh-TW
  slice replaces the rejection with real branching, not this issue's job).
- `bll process foo.ja.srt foo.en.srt --lang ja` (explicit) and omitting `--lang` entirely are
  indistinguishable — both take the `"ja"` path. This is required by byte-invariance.

---

### Decision 4: `upsert_word`'s INSERT explicitly lists `lang` instead of relying on the schema `DEFAULT`

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** Today's INSERT (`INSERT INTO words (lemma, reading, romaji, gloss, pos,
first_seen, added_at) VALUES (...)`) omits `lang` and relies on the column's
`DEFAULT 'ja'`. Once `upsert_word` accepts a real `lang` argument, that reliance would silently
insert every row as `"ja"` regardless of what the caller asked for.

**Choice:** Add `lang` to the INSERT's column list and bind `lang` (the parameter) as its
value.

**Rationale:** The whole point of this issue is that `lang` becomes a real, respected input;
falling back to the schema default would make the new parameter a no-op for inserts. When the
caller passes `lang="ja"` (explicitly or via the default), the inserted value is `"ja"` — byte-
identical to what the schema default already produced, so this is safe under the hard rail.

**Alternatives Rejected:**
1. Keep relying on the schema default and add a separate `UPDATE words SET lang=? WHERE id=?`
   right after insert for non-`"ja"` callers — rejected: an unnecessary second statement (and
   a moment where the row briefly exists with the wrong `lang`) when the INSERT can just
   carry the right value from the start.

**Consequences:** None beyond the fix itself — this only changes behavior for `lang != "ja"`
callers, which don't exist yet in this codebase (zh-TW ingestion is a later slice).

---

### Decision 5: `cmd_mark`, `cmd_note`, and `cmd_words` are NOT touched by this issue

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** `cmd_mark` calls `dbm.set_status(conn, lemma, status)` and `cmd_note` calls
`dbm.set_note(conn, args.lemma, ...)` with no `lang` argument — both now implicitly resolve to
`lang="ja"` via the new default. `cmd_words` builds its own raw `SELECT * FROM words` SQL,
bypassing `all_words()` entirely, and the issue's checklist does not name it.

**Choice:** Leave `cmd_mark`, `cmd_note`, and `cmd_words` exactly as they are. Only the
`process` subcommand gains a `--lang` flag, exactly as the issue's Context section specifies
("`bll/cli.py`: `cmd_process` gains `--lang`").

**Rationale:** The issue is explicit and scoped: it names `cmd_process` as the only cli.py
command gaining a flag. Adding `--lang` to `mark`/`note`/`words` as well would be scope creep
beyond what was asked — a real but separate future need (once zh-TW words actually exist,
marking/noting by lemma alone becomes ambiguous across languages), analogous in spirit to the
`episodes`/`align_cache` items `docs/DB_AUDIT.md` already tags **WATCH — blocked on #16**
rather than fixing now.

**Alternatives Rejected:**
1. Add `--lang` to all four cli.py subcommands proactively "while we're in here" — rejected:
   not requested by the issue, expands the diff and the test surface beyond the acceptance
   criteria, and duplicates a decision that plausibly belongs to a later, better-informed
   slice of epic #16 (same reasoning `docs/DB_AUDIT.md` already applied to `episodes` and
   `align_cache`).

**Consequences:** `bll mark`/`bll note` continue to operate on whichever row has
`lang="ja"` for a given lemma, even after zh-TW rows exist. This is a known, explicitly-logged
residual gap for a later slice, not a defect of this one.

---

## File Manifest

| # | File | Action | Purpose | Agent | Dependencies |
|---|------|--------|---------|-------|--------------|
| 1 | `bll/db.py` | Modify | Add `lang: str = "ja"` + SQL scoping to `all_words`, `upsert_word`, `set_status`, `set_note` | build (direct) | None |
| 2 | `bll/web.py` | Modify | Add `lang` query param (`words`, `word_history`) / body field (`update_word`) + SQL scoping | build (direct) | 1 |
| 3 | `bll/cli.py` | Modify | Add `--lang` to the `process` subparser; runtime rejection + threading in `cmd_process` | build (direct) | 1 |
| 4 | `tests/test_db.py` | Modify (append) | 3 new tests: coexistence/no-collapse, `set_status` isolation, `set_note` isolation | build (direct) | 1 |

**Total Files:** 4 (all modifications to existing files — no new files)

---

## Agent Assignment Rationale

This is a small, tightly-coupled, single-package change (one new parameter, threaded through
4 functions in 1 file, 3 endpoints in 1 file, and 1 CLI flag in 1 file, plus 3 tests in an
existing test file). Splitting it across multiple specialist sub-agents (e.g.
`agentspec:python:python-developer` for the `.py` edits, `agentspec:test:test-generator` for
the tests) would fragment a single coherent seam into pieces that all depend on the exact same
signature decision (Decision 1) — the risk of two agents drifting on the signature shape
outweighs any parallelism benefit for a change this size.

| Agent | Files Assigned | Why This Agent |
|-------|----------------|----------------|
| (general — build phase, direct) | 1, 2, 3, 4 | Single coherent seam change; one signature decision (Decision 1) must be applied identically everywhere it's threaded — no specialist split warranted at this size. |

**Agent Discovery:** specialists exist in this environment (`agentspec:python:python-developer`,
`agentspec:test:test-generator`, `agentspec:python:code-reviewer`) but were not assigned, per
the rationale above.

---

## Code Patterns

### Pattern 1: db.py — lang-scoped signature + SQL (representative: `set_status`)

```python
# Before:
def set_status(conn: sqlite3.Connection, lemma: str, status: str) -> int:
    cur = conn.execute("UPDATE words SET status=? WHERE lemma=?", (status, lemma))
    return cur.rowcount

# After:
def set_status(conn: sqlite3.Connection, lemma: str, status: str, lang: str = "ja") -> int:
    cur = conn.execute(
        "UPDATE words SET status=? WHERE lemma=? AND lang=?", (status, lemma, lang)
    )
    return cur.rowcount
```

`set_note` follows the identical pattern (`SET note=? WHERE lemma=? AND lang=?`).

`all_words`:

```python
def all_words(conn: sqlite3.Connection, lang: str = "ja") -> dict[str, sqlite3.Row]:
    """lemma -> row for every word in the DB, scoped to one language."""
    return {r["lemma"]: r for r in conn.execute("SELECT * FROM words WHERE lang=?", (lang,))}
```

`upsert_word` (lookup gains the `lang` predicate; INSERT gains the `lang` column — see
Decision 4):

```python
def upsert_word(
    conn: sqlite3.Connection,
    lemma: str,
    reading: str | None,
    romaji: str | None,
    gloss: str | None,
    pos: str | None,
    episode_name: str,
    lang: str = "ja",
) -> int | None:
    row = conn.execute(
        "SELECT id FROM words WHERE lemma=? AND lang=?", (lemma, lang)
    ).fetchone()
    if row:
        if gloss:
            conn.execute("UPDATE words SET gloss=? WHERE id=?", (gloss, row["id"]))
        return row["id"]
    cur = conn.execute(
        "INSERT INTO words (lemma, lang, reading, romaji, gloss, pos, first_seen, added_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            lemma,
            lang,
            reading,
            romaji,
            gloss,
            pos,
            episode_name,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    return cur.lastrowid
```

### Pattern 2: web.py — query param (GET) and body field (POST)

```python
# GET /api/words -- lang joins the existing plain-typed-param list
@app.get("/api/words")
def words(
    status: str = "all",
    lang: str = "ja",
    kana_threshold: int = DEFAULTS["kana_threshold"],
    note_threshold: int = DEFAULTS["note_threshold"],
    learning_threshold: int = DEFAULTS["learning_threshold"],
    at: int = None,
):
    c = read_conn(at)
    clk = dbm.clock(c)
    q = "SELECT * FROM words WHERE lang=?"
    params: tuple = (lang,)
    if status != "all":
        q += " AND status=?"
        params = (lang, status)
    q += " ORDER BY status, exposures DESC, lemma"
    for r in c.execute(q, params):
        ...  # unchanged below this line

# GET /api/word/{lemma}/history
@app.get("/api/word/{lemma}/history")
def word_history(lemma: str, lang: str = "ja"):
    c = conn()
    w = c.execute("SELECT * FROM words WHERE lemma=? AND lang=?", (lemma, lang)).fetchone()
    if not w:
        raise HTTPException(404, f"{lemma} not in database")
    ...  # unchanged below this line

# POST /api/word/{lemma} -- lang read from the existing bare dict body
@app.post("/api/word/{lemma}")
def update_word(lemma: str, body: dict):
    c = conn()
    lang = body.get("lang", "ja")
    if not c.execute(
        "SELECT 1 FROM words WHERE lemma=? AND lang=?", (lemma, lang)
    ).fetchone():
        raise HTTPException(404, f"{lemma} not in database")
    if "status" in body:
        dbm.set_status(c, lemma, body["status"], lang=lang)
    if "note" in body:
        dbm.set_note(c, lemma, body["note"] or None, lang=lang)
    c.commit()
    return {"ok": True}
```

### Pattern 3: cli.py — `--lang` flag + runtime rejection

```python
# In the `process` subparser (pp), grouped with the other per-episode metadata
# flags (--show / --episode), after --episode:
pp.add_argument(
    "--lang",
    default="ja",
    help="language track to process (default ja). Other values are accepted "
    "here but not yet supported end-to-end -- see issue #16.",
)

# First statement inside cmd_process -- before dbm.backup() or any DB write:
def cmd_process(args: argparse.Namespace) -> int:
    if args.lang != "ja":
        print(
            f"--lang {args.lang!r} is not supported yet -- zh-TW ingestion "
            "lands in a later slice of issue #16 (this change only plumbs "
            "the seam). Use --lang ja, or omit --lang, for now.",
            file=sys.stderr,
        )
        return 1
    if args.backend == "ollama" and not args.model:
        ...  # unchanged below this line

    # further down, the two call-sites thread args.lang through:
    db_words = dbm.all_words(conn, lang=args.lang)
    ...
    wid = dbm.upsert_word(
        conn, lemma, w["reading"], w["romaji"], info.get("gloss"), w["pos"], episode,
        lang=args.lang,
    )
```

---

## Data Flow

```text
1. CLI:  `bll process a.ja.srt a.en.srt [--lang ja]`
   │
   ▼
2. cmd_process: args.lang != "ja"? -> print #16 pointer to stderr, return 1 (stop, no writes)
   │  (args.lang == "ja" -- the only path today; the only path pre-existing tests exercise)
   ▼
3. dbm.all_words(conn, lang=args.lang) / dbm.upsert_word(..., lang=args.lang)
   │
   ▼
4. SQL:  SELECT/UPDATE/INSERT ... WHERE lang=?  (or explicit lang column on INSERT)
   │
   ▼
5. words table -- lang column (PR #27) now actually load-bearing, not just present

   (Web API path is the same shape, entering at step 3 via update_word / word_history / words
   instead of cmd_process, with `lang` sourced from a query param or body field per Decision 2.)
```

---

## Integration Points

None new. `bll/web.py` continues to talk only to `bll/db.py` (same process, same SQLite file);
`bll/cli.py` continues to talk only to `bll/db.py`. No external system, network call, or
third-party API is touched by this issue.

---

## Testing Strategy

| Test Type | Scope | Files | Tools | Coverage Goal |
|-----------|-------|-------|-------|----------------|
| Unit (new) | db.py lang scoping | `tests/test_db.py` (3 new tests, appended) | pytest | AT1, AT2 |
| Regression (unmodified) | every existing test keeps passing with no edits | `tests/*.py` (floor of 32) | pytest | AT5 (byte-invariance signal) |
| E2E smoke (byte-invariance oracle) | full `process` pipeline, no `--lang` flag | `tests/test_process_e2e_smoke.py` (run as-is, not modified by this issue) | pytest + offline JMdict fixture | AT5 |
| Structural, optional | `--lang` rejection message/exit code; web.py default param wiring | none required by the issue; light coverage is build's latitude, not a gate | pytest / manual curl | AT3, AT4 |

### New unit tests (exact — append to `tests/test_db.py`, using the existing `conn` fixture)

**`test_upsert_word_same_lemma_different_lang_coexist`**
```python
def test_upsert_word_same_lemma_different_lang_coexist(conn):
    wid_ja = db.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun", "e01.ja.srt")
    wid_zh = db.upsert_word(conn, "猫", None, None, "cat (zh)", "noun", "e01.zh.srt", lang="zh-TW")
    assert wid_ja != wid_zh  # two distinct rows -- same lemma did not upsert-over across langs

    default_scope = db.all_words(conn)              # no lang arg -> defaults to "ja"
    ja_scope = db.all_words(conn, lang="ja")
    zh_scope = db.all_words(conn, lang="zh-TW")

    assert set(default_scope) == {"猫"} == set(ja_scope)   # default == explicit "ja" (byte-invariance)
    assert default_scope["猫"]["id"] == ja_scope["猫"]["id"] == wid_ja
    assert set(zh_scope) == {"猫"}
    assert zh_scope["猫"]["id"] == wid_zh
    assert ja_scope["猫"]["gloss"] == "cat"
    assert zh_scope["猫"]["gloss"] == "cat (zh)"          # neither scope leaks into the other
```

**`test_set_status_scoped_by_lang_leaves_other_lang_row_untouched`**
```python
def test_set_status_scoped_by_lang_leaves_other_lang_row_untouched(conn):
    db.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun", "e01.ja.srt")
    db.upsert_word(conn, "猫", None, None, "cat (zh)", "noun", "e01.zh.srt", lang="zh-TW")

    rowcount = db.set_status(conn, "猫", "known", lang="ja")

    assert rowcount == 1
    assert db.all_words(conn, lang="ja")["猫"]["status"] == "known"
    assert db.all_words(conn, lang="zh-TW")["猫"]["status"] == "learning"  # untouched (schema default)
```

**`test_set_note_scoped_by_lang_leaves_other_lang_row_untouched`**
```python
def test_set_note_scoped_by_lang_leaves_other_lang_row_untouched(conn):
    db.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun", "e01.ja.srt")
    db.upsert_word(conn, "猫", None, None, "cat (zh)", "noun", "e01.zh.srt", lang="zh-TW")

    rowcount = db.set_note(conn, "猫", "override note", lang="ja")

    assert rowcount == 1
    assert db.all_words(conn, lang="ja")["猫"]["note"] == "override note"
    assert db.all_words(conn, lang="zh-TW")["猫"]["note"] is None  # untouched
```

No existing test in `tests/test_db.py` is modified — every one of them calls these four
functions with the same argument count as today, so they exercise (and pin) the `lang="ja"`
default path unchanged. That unmodified suite is itself the primary byte-invariance regression
check for `db.py`.

---

## Error Handling

| Error Type | Handling Strategy | Retry? |
|------------|-------------------|--------|
| `--lang` value other than `"ja"` | `cmd_process` prints a message naming issue #16 to stderr, returns exit code 1, before `dbm.backup()` or any DB write | No |
| Lemma exists but not in the requested `lang` (web.py lookups) | Identical to "lemma not found at all" — existing `404 HTTPException` path, since `(lemma, lang)` is just a more specific not-found condition; no new error class | No |
| Lemma exists but not in the requested `lang` (db.py `set_status`/`set_note`) | Identical to "lemma not found" today — `rowcount == 0`, already the documented signal (see `set_note`'s existing docstring: "Returns rowcount (0 = lemma not in DB)") | No |

---

## Configuration

| Config Key | Type | Default | Description |
|------------|------|---------|--------------|
| `lang` (db.py: `all_words`, `upsert_word`, `set_status`, `set_note`) | `str` | `"ja"` | Language tag scoping the query/mutation. Trailing, keyword-defaultable (Decision 1). |
| `lang` (web.py: `words` query, `word_history` query) | `str` | `"ja"` | FastAPI query param, e.g. `?lang=zh-TW`. |
| `lang` (web.py: `update_word` body field) | `str` | `"ja"` | Read via `body.get("lang", "ja")` from the existing bare-`dict` POST body. |
| `--lang` (cli.py `process` subcommand) | `str` | `"ja"` | Accepted syntactically for any value; `cmd_process` rejects non-`"ja"` at runtime, pointing at #16. |

---

## Security Considerations

- No new trust boundary: `lang` is bound the same way `status`/`note`/`lemma` already are
  (FastAPI query/body coercion, argparse string argument) and flows only into parameterized
  `?` SQL placeholders — no string interpolation is introduced anywhere in this design, so no
  new SQL-injection surface is opened.
- The CLI rejection path (Decision 3) fails closed: a rejected `--lang` value returns before
  `dbm.backup()` or `dbm.connect()`'s write path, so a bad flag value cannot leave a half-
  written backup or DB state.

---

## Observability

This is a local CLI + single-process FastAPI tool with no existing logging/metrics/tracing
infrastructure (`print()` is the only current output channel) — this issue does not add any.
The one new observable signal is the `--lang` rejection message itself (stderr line + exit
code 1), which is sufficient for a human operator to understand why nothing happened.

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-07-06 | design phase (agentspec:workflow:design) | Initial version, from issue #28 + DEFINE_ISSUE_28_LANG_PLUMBING.md |

---

## Next Step

**Ready for:** `/build .claude/sdd/features/DESIGN_ISSUE_28_LANG_PLUMBING.md`
