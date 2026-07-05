# DESIGN — Issue #8: Behavioral test coverage for `bll/db.py` and `bll/timeline.py`

> Input: `.claude/sdd/_synthesized/DEFINE_ISSUE_8_BEHAVIORAL_TEST_COVERAGE.md`. KB-first resolution:
> loaded `kb/testing/concepts/{fixtures,pytest-basics}.md`, `kb/testing/patterns/unit-test-
> patterns.md`, `kb/testing/quick-reference.md` (pytest fixture-scope, parametrize, `pytest.raises`
> conventions); agent match found (`agents/test/test-generator.md` — pytest/fixtures domain, `testing`
> in its `kb_domains`) → **confidence 0.95**. No data-engineering/infra KB domain applies (same
> conclusion as `DESIGN_ISSUE_6`) — this is a single-domain, test-only change. Every scenario below
> that is even slightly non-obvious (AT6's migration numbers, AT9's white-box registry injection, the
> commit/visibility interaction between `db.py` and `timeline.py` connections) was **executed against
> the real `bll/db.py` and `bll/timeline.py` in this checkout**, not hand-traced — the exact values
> asserted below are verified output, not guesses.

## Coverage overview

```text
┌───────────────────────────────────────────────────────────────────────────┐
│  tests/test_db.py  ──covers──▶  bll/db.py            (7 test functions)   │
│    AT1 set_status transitions (x3, parametrized)                          │
│    AT2 set_status on unknown lemma (no-op)                                │
│    AT3 record_sighting: exposures accumulate + sightings upsert-aggregate │
│    AT4 stamp_learned idempotency                                         │
│    AT5 clock() sums tokens  /  touch_last_seen() updates position         │
│    AT6 connect() migration + backfill from a hand-built legacy schema     │
├───────────────────────────────────────────────────────────────────────────┤
│  tests/test_timeline.py  ──covers──▶  bll/timeline.py  (7 test functions) │
│    AT7  init() idempotent  →  record() snapshots a new episode  →  no-op  │
│    AT8  branch(): success + keeps source branch  /  no-snapshot ValueError│
│    AT9  switch(): success  /  unknown-branch  /  no-snapshot (white-box)  │
│    AT10 state(): per-branch head_pos/fork_pos + current branch's seekable │
└───────────────────────────────────────────────────────────────────────────┘
  test_timeline.py's DB fixture is built exclusively through bll/db.py's own
  public API (db.connect + db.add_episode) — never an invented schema.
```

All 14 tests run against `:memory:` SQLite or a single tiny `tmp_path` file per test; no sleeps, no
network. Measured total runtime for this shape of test (in-process SQLite, no I/O beyond a few KB
of file copies) is well under the "a second or two" budget in DEFINE's constraints.

Two small, deliberate deltas from the 4 existing test files, called out once here rather than
per-test: (1) `import pytest` is added to both new files — none of the existing 4 need it today
(they only use `monkeypatch`/`tmp_path`, which pytest injects by fixture name with no import
required), but `@pytest.fixture`, `@pytest.mark.parametrize`, and `pytest.raises(...)` all need the
module itself. (2) No new `conftest.py` — both files stay self-contained (their own fixtures/helpers
defined locally), matching the repo's existing one-file-one-unit convention and `DESIGN_ISSUE_6`'s
explicit "no new conftest.py" precedent.

---

## Decision 1 — `test_db.py`: one fresh `:memory:` connection per test, not a shared fixture

**Chosen: function-scoped `conn` fixture (pytest's default scope), a brand-new `db.connect(":memory:")` for every test.**

```python
@pytest.fixture
def conn():
    """Fresh in-memory DB per test. db.py's tables are mutated in place by
    almost every function under test (exposures, sightings, status), so a
    shared connection would make later tests depend on earlier tests' state
    and execution order. `:memory:` connect is in-process/no I/O, so the
    per-test cost is negligible."""
    return db.connect(":memory:")
```

Rationale (independent tests over a shared connection):

- **KB guidance is explicit on this**: `kb/testing/concepts/fixtures.md`'s "Common Mistakes" section
  and `kb/testing/quick-reference.md`'s "Common Pitfalls" table both call out "mutable state shared
  across tests" / scope-too-wide fixtures as *the* source of order-dependent flakiness, and prescribe
  function scope for exactly this reason.
- **Matches this repo's own established convention**: every existing test creates its own fresh
  `tmp_path`/`--db` (`test_backend_gate.py`; `test_process_e2e_smoke.py`'s `run_process()` helper is
  explicitly "fresh --db, fresh output dir" per test) — there is no precedent anywhere in this repo
  for tests sharing mutable fixture state.
- `db.connect(":memory:")` is cheap: it's a no-op `os.makedirs` (the dirname of
  `os.path.abspath(":memory:")` is just the cwd, which already exists) plus an in-process SQLite
  handle — no measurable per-test overhead, so independence costs nothing.

