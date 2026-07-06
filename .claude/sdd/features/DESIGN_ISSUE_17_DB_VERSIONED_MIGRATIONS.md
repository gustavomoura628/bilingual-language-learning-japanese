# DESIGN — Issue #17: Database structure review + migration plan

> Input: `.claude/sdd/_synthesized/DEFINE_ISSUE_17_DB_VERSIONED_MIGRATIONS.md`. The DEFINE doc's
> "Resolved at refine" section is binding — this design does not re-litigate the versioning
> mechanism (`PRAGMA user_version`) or the lang-keying approach (`words` gains `lang`, table
> rebuild). It specifies their concrete mechanics. Grounded directly against `bll/db.py` (full
> read), `tests/test_db.py` (full read), `bll/web.py` (lemma-keyed routes, lines 220-368),
> `pyproject.toml`, `.github/workflows/tests.yml` (full reads), and a repo-wide grep confirming
> `_migrate`/`SCHEMA`/`user_version` have zero call sites outside `db.py` (safe to restructure
> internally) and `backup()` has exactly one existing caller (`cli.py:361`).

## Architecture overview

```text
┌────────────────────────────────────────────────────────────────────────────────┐
│ bll/db.py                                                                      │
│                                                                                 │
│  SCHEMA (str)                     MIGRATIONS: list[Callable[[Connection],None]]│
│  ── CREATE TABLE IF NOT EXISTS ── ── ordered migration steps ──────────────── │
│  words (... lang TEXT NOT NULL    [0] _migration_v1_baseline   (v0 -> v1)      │
│         DEFAULT 'ja' ...,          old ad-hoc _migrate(): column-check ALTERs  │
│         UNIQUE(lemma, lang))       + the 2 data-condition backfills, unchanged │
│  episodes, sightings, variants,   [1] _migration_v2_lang_keying (v1 -> v2)     │
│  align_cache (unchanged)           adds `lang`; widens UNIQUE via table        │
│                                     rebuild (create/copy/drop/rename)          │
│                                                                                 │
│  SCHEMA_VERSION = len(MIGRATIONS)  # 2                                        │
└───────────────────────────────────┬─────────────────────────────────────────────┘
                                     │
                                     ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│ def connect(path=None) -> sqlite3.Connection:                                 │
│   1. pre_existing = path != ":memory:" and os.path.exists(path)   # BEFORE    │
│      sqlite3.connect() creates the file, else this is always False           │
│   2. os.makedirs(...); conn = sqlite3.connect(path); row_factory = Row       │
│   3. conn.executescript(SCHEMA)      # CREATE TABLE IF NOT EXISTS: no-op on  │
│                                       # any table that already exists,        │
│                                       # regardless of its column shape        │
│   4. _apply_migrations(conn, path, pre_existing):                            │
│        current = PRAGMA user_version                                         │
│        if current >= SCHEMA_VERSION: return         # fast path, no I/O      │
│        if pre_existing: backup(path)                 # one-shot safety net   │
│        BEGIN                                                                 │
│          for step in range(current, SCHEMA_VERSION):                         │
│              MIGRATIONS[step](conn)                                          │
│              PRAGMA user_version = step + 1          # f-string; see D1      │
│        COMMIT (or ROLLBACK + re-raise on any exception)                      │
│   5. conn.commit(); return conn                                              │
└────────────────────────────────────────────────────────────────────────────────┘
```

Every migration step is individually idempotent (guarded by its own `PRAGMA table_info`
existence check), so it is always safe to run against a DB that already has the target shape.
This is why a **brand-new** DB — created fresh from `SCHEMA`, which already declares `lang`
directly — still walks both steps (AT2 requires this) without side effects: `_migration_v1_baseline`
finds all its columns already present (no-op ALTERs skipped) and `_migration_v2_lang_keying` finds
`lang` already present (returns immediately, no rebuild). Both a genuinely fresh DB and Lucas's
real, populated, never-versioned DB report `PRAGMA user_version == 0` today — nothing in SQLite
distinguishes them at that level — which is why the backup trigger below is gated on **on-disk
file existence before this `connect()` call**, not on `current == 0`.