This fixture is used by every `test_db.py` test **except AT6** (the migration test), which needs a
real on-disk file and therefore uses `tmp_path` directly instead (per DEFINE's explicit constraint —
see Decision 3).

---

## Decision 2 — AT1, AT2, AT3, AT4, AT5: straightforward behavior over the `conn` fixture

All verified by executing them against `bll/db.py` in this checkout.

### AT1 + AT2 — `set_status`

```python
@pytest.mark.parametrize("status", ["known", "ignored", "learning"])
def test_set_status_transitions_update_row_and_return_rowcount_1(conn, status):
    db.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun", "e01.ja.srt")
    rowcount = db.set_status(conn, "猫", status)
    assert rowcount == 1
    assert db.all_words(conn)["猫"]["status"] == status


def test_set_status_unknown_lemma_is_noop(conn):
    rowcount = db.set_status(conn, "nonexistent", "known")
    assert rowcount == 0
    assert db.all_words(conn) == {}   # no row was created
```

Parametrizing over the three status values is what makes this test literally exercise "transitions
between `learning`/`known`/`ignored`" (AT1's own wording), for the cost of one extra decorator line —
a direct match for `kb/testing`'s "same logic, many inputs → `@pytest.mark.parametrize`" guidance.

### AT3 — `record_sighting`

```python
def test_record_sighting_accumulates_exposures_and_upserts_sightings(conn):
    wid = db.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun", "e01.ja.srt")
    ep1 = db.add_episode(conn, "e01.ja.srt", new_words=1, replacements=0)
    ep2 = db.add_episode(conn, "e02.ja.srt", new_words=0, replacements=0)

    db.record_sighting(conn, wid, ep1, occurrences=2, replacements=2)
    db.record_sighting(conn, wid, ep2, occurrences=3, replacements=3)
    assert db.all_words(conn)["猫"]["exposures"] == 5   # 2 + 3 across two episodes

    # Repeated call for the SAME (word_id, episode_id) pair must aggregate
    # (ON CONFLICT ... SET occurrences=occurrences+excluded.occurrences),
    # not overwrite.
    db.record_sighting(conn, wid, ep1, occurrences=1, replacements=1)
    row = conn.execute(
        "SELECT occurrences, replacements FROM sightings WHERE word_id=? AND episode_id=?",
        (wid, ep1),
    ).fetchone()
    assert (row["occurrences"], row["replacements"]) == (3, 3)   # 2+1, 2+1 -- not 1,1
    assert db.all_words(conn)["猫"]["exposures"] == 6             # 5 + 1
```

Kept as one function: both clauses of AT3 (cross-episode sum, same-episode upsert-aggregate) are
facets of the same `record_sighting` call sequence over the same two words/episodes — splitting
would only duplicate the arrange step.

### AT4 — `stamp_learned` idempotency

```python
def test_stamp_learned_is_idempotent(conn):
    wid = db.upsert_word(conn, "覚える", "おぼえる", "oboeru", "to memorize", "verb", "e01.ja.srt")
    ep = db.add_episode(conn, "e01.ja.srt", new_words=1, replacements=0)
    db.record_sighting(conn, wid, ep, occurrences=10, replacements=10)   # exposures -> 10

    db.stamp_learned(conn, wid, "e01.ja.srt", threshold=10)
    first = db.all_words(conn)["覚える"]
    assert first["learned_at"] is not None
    assert first["learned_at_episode"] == "e01.ja.srt"

    # Bump exposures further (still >= threshold) and call again with a
    # DIFFERENT episode_name. learned_at_episode, not learned_at's timestamp,
    # is the load-bearing assertion: both calls can land in the same
    # wall-clock second, so a buggy re-stamp could coincidentally leave
    # learned_at looking "unchanged" too. learned_at_episode flipping to
    # "e02.ja.srt" is the one signal a re-stamp bug cannot hide from.
    db.record_sighting(conn, wid, ep, occurrences=1, replacements=5)
    db.stamp_learned(conn, wid, "e02.ja.srt", threshold=10)
    second = db.all_words(conn)["覚える"]
    assert second["learned_at"] == first["learned_at"]
    assert second["learned_at_episode"] == "e01.ja.srt"   # NOT e02 -- unchanged
```