---

## Decision 1 — versioning mechanism (concrete mechanics)

**Data structure:** an ordered `list` of one-argument functions, each taking the open connection
and mutating it in place to advance exactly one version. Index `i` in the list is the migration
that takes the DB from version `i` to version `i+1`.

```python
from collections.abc import Callable  # new import — see "Imports" below

MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migration_v1_baseline,
    _migration_v2_lang_keying,
]
SCHEMA_VERSION: int = len(MIGRATIONS)
```

**Walk function** (replaces the current bare `_migrate(conn)` call inside `connect()`):

```python
def _apply_migrations(conn: sqlite3.Connection, path: str, pre_existing: bool) -> None:
    """Walk PRAGMA user_version from wherever this DB currently sits up to
    SCHEMA_VERSION, running each pending step in MIGRATIONS as one atomic
    transaction (all steps or none) so a mid-walk failure never leaves the
    DB stamped at a version this code never validated landing on cleanly.

    Backs up the file first, exactly once, iff this call is the first to
    advance an ALREADY-EXISTING on-disk DB. `pre_existing` (computed by the
    caller before sqlite3.connect() creates the file) is what gates this --
    not `current == 0` -- because a brand-new DB also reads user_version 0
    and has nothing to lose.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= SCHEMA_VERSION:
        return
    if pre_existing:
        backup(path)
    conn.execute("BEGIN")
    try:
        for step in range(current, SCHEMA_VERSION):
            MIGRATIONS[step](conn)
            conn.execute(f"PRAGMA user_version = {step + 1}")
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
```

**Non-obvious points the build phase should not have to rediscover:**

- `PRAGMA user_version = ?` **cannot be parameterized** — SQLite pragmas don't accept bind
  placeholders. The f-string is safe despite looking like an injection smell: `step + 1` is an
  internal `int` from `range()`, never user input.
- **Whole-walk atomicity, not per-step.** One `BEGIN`/`COMMIT` wraps every pending step in a
  single `connect()` call, not one transaction per step. The DEFINE doc says "migrations run
  transactionally" without specifying granularity; whole-walk is simpler, avoids ever landing on
  an intermediate version this codebase has never run standalone, and this repo's migrations are
  a rare, one-time-per-DB event, not a hot path needing fine-grained resumability.
- **The explicit `BEGIN` is load-bearing, not decorative.** Python's `sqlite3` module (legacy
  transaction mode, unchanged default in this codebase) auto-opens an implicit transaction before
  DML (INSERT/UPDATE/DELETE) but **not** before DDL (CREATE/ALTER/DROP TABLE) — each DDL statement
  otherwise commits immediately as its own unit. Without the explicit `conn.execute("BEGIN")`,
  the table-rebuild in Decision 2 (create, copy, drop, rename — 4 separate DDL statements) would
  NOT be atomic and a crash mid-rebuild could leave `words` dropped with `words_new` never renamed.
  This is exactly the failure mode "transactional migrations" (AT2) exists to prevent.