Note `stamp_learned`'s own signature requires `threshold` explicitly (it has no default — the
"hardcoded default of 10" in DEFINE refers to `_backfill_learned(conn, threshold=10)`, a different
function exercised by AT6, not this one).

### AT5 — `clock` / `touch_last_seen`

Split into two functions (unlike AT3/AT4, these exercise two functions with no shared setup — one
doesn't need episodes, the other doesn't need tokens):

```python
def test_clock_sums_episode_tokens(conn):
    assert db.clock(conn) == 0
    db.add_episode(conn, "e01.ja.srt", new_words=1, replacements=0, tokens=120)
    db.add_episode(conn, "e02.ja.srt", new_words=0, replacements=0, tokens=80)
    assert db.clock(conn) == 200


def test_touch_last_seen_updates_position(conn):
    wid = db.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun", "e01.ja.srt")
    assert db.all_words(conn)["猫"]["last_seen_pos"] == 0   # schema default
    db.touch_last_seen(conn, wid, 200)
    assert db.all_words(conn)["猫"]["last_seen_pos"] == 200
```

---

## Decision 3 — AT6: migration/backfill from a hand-built legacy schema (tricky point 1)

**Real `tmp_path` file, not `:memory:`** — this is DEFINE's explicit constraint and the only reason:
migration testing needs two independent connections observing the *same on-disk state* in sequence
(an older raw `sqlite3.connect()` that creates the legacy schema and seed rows, then `db.connect()`
on that same path to run the migration). Two `:memory:` connections are two unrelated databases —
`:memory:` cannot express "an existing older DB."

### Legacy schema DDL (hand-built, one column set removed per table relative to `db.SCHEMA`)

`words` loses exactly the four columns `_migrate` adds (`last_seen_pos`, `note`, `learned_at`,
`learned_at_episode`); `episodes` loses exactly the three it adds (`tokens`, `show`, `episode_no`).
`sightings` is reproduced at its **current, unchanged** definition (nothing in `_migrate` alters
it) since its rows are what `_backfill_learned` replays — if it doesn't already exist when
`db.connect()` calls `executescript(SCHEMA)`, `CREATE TABLE IF NOT EXISTS` would create it *empty*
and our seed rows would never make it in. `variants`/`align_cache` are omitted entirely — nothing
here touches them and `executescript` creates them fresh (harmlessly) regardless.

```python
LEGACY_SCHEMA = """
CREATE TABLE words (
    id INTEGER PRIMARY KEY,
    lemma TEXT UNIQUE NOT NULL,
    reading TEXT,
    romaji TEXT,
    gloss TEXT,
    pos TEXT,
    status TEXT NOT NULL DEFAULT 'learning',
    exposures INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT,
    added_at TEXT NOT NULL
);
CREATE TABLE episodes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    new_words INTEGER NOT NULL DEFAULT 0,
    replacements INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE sightings (
    word_id INTEGER NOT NULL REFERENCES words(id),
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    occurrences INTEGER NOT NULL DEFAULT 0,
    replacements INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (word_id, episode_id)
);
"""
```

### Seed rows

Three episodes (ids 1/2/3 by insertion order), named so **one** (`e04.ja.srt`) matches the
`_backfill_episode_meta` regex exactly as the issue suggested — but all three deliberately use the
same `eNN` convention, which backfills all three at once and is a stronger proof that the regex
loop (not a one-off fluke) is what's under test:

```python
EPISODES = [
    # (name, processed_at, new_words, replacements)
    ("e01.ja.srt", "2026-01-01T00:00:00", 3, 4),
    ("e02.ja.srt", "2026-01-02T00:00:00", 1, 5),
    ("e04.ja.srt", "2026-01-03T00:00:00", 2, 3),
]
```

Two words: `覚える` ("to memorize") reaches the threshold; `迷う` ("to hesitate") never does.
`words.exposures` is seeded consistent with the real invariant (sum of that word's
`sightings.replacements`), and sightings are seeded **in episode-id order** so the cumulative replay
genuinely has to walk episodes 1→2→3 to find the crossing point, not just read row 1 in isolation:

```python
WORDS = [
    # (lemma, reading, romaji, gloss, pos, exposures)
    ("覚える", "おぼえる", "oboeru", "to memorize", "verb", 12),   # 4+5+3, crosses 10 at ep3
    ("迷う",   "まよう",   "mayou", "to hesitate",  "verb", 9),    # 3+3+3, never crosses 10
]

SIGHTINGS = [
    # (lemma, episode_name, occurrences, replacements)
    ("覚える", "e01.ja.srt", 5, 4),   # cumulative after e01: 4
    ("覚える", "e02.ja.srt", 5, 5),   # cumulative after e02: 9  (still under)
    ("覚える", "e04.ja.srt", 3, 3),   # cumulative after e04: 12 -- CROSSES 10 HERE
    ("迷う",   "e01.ja.srt", 2, 3),   # cumulative: 3
    ("迷う",   "e02.ja.srt", 2, 3),   # cumulative: 6
    ("迷う",   "e04.ja.srt", 2, 3),   # cumulative: 9  -- never reaches 10
]
```

**Build order (verified against the real modules):** open a raw `sqlite3.connect(str(path))`,
`executescript(LEGACY_SCHEMA)`, insert `EPISODES`/`WORDS`/`SIGHTINGS` (translate lemma/episode-name
to the ids `INTEGER PRIMARY KEY` assigned on insert — 覚える=1, 迷う=2, e01=1, e02=2, e04=3), then
**`.commit()` and `.close()` the raw connection before calling `db.connect(str(path))`** — closing
first is what makes this "two independent look-ins at the same on-disk state" rather than two
connections racing on one open handle.

### Assertions (executed, not hand-derived)

```python
conn = db.connect(str(path))   # runs _migrate() + both backfills

words_cols = {r["name"] for r in conn.execute("PRAGMA table_info(words)")}
assert {"last_seen_pos", "note", "learned_at", "learned_at_episode"} <= words_cols

episodes_cols = {r["name"] for r in conn.execute("PRAGMA table_info(episodes)")}
assert {"tokens", "show", "episode_no"} <= episodes_cols

# episode_no backfilled from the filename. NOTE: the regex's `0*` is greedy
# and consumes the leading zero before `(\d+)` captures the rest, so
# "e04.ja.srt" backfills to "4", NOT "04" (confirmed by execution -- the
# `_backfill_episode_meta` docstring's own "-> '04'" example comment does
# not match its regex's actual captured value; assert the real one).
eps = conn.execute("SELECT name, episode_no FROM episodes ORDER BY id").fetchall()
assert [(r["name"], r["episode_no"]) for r in eps] == [
    ("e01.ja.srt", "1"), ("e02.ja.srt", "2"), ("e04.ja.srt", "4"),
]

words = db.all_words(conn)
assert words["覚える"]["learned_at"] == "2026-01-03T00:00:00"     # e04's processed_at
assert words["覚える"]["learned_at_episode"] == "e04.ja.srt"      # first-crossing episode
assert words["覚える"]["last_seen_pos"] == 0                       # new column, defaulted
assert words["覚える"]["note"] is None                             # new column, defaulted

assert words["迷う"]["learned_at"] is None            # never crossed 10 -- stays unstamped
assert words["迷う"]["learned_at_episode"] is None
```

Every value above (column sets, the `"4"` not `"04"` capture, the exact `learned_at`/
`learned_at_episode` pair, `迷う`'s untouched `None`s) was confirmed by running this exact scenario
against `bll/db.py` in this checkout — not inferred from reading the source alone.

Not tested here (explicitly out of scope, not asked for by AT6): re-running `db.connect()` a second
time on the now-migrated file. `_migrate`'s own gating queries (`WHERE episode_no IS NULL`, `WHERE
learned_at IS NULL AND exposures>=10`) structurally guarantee that re-running is a cheap no-op once
backfilled, by construction — a second explicit test of that would be redundant scope, not a new
acceptance criterion.

---

## Decision 4 — `test_timeline.py`: campaign DB fixture built only from `db.py`'s real schema

**Fixture:**

```python
@pytest.fixture
def campaign_db(tmp_path):
    """A minimal, real campaign DB file using db.py's actual current schema
    (never a hand-rolled one) -- timeline.py only reads the `episodes`
    table's row count and each row's name/id ordering, so a full db.connect()
    is exactly enough surface and no more. Starts at 0 episodes; individual
    tests advance it via _add_episode()."""
    path = str(tmp_path / "campaign.db")
    db.connect(path).close()
    return path
```

The fixture returns a **plain `str` path** (not a `Path`, not an open connection) — this is what
every `timeline.py` function actually takes (`os.path.exists(db)`, `sqlite3.connect(db)`,
`shutil.copy2(db, ...)` throughout `timeline.py`), and matches how the real CLI passes `--db` (an
argparse string), so the fixture doesn't introduce a type the module wasn't written against.

**Helper (module-level function, not a fixture — needs a per-call `name` argument, matching this
repo's own precedent: `test_process_e2e_smoke.py`'s `run_process(tmp_path, run_name, gloss_json_path)`
is exactly this shape, a plain helper function rather than a pytest fixture-factory):**

```python
def _add_episode(path, name):
    """Advance the campaign DB's episode count by one. Opens its own
    connection and explicitly commits + closes it -- required, not
    stylistic: db.add_episode() itself never calls conn.commit(), and every
    timeline.py read (_episode_count, _last_episode_name) opens an
    INDEPENDENT sqlite3.connect(db) to the same file. A second connection to
    the same on-disk file only sees committed data (confirmed by executing
    this exact sequence: a separate connection reports 0 episodes before the
    commit, 1 after) -- and a bare conn.close() with no prior commit() rolls
    the insert back rather than persisting it (also confirmed by execution).
    Skipping this commit would make every timeline test silently see
    _episode_count() == 0 forever, since nothing here would ever surface a
    traceback -- record()/init() would just quietly never snapshot."""
    conn = db.connect(path)
    db.add_episode(conn, name, new_words=1, replacements=0)
    conn.commit()
    conn.close()
```

This is the single most important non-obvious fact this design rests on, so it's worth stating the
verification plainly: it was checked by inserting a row through one connection, reading the count
through a second, separate connection *before* calling `.commit()` on the first (got `0`), reading it
again *after* `.commit()` (got `1`), and separately confirming that `.close()`-without-`.commit()`
loses the write rather than persisting it. All three matched the reasoning above exactly.

`campaign_db` deliberately starts at **0** episodes rather than pre-seeding one, so every test states
its own episode-count progression explicitly via `_add_episode` — important for a module whose
entire public surface is keyed on *position*.

---

## Decision 5 — AT7: `init()` idempotency, `record()` snapshotting a new episode, then no-op

```python
def test_init_idempotent_then_record_snapshots_new_episode_then_noop(campaign_db):
    path = campaign_db
    _add_episode(path, "e01.ja.srt")

    reg1 = timeline.init(path)
    assert len(reg1["snaps"]) == 1
    first_snap_id = reg1["snaps"][0]["id"]

    reg2 = timeline.init(path)                        # unchanged episode count
    assert len(reg2["snaps"]) == 1
    assert reg2["snaps"][0]["id"] == first_snap_id     # same snapshot, not a new one

    _add_episode(path, "e02.ja.srt")                   # a NEW episode
    reg3 = timeline.record(path)
    assert len(reg3["snaps"]) == 2
    assert sorted(s["position"] for s in reg3["snaps"]) == [1, 2]

    reg4 = timeline.record(path)                       # no new episode since
    assert len(reg4["snaps"]) == 2
```

One function for all three clauses of AT7: each stage's assertions depend on the DB state the prior
stage left behind, so this reads as one continuous scenario rather than three independent facts.
(Aside, not asserted on: `record()` calls `init()` internally, and `init()` alone already contains
the "snapshot the current head if not already snapshotted" check — so the *new* snapshot after
adding e02 is technically taken by the `init()` call inside `record()`, with `record()`'s own
duplicate check being a no-op belt-and-suspenders. The test asserts the observable outcome the
acceptance criterion describes, not which internal call is technically responsible for it.)

---

## Decision 6 — AT8: `branch()` success + source-branch survival, and the no-snapshot failure

```python
def test_branch_at_snapshotted_position_succeeds_and_keeps_source_branch(campaign_db):
    path = campaign_db
    _add_episode(path, "e01.ja.srt"); timeline.record(path)
    _add_episode(path, "e02.ja.srt"); timeline.record(path)
    _add_episode(path, "e03.ja.srt"); timeline.record(path)

    bid = timeline.branch(path, 2, "Alt Take")
    assert bid == "alt-take"          # re.sub(r"[^a-z0-9]+", "-", ...) slug of "Alt Take"

    reg = timeline._load(path)
    assert reg["current"] == "alt-take"
    alt = reg["branches"]["alt-take"]
    assert alt["name"] == "Alt Take"
    assert alt["parent"] == "main"
    assert alt["fork_pos"] == 2

    # source branch's own snapshots remain untouched ("keep both")
    main_positions = sorted(s["position"] for s in reg["snaps"] if s["branch"] == "main")
    assert main_positions == [1, 2, 3]
    assert any(s["branch"] == "alt-take" and s["position"] == 2 for s in reg["snaps"])
    assert len(reg["snaps"]) == 4   # 3 (main) + 1 (alt-take's new branch-base snapshot)


def test_branch_at_position_without_snapshot_raises_value_error(campaign_db):
    with pytest.raises(ValueError, match="no snapshot"):
        timeline.branch(campaign_db, 1, "nope")
```

The failure case uses a **fresh, 0-episode `campaign_db`** and asks to branch from position 1 — no
episode, so no snapshot could ever exist there; `init()`'s own auto-snapshot-current-head check is
`if pos and ...` (falsy at `pos=0`), so it can't accidentally create one either. This is the minimal
setup that reaches the guard clause, proportional to what's being tested. Verified message: `"no
snapshot at episode #1 to branch from"`.

---

## Decision 7 — AT9: `switch()`'s three paths, including the white-box no-snapshot branch (tricky point 2)

**Success and unknown-branch are reachable through the public API alone:**

```python
def test_switch_to_existing_branch_with_snapshot_succeeds(campaign_db):
    path = campaign_db
    _add_episode(path, "e01.ja.srt"); timeline.record(path)
    _add_episode(path, "e02.ja.srt"); timeline.record(path)
    _add_episode(path, "e03.ja.srt"); timeline.record(path)
    timeline.branch(path, 2, "Alt Take")           # current -> alt-take; live file rewound to 2 eps
    assert timeline._episode_count(path) == 2

    reg = timeline.switch(path, "main")
    assert reg["current"] == "main"
    assert timeline._load(path)["current"] == "main"
    assert timeline._episode_count(path) == 3      # live DB file swapped back to main's head


def test_switch_to_unknown_branch_raises_value_error(campaign_db):
    with pytest.raises(ValueError, match="no such branch"):
        timeline.switch(campaign_db, "no-such-branch")
```

**The third failure mode — a branch that exists in the registry but has no snapshot — is genuinely
not reachable through `init`/`record`/`branch`/`switch` alone**, because `branch()`'s last step is
always `_take(db, reg, bid, position, snap["episode"], label="branch base")`: every branch the
public API can create is snapshotted in the same call that creates it. Reaching this state requires
directly writing the registry JSON that `_load`/`_save` read and write
(`<db path>.timeline/registry.json`), using the module's own (de)serialization so the injected shape
is exactly what `switch()` expects — not a hand-guessed JSON structure:

```python
def test_switch_to_branch_without_snapshot_raises_value_error(campaign_db):
    path = campaign_db
    _add_episode(path, "e01.ja.srt")
    timeline.record(path)                           # normal main@1 snapshot exists

    # White-box: hand-inject a branch registry entry with NO matching
    # snapshot record. Built via timeline._load/_save (the module's own
    # registry (de)serialization) so the shape matches exactly what
    # switch() expects -- not a hand-guessed JSON structure.
    reg = timeline._load(path)
    reg["branches"]["ghost"] = {
        "name": "Ghost", "parent": "main", "fork_pos": 1,
        "created": timeline._now(),
    }
    timeline._save(path, reg)

    with pytest.raises(ValueError, match="no snapshot"):
        timeline.switch(path, "ghost")
```

Traced (and executed) through `switch()`: its first line, `reg = init(db)`, reloads the registry from
disk — which now includes our injected `"ghost"` branch, since `init()` only *adds* a head snapshot
for the *current* branch (`"main"`, already snapshotted, so a no-op here) and never removes existing
branch entries. `"ghost" in reg["branches"]` is `True` (passes the unknown-branch check), `"ghost" !=
reg["current"]` (`"main"`, so it doesn't short-circuit-return), then `_head_snap(reg, "ghost")` finds
no snap for that branch and returns `None`, and `switch()` raises `ValueError(f"branch {branch_id}
has no snapshot")` — verified message: `"branch ghost has no snapshot"`.

---

## Decision 8 — AT10: `state()` summary after a mixed init/record/branch sequence

```python
def test_state_reports_branches_and_seekable_positions(campaign_db):
    path = campaign_db
    _add_episode(path, "e01.ja.srt"); timeline.record(path)
    _add_episode(path, "e02.ja.srt"); timeline.record(path)
    _add_episode(path, "e03.ja.srt"); timeline.record(path)
    timeline.branch(path, 2, "Alt Take")            # current -> alt-take, rewound to 2 episodes
    _add_episode(path, "e03-alt.ja.srt")             # a NEW, different episode 3 on this branch
    timeline.record(path)

    st = timeline.state(path)
    by_id = {b["id"]: b for b in st["branches"]}     # dict-index -- don't rely on list order

    assert st["current"] == "alt-take"
    assert by_id["main"]["fork_pos"] == 0
    assert by_id["main"]["head_pos"] == 3
    assert by_id["main"]["active"] is False
    assert by_id["alt-take"]["fork_pos"] == 2
    assert by_id["alt-take"]["head_pos"] == 3
    assert by_id["alt-take"]["active"] is True
    assert st["seekable"] == [2, 3]                  # alt-take's own snapshotted positions
```

`main`'s `head_pos` staying `3` even though the *live* file is currently on the `alt-take` branch's
content confirms `state()`/`_head_snap()` read purely from the registry's own snapshot records, not
from whatever the working DB file happens to hold at call time — which is the entire point of the
snapshot system. All values above were confirmed by executing this exact sequence.

---

## File manifest

| File | Action | Purpose | Agent | Rationale |
|------|--------|---------|-------|-----------|
| `tests/test_db.py` | Create | 7 test functions (1 parametrized x3) covering AT1–AT6 for `bll/db.py` | `@test-generator` | `agents/test/test-generator.md` — pytest/fixtures domain match, `testing` in its `kb_domains` |
| `tests/test_timeline.py` | Create | 7 test functions covering AT7–AT10 for `bll/timeline.py` | `@test-generator` | Same — pytest/fixtures domain match |

No other files. Per DEFINE's scope: no dependency changes (`pytest>=8.0` already in
`[dependency-groups] dev`, landed with #9/#12), no CI changes (workflow already wired), no
`conftest.py` (Decision 4 / overview).

---

## Traceability — acceptance test → test function(s)

| AT | Test function(s) | File |
|----|-------------------|------|
| AT1 | `test_set_status_transitions_update_row_and_return_rowcount_1` (parametrized) | `test_db.py` |
| AT2 | `test_set_status_unknown_lemma_is_noop` | `test_db.py` |
| AT3 | `test_record_sighting_accumulates_exposures_and_upserts_sightings` | `test_db.py` |
| AT4 | `test_stamp_learned_is_idempotent` | `test_db.py` |
| AT5 | `test_clock_sums_episode_tokens`, `test_touch_last_seen_updates_position` | `test_db.py` |
| AT6 | `test_connect_migrates_and_backfills_legacy_schema` | `test_db.py` |
| AT7 | `test_init_idempotent_then_record_snapshots_new_episode_then_noop` | `test_timeline.py` |
| AT8 | `test_branch_at_snapshotted_position_succeeds_and_keeps_source_branch`, `test_branch_at_position_without_snapshot_raises_value_error` | `test_timeline.py` |
| AT9 | `test_switch_to_existing_branch_with_snapshot_succeeds`, `test_switch_to_unknown_branch_raises_value_error`, `test_switch_to_branch_without_snapshot_raises_value_error` | `test_timeline.py` |
| AT10 | `test_state_reports_branches_and_seekable_positions` | `test_timeline.py` |

---

## Scope boundaries (carried verbatim from DEFINE, not re-litigated)

- `record_variant`/`word_variants`/`cache_get`/`cache_put`/`set_note` in `db.py`: not covered — not
  named by any acceptance test; no incidental coverage fell out of the fixtures above, so none is
  added.
- `_import_backups` in `timeline.py`: not covered — every `campaign_db` fixture is a fresh `tmp_path`
  with no `backups/` directory, so it runs as a harmless no-op in every test above; no dedicated test.
- The CLI module's cue-timing/cue-overlap alignment helper: a different module, explicitly out of
  scope per DEFINE's logged scope correction — not touched by this design.
- No CI/dependency file changes (see File manifest).