- **`PRAGMA foreign_keys` needs no handling inside the migration.** Confirmed by grep: this
  codebase never enables FK enforcement anywhere in `db.py` (`PRAGMA foreign_keys` is OFF by
  SQLite's own default and untouched), so `DROP TABLE words` while `sightings`/`variants`
  reference it is not blocked. Toggling the pragma defensively inside the open migration
  transaction would also be a **no-op anyway** — SQLite documents `PRAGMA foreign_keys` as
  inert once a transaction has started — so don't add dead code that looks protective but isn't.
- **`_migration_v1_baseline` is `_migrate`, renamed, body unchanged.** It's already correct and
  covered by the existing legacy test; this issue folds it into the list as step 0 verbatim
  (calls `_backfill_episode_meta`/`_backfill_learned` exactly as today).

`connect()`'s own body changes minimally:

```python
def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or DEFAULT_DB
    pre_existing = path != ":memory:" and os.path.exists(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _apply_migrations(conn, path, pre_existing)
    conn.commit()  # persist schema migrations + one-time backfills
    return conn
```

`pre_existing` must be computed **before** `sqlite3.connect(path)` runs — that call creates the
file on disk if it doesn't exist yet, so checking `os.path.exists` after it would always be `True`.

---

## Decision 2 — the `words` table rebuild (exact SQL)

`SCHEMA`'s `words` table gains `lang` and the widened constraint directly, so brand-new DBs get
the final shape from `CREATE TABLE` (consistent with how `last_seen_pos`/`note`/`learned_at`/
`learned_at_episode` are already baked into today's `SCHEMA` even though `_migrate` also
ALTER-guards them for pre-existing DBs):

```sql
CREATE TABLE IF NOT EXISTS words (
    id INTEGER PRIMARY KEY,
    lemma TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT 'ja',  -- language tag for this lemma (issue #16: zh-TW etc.)
    reading TEXT,
    romaji TEXT,
    gloss TEXT,
    note TEXT,
    pos TEXT,
    status TEXT NOT NULL DEFAULT 'learning',
    exposures INTEGER NOT NULL DEFAULT 0,
    last_seen_pos INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT,
    learned_at TEXT,
    learned_at_episode TEXT,
    added_at TEXT NOT NULL,
    UNIQUE(lemma, lang)
);
```

(`lemma`'s column-level `UNIQUE` is removed; uniqueness moves to the table-level `UNIQUE(lemma,
lang)` clause — column-level `UNIQUE` can't express a composite key.)

For an **existing** DB (`CREATE TABLE IF NOT EXISTS` is a no-op on a table that already exists,
regardless of column mismatch — same reasoning `_migrate`'s own docstring already states), the
rebuild helper does the actual work, guarded for idempotency:

```python
def _migration_v2_lang_keying(conn: sqlite3.Connection) -> None:
    """v1 -> v2: words gains lang TEXT NOT NULL DEFAULT 'ja'; UNIQUE(lemma)
    widens to UNIQUE(lemma, lang). Idempotent -- skips the rebuild if `lang`
    is already a column (true for any DB created fresh from SCHEMA)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(words)")}
    if "lang" in have:
        return
    _rebuild_words_table_add_lang(conn)


def _rebuild_words_table_add_lang(conn: sqlite3.Connection) -> None:
    """SQLite can't ALTER a UNIQUE constraint in place: create a new table
    with the target shape, copy every row across with lang='ja', drop the
    old table, rename the new one into place. Explicit column lists in both
    the CREATE and the INSERT...SELECT -- never SELECT * -- so column-order
    drift between the two tables can't silently corrupt data. `id` values
    are copied unchanged (SQLite's plain INTEGER PRIMARY KEY is just the
    rowid; explicit id in the column list preserves it exactly), so
    sightings.word_id and variants.word_id keep resolving to the same rows.
    PRAGMA foreign_keys is off by default in this codebase and untouched --
    no special handling needed (see Decision 1)."""
    conn.execute(
        """
        CREATE TABLE words_new (
            id INTEGER PRIMARY KEY,
            lemma TEXT NOT NULL,
            lang TEXT NOT NULL DEFAULT 'ja',
            reading TEXT,
            romaji TEXT,
            gloss TEXT,
            note TEXT,
            pos TEXT,
            status TEXT NOT NULL DEFAULT 'learning',
            exposures INTEGER NOT NULL DEFAULT 0,
            last_seen_pos INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT,
            learned_at TEXT,
            learned_at_episode TEXT,
            added_at TEXT NOT NULL,
            UNIQUE(lemma, lang)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO words_new (
            id, lemma, lang, reading, romaji, gloss, note, pos, status,
            exposures, last_seen_pos, first_seen, learned_at,
            learned_at_episode, added_at
        )
        SELECT
            id, lemma, 'ja', reading, romaji, gloss, note, pos, status,
            exposures, last_seen_pos, first_seen, learned_at,
            learned_at_episode, added_at
        FROM words
        """
    )
    conn.execute("DROP TABLE words")
    conn.execute("ALTER TABLE words_new RENAME TO words")
```

Both `CREATE TABLE words_new` and the target `SCHEMA` shape must stay in lockstep by hand (no
shared constant) — acceptable here since both are short, adjacent, and reviewed together; flagged
so a future column addition to `words` doesn't update one and forget the other.

---

## Decision 3 — `align_cache` / `variants` (and `episodes` / `sightings`) lang-awareness

Per-table verdict, with reasoning (feeds directly into `docs/DB_AUDIT.md`, Decision 4):

| Table | Verdict | Reasoning |
|---|---|---|
| `words` | **CHANGE** | This issue's migration v1→v2 (Decision 2). |
| `sightings` | **KEEP** | PK `(word_id, episode_id)`, no `lemma`/language column of its own. `word_variants`/`record_sighting` etc. always operate through the numeric `word_id`, which now points at a row that carries `lang`. Inherits lang-awareness "for free" via indirection — no change needed. |
| `variants` | **KEEP** | Identical reasoning to `sightings`: keyed purely by `word_id`, no lemma/language column. `record_variant`/`word_variants` never touch `lemma` directly. |
| `episodes` | **WATCH — blocked on #16** | No language-track column exists. Whether a zh-TW viewing session shares an `episodes` row with its JA counterpart (same media, two vocab tracks) or gets a separate row is a #16 ingestion-design question — not decided here, no shape assumed. |
| `align_cache` | **WATCH — blocked on #16** | PK is `(ja_line, en_text, lemma)`; `lemma` is a bare `TEXT` value, **not** a FK to `words.id` — it does not inherit lang-awareness through indirection the way `sightings`/`variants` do. A future zh-TW lemma that renders identically to an existing JA lemma could collide. Not fixed now because the real fix isn't just adding `lang` to the PK: the column is literally named `ja_line`, baking "Japanese" into the schema itself. A proper fix (rename `ja_line`, widen the PK) is a rebuild of similar shape to Decision 2, better designed once #16's actual zh-TW ingestion shape is known rather than guessed at here. Worst case pre-fix: one wrong cached alignment served once — `align_cache` is a cache by definition, safe to clear, so blast radius is small. |

`sightings`/`variants` = **keep, resolved, not deferred** (there is nothing to watch for — the
indirection through `word_id` is a permanent property of the schema, not a temporary gap).
`episodes`/`align_cache` = **genuine watches**, each with a concrete trigger (#16 landing) and a
named fix shape, not a silent "ignore and hope."

---

## Decision 4 — docstring fix

`_backfill_episode_meta`'s docstring currently claims `"e04.ja.srt" -> "04"`. Traced against the
actual regex `r"(?:e|ep|episode|\bx)\s*0*(\d+)"`: the greedy `0*` consumes the leading zero
*before* `(\d+)` starts capturing, so the captured group is `"4"`, not `"04"` — confirmed by
`tests/test_db.py`'s own existing assertion (`("e04.ja.srt", "4")` at
`test_connect_migrates_and_backfills_legacy_schema`, line ~238) and by the test's own comment
directly above it flagging this exact mismatch.

```python
def _backfill_episode_meta(conn: sqlite3.Connection) -> None:
    """Parse an episode number out of each episode's filename that doesn't have
    one yet (e.g. e04.ja.srt -> "4" -- the regex's greedy 0* consumes any
    leading zeros before the capture group starts). Show is left for the
    operator to set."""
```

---

## Decision 5 — one-time pre-migration backup: reuse `backup()` unmodified

**Choice: call the existing `backup(path)` helper as-is, unmodified, from inside
`_apply_migrations`, gated on `pre_existing` (Decision 1).** No new parameter, no new function.

**Why this beats a custom `bll.db.bak-v<N>` naming scheme:** the issue text's `bak-v<N>` phrasing
describes intent ("a labeled snapshot before the risky migration runs"), not a literal filename
the acceptance criteria string-match. `backup()` already does exactly the needed thing — copies
the file into `backups/` next to it with rotation — and is already exercised by `cli.py:361`'s
existing call site, so reusing it verbatim adds zero new surface to get wrong. Building a
parallel `label=` parameter just to rename the output file would be new, untested code purely for
cosmetic naming, which is the opposite of "trivial." The PR body (AT6) is what actually protects
Lucas — it must say plainly that his real vocab DB will be backed up automatically to `backups/`
the next time he runs `bll` after this PR merges, pointing at the real timestamped filename
convention `backup()` already produces.

**Reused exactly as-is** — no changes to `backup()`'s signature, body, or existing tests.

---

## Decision 6 — imports

`bll/db.py` needs exactly one new import line, added after the existing stdlib imports
(alphabetical within the `from`-imports group, `collections.abc` sorts before `datetime`):

```python
from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
```

No other file needs a new import. `sqlite3.Connection`/`sqlite3.Row` are already imported.

---

## File manifest

| # | File | Action | Purpose |
|---|------|--------|---------|
| 1 | `bll/db.py` | Modify | `SCHEMA`'s `words` table, `_migration_v1_baseline` (renamed from `_migrate`, body unchanged), new `_migration_v2_lang_keying` + `_rebuild_words_table_add_lang`, new `MIGRATIONS`/`SCHEMA_VERSION`, new `_apply_migrations`, updated `connect()`, docstring fix, new `Callable` import |
| 2 | `tests/test_db.py` | Modify | Extend `test_connect_migrates_and_backfills_legacy_schema` with a `PRAGMA user_version` assertion; add new `test_connect_migrates_words_lang_with_no_data_loss` |
| 3 | `docs/DB_AUDIT.md` | Create | Per-table audit doc (AT1) — content specified in full below |

No changes to `bll/cli.py`, `bll/web.py`, `bll/jmdict.py`, or any other module (Decision 7,
explicit scope boundary).

---

## Test plan

### (a) Extend the existing legacy-migration test

Add, at the end of `test_connect_migrates_and_backfills_legacy_schema` (after the existing
`words`/`episodes` column-presence and backfill-value assertions):

```python
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    words_after = db.all_words(conn)
    assert all(r["lang"] == "ja" for r in words_after.values())
```

This proves the legacy→versioned upgrade lands on the *latest* version end-to-end (AT5) — not
just that individual columns exist, which the test already checked — and folds in a cheap `lang`
spot-check on the same fixture the test already builds (both words get `lang='ja'`).

### (b) New no-data-loss test

Targets specifically the v1→v2 rebuild (the riskiest step — the only one that drops a table).
Builds a DB at **today's current schema shape** (i.e. everything `_migration_v1_baseline` already
produces: `last_seen_pos`/`note`/`learned_at`/`learned_at_episode` present) but *without* `lang`
and with the old single-column `UNIQUE(lemma)` — i.e., "v1," one hop short of target — with rows
in `words`, `episodes`, `sightings`, **and** `variants`, so the assertion directly exercises that
`sightings.word_id`/`variants.word_id` still resolve correctly after the rebuild (proving `id`
preservation, not just "no exception was raised"):

```python
# --- new no-data-loss test for the v1 -> v2 (lang) migration ---
#
# Shape = today's current SCHEMA (pre-#17): everything _migration_v1_baseline
# already produces, but words still has UNIQUE(lemma) and no lang column.
# Populates words + episodes + sightings + variants so the assertions can
# prove id-preservation across the rebuild, not just column presence.
PRE_LANG_SCHEMA = """
CREATE TABLE words (
    id INTEGER PRIMARY KEY,
    lemma TEXT UNIQUE NOT NULL,
    reading TEXT,
    romaji TEXT,
    gloss TEXT,
    note TEXT,
    pos TEXT,
    status TEXT NOT NULL DEFAULT 'learning',
    exposures INTEGER NOT NULL DEFAULT 0,
    last_seen_pos INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT,
    learned_at TEXT,
    learned_at_episode TEXT,
    added_at TEXT NOT NULL
);
CREATE TABLE episodes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    new_words INTEGER NOT NULL DEFAULT 0,
    replacements INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    show TEXT,
    episode_no TEXT
);
CREATE TABLE sightings (
    word_id INTEGER NOT NULL REFERENCES words(id),
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    occurrences INTEGER NOT NULL DEFAULT 0,
    replacements INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (word_id, episode_id)
);
CREATE TABLE variants (
    word_id INTEGER NOT NULL REFERENCES words(id),
    en_word TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (word_id, en_word)
);
"""


def test_connect_migrates_words_lang_with_no_data_loss(tmp_path):
    path = str(tmp_path / "prelang.db")

    raw = sqlite3.connect(path)
    raw.executescript(PRE_LANG_SCHEMA)
    w1 = raw.execute(
        "INSERT INTO words (lemma, reading, romaji, gloss, pos, exposures, added_at) "
        "VALUES ('猫','ネコ','neko','cat','noun',5,'2026-01-01T00:00:00')"
    ).lastrowid
    w2 = raw.execute(
        "INSERT INTO words (lemma, reading, romaji, gloss, pos, exposures, added_at) "
        "VALUES ('覚える','おぼえる','oboeru','to memorize','verb',12,'2026-01-01T00:00:00')"
    ).lastrowid
    ep1 = raw.execute(
        "INSERT INTO episodes (name, processed_at, new_words, replacements, tokens) "
        "VALUES ('e01.ja.srt','2026-01-01T00:00:00',2,5,120)"
    ).lastrowid
    raw.execute(
        "INSERT INTO sightings (word_id, episode_id, occurrences, replacements) VALUES (?,?,3,3)",
        (w1, ep1),
    )
    raw.execute(
        "INSERT INTO variants (word_id, en_word, count) VALUES (?, 'cat', 3)",
        (w1,),
    )
    raw.commit()
    raw.close()

    conn = db.connect(path)  # runs the full v0(effectively v1)->v2 walk

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION

    words = db.all_words(conn)
    assert set(words) == {"猫", "覚える"}  # every pre-existing row survives
    assert all(r["lang"] == "ja" for r in words.values())
    assert words["猫"]["id"] == w1  # id preserved across the rebuild
    assert words["覚える"]["id"] == w2

    # sightings/variants still resolve through the preserved id
    sighting = conn.execute(
        "SELECT occurrences FROM sightings WHERE word_id=? AND episode_id=?", (w1, ep1)
    ).fetchone()
    assert sighting["occurrences"] == 3
    variants = db.word_variants(conn, w1)
    assert variants == {"cat": 3}
```

Both tests follow the existing file's established pattern: a raw `sqlite3` connection builds and
commits+closes the legacy fixture *before* `db.connect()` opens the same path (per the file's own
module docstring on why `:memory:` can't express "an existing older DB").

---

## Decision 7 — explicitly out of scope (do not touch these call sites)

This issue makes `words` language-aware at the schema/migration level **only**. It does **not**
plumb a `lang` parameter through any lemma-keyed call site — that is future zh-TW
ingestion-pipeline work for issue #16:

- `bll/db.py`: `upsert_word` (`WHERE lemma=?`), `set_status` (`WHERE lemma=?`), `set_note`
  (`WHERE lemma=?`), `all_words` (`dict[lemma] -> row`, which would silently collapse two
  same-lemma-different-`lang` rows into one entry once non-`ja` data exists)
- `bll/web.py`: `POST /api/word/{lemma}` (`update_word`, ~line 271), `GET
  /api/word/{lemma}/history` (`word_history`, ~line 332), and the `GET /api/words` listing
  endpoint (queries `words` with no lang filter, ~line 220)

All of the above default correctly today because every existing row is `lang='ja'` — they are not
*wrong*, just not lang-safe yet. Recorded as the explicit "watch" scope note in `docs/DB_AUDIT.md`
below, so issue #16 starts from a named checklist instead of rediscovering these sites.

Also out of scope, unchanged from the DEFINE doc: switching databases, ORM adoption, per-user
keying (blocked on #19), any behavior change to `_backfill_episode_meta`'s actual regex (only its
docstring is wrong, not its logic — do not "fix" the regex to produce `"04"`, that would change
real behavior current tests depend on).

---

## `docs/DB_AUDIT.md` — full content (AT1, AT4 boundary note)

```markdown
# Database Schema Audit — bll/db.py

> Written as part of issue #17 (versioned migrations + language-keying seam). Verdicts are
> keep / change / watch per table. "Watch" entries name the trigger condition that would
> promote them to "change" -- they are not silently deferred.

## Versioning

As of this issue, `bll/db.py` tracks schema version via SQLite's built-in `PRAGMA
user_version` integer counter, walked by an ordered list of migration steps
(`db.MIGRATIONS`) on every `connect()`. The pre-#17 ad-hoc `_migrate()` column-check +
backfill logic becomes migration step v0->v1 (`_migration_v1_baseline`), unchanged in
behavior. This issue adds v1->v2 (`_migration_v2_lang_keying`).

## Per-table verdicts

| Table | Verdict | Detail |
|---|---|---|
| `words` | **CHANGE** (this issue, migration v1->v2) | Adds `lang TEXT NOT NULL DEFAULT 'ja'`; widens `UNIQUE(lemma)` to `UNIQUE(lemma, lang)` via the SQLite create-copy-drop-rename rebuild pattern (constraints can't be altered in place). Existing rows backfill `lang='ja'`; `id` values are preserved unchanged so `sightings.word_id`/`variants.word_id` references stay valid. |
| `episodes` | **WATCH** -- blocked on #16 | No `lang`/language-track column exists. Whether a zh-TW viewing session shares an `episodes` row with its JA counterpart (same media, two vocab tracks) or gets its own row is a #16 ingestion-design question, not decided here -- no shape assumed. |
| `sightings` | **KEEP** | Keyed by `(word_id, episode_id)`, no `lemma`/language data of its own. Inherits lang-awareness "for free" transitively through `word_id -> words.id`, which now carries `lang`. No change needed. |
| `variants` | **KEEP** | Same reasoning as `sightings` -- keyed by `word_id` only, no lemma/language column, fully indirected through `words.id`. |
| `align_cache` | **WATCH** -- blocked on #16 | PK is `(ja_line, en_text, lemma)`; `lemma` is stored as a bare `TEXT` value, not a FK to `words.id`, so it does NOT inherit lang-awareness through indirection the way `sightings`/`variants` do. A future zh-TW lemma that happens to render identically to an existing JA lemma could collide in this cache. Not fixed now because the correct fix isn't just adding a `lang` column to the PK -- the column is literally named `ja_line`, baking "Japanese" into the schema itself, so a proper fix (rename `ja_line` -> a language-neutral name, add `lang` to the PK) is a rebuild of similar shape to this issue's `words` change, better designed once #16's actual zh-TW ingestion shape is known. Worst case if a collision does occur before then: a wrong cached alignment is served once; `align_cache` is a cache by definition (safe to clear/rebuild), so the blast radius is small. |
| *(all 5 tables)* | **WATCH -- blocked on #19** | Per-user keying. None of the 5 tables have any per-user column today; this issue does not add one. Out of scope per #17's own DEFINE; #19 owns the design. |

## Scope note (issue #16 seam)

This issue makes `words` language-aware at the schema/migration level only. It deliberately
does **not** plumb a `lang` parameter through the lemma-keyed call sites below -- that is
future zh-TW ingestion-pipeline work for issue #16:

- `bll/db.py`: `upsert_word`, `set_status`, `set_note`, `all_words` (all take/key by `lemma`
  alone; `all_words`'s `dict[lemma] -> row` return shape would silently collapse two
  same-lemma, different-`lang` rows into one entry once non-`ja` data exists)
- `bll/web.py`: `POST /api/word/{lemma}` (`update_word`), `GET /api/word/{lemma}/history`
  (`word_history`), and the `GET /api/words` listing endpoint (queries `words` with no lang
  filter)

All of the above default correctly today because every existing row is `lang='ja'` and #16
hasn't landed yet -- but they are not lang-safe, and issue #16 should treat this list as its
starting checklist.

## Known pre-existing rough edge, fixed in this issue

`_backfill_episode_meta`'s docstring previously claimed `"e04.ja.srt" -> "04"`; the regex's
greedy `0*` actually captures `"4"` (docstring corrected, behavior unchanged).
```

---

## Type annotations (full signatures, every new/modified function)

`pyproject.toml`'s `[[tool.mypy.overrides]] module = ["bll.db", ...] disallow_untyped_defs = true`
already scopes `bll.db` — every signature below must be fully annotated or `mypy bll/` fails CI.
`from __future__ import annotations` is already present in the file (modern `X | None` syntax
usable everywhere).

```python
def _migration_v1_baseline(conn: sqlite3.Connection) -> None: ...        # renamed from _migrate
def _migration_v2_lang_keying(conn: sqlite3.Connection) -> None: ...     # new
def _rebuild_words_table_add_lang(conn: sqlite3.Connection) -> None: ... # new
def _apply_migrations(conn: sqlite3.Connection, path: str, pre_existing: bool) -> None: ...  # new
def connect(path: str | None = None) -> sqlite3.Connection: ...          # signature unchanged
```

`_backfill_episode_meta`/`_backfill_learned` keep their existing (already-annotated) signatures
unchanged — only `_backfill_episode_meta`'s docstring text changes (Decision 4). `backup()` is
reused verbatim, zero signature change (Decision 5). `MIGRATIONS`/`SCHEMA_VERSION` are annotated
module-level constants (shown in Decision 1) — not required by `disallow_untyped_defs` (which
only gates `def`s), but included for clarity since they define the exact contract every migration
step must satisfy.

---

## Risk assessment / PR body requirement

| Risk | Mitigation |
|---|---|
| Lucas's real vocab DB migrates on his next `bll` run (AMBER blast radius per DEFINE) | Automatic `backup()` snapshot before the migration runs (Decision 5); PR body **must** state this plainly, naming the `backups/` directory and that it happens automatically — not asking Lucas to do anything manually |
| Mid-migration crash leaves DB in a partial state | Whole-walk transaction (Decision 1) — SQLite DDL is fully rollback-safe inside an explicit `BEGIN` |
| `words` rebuild silently drops/corrupts rows | Explicit column lists throughout (never `SELECT *`); new no-data-loss test (test plan (b)) asserts every row + its `id` survives |
| `align_cache`/`episodes` lang-collision risk deferred | Documented as named "watch" entries with trigger conditions in `docs/DB_AUDIT.md`, not silently ignored |
| Lemma-keyed call sites (`upsert_word` etc.) not lang-safe yet | Explicitly scoped out (Decision 7) and listed as issue #16's starting checklist in the audit doc |

---

## Out of scope (carried from DEFINE, not re-litigated)

Switching databases; ORM adoption; per-user keying (blocked on #19); plumbing `lang` through any
lemma-keyed call site (blocked on #16); fixing `_backfill_episode_meta`'s actual regex behavior
(only its docstring was wrong); any git/gh operations (this phase is read-only planning; build
executes it).

## Next step

**Ready for:** `/build .claude/sdd/features/DESIGN_ISSUE_17_DB_VERSIONED_MIGRATIONS.md`
