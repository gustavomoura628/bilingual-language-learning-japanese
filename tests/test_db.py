"""Behavioral test coverage for bll/db.py's word/episode/sighting state
machine (issue #8, AT1-AT6): status transitions, exposure/sighting
accumulation, stamp_learned idempotency, the forgetting clock, and the
connect()-time migration + backfill path from a legacy (pre-migration)
schema.

Every test uses a fresh in-memory connection (see the `conn` fixture below) --
db.py's tables are mutated in place by almost every function under test, so a
shared connection would make later tests depend on earlier tests' state and
execution order. The one exception is the migration test
(test_connect_migrates_and_backfills_legacy_schema), which needs a real
on-disk file so two independent connections -- a raw sqlite3 connection that
builds the legacy schema, then db.connect() on that same path -- can observe
the same on-disk state in sequence; `:memory:` cannot express "an existing
older DB."

Run: pytest tests/test_db.py
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bll import db


@pytest.fixture
def conn():
    """Fresh in-memory DB per test. db.py's tables are mutated in place by
    almost every function under test (exposures, sightings, status), so a
    shared connection would make later tests depend on earlier tests' state
    and execution order. `:memory:` connect is in-process/no I/O, so the
    per-test cost is negligible."""
    return db.connect(":memory:")


@pytest.mark.parametrize("status", ["known", "ignored", "learning"])
def test_set_status_transitions_update_row_and_return_rowcount_1(conn, status):
    db.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun", "e01.ja.srt")
    rowcount = db.set_status(conn, "猫", status)
    assert rowcount == 1
    assert db.all_words(conn)["猫"]["status"] == status


def test_set_status_unknown_lemma_is_noop(conn):
    rowcount = db.set_status(conn, "nonexistent", "known")
    assert rowcount == 0
    assert db.all_words(conn) == {}  # no row was created


def test_record_sighting_accumulates_exposures_and_upserts_sightings(conn):
    wid = db.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun", "e01.ja.srt")
    ep1 = db.add_episode(conn, "e01.ja.srt", new_words=1, replacements=0)
    ep2 = db.add_episode(conn, "e02.ja.srt", new_words=0, replacements=0)

    db.record_sighting(conn, wid, ep1, occurrences=2, replacements=2)
    db.record_sighting(conn, wid, ep2, occurrences=3, replacements=3)
    assert db.all_words(conn)["猫"]["exposures"] == 5  # 2 + 3 across two episodes

    # Repeated call for the SAME (word_id, episode_id) pair must aggregate
    # (ON CONFLICT ... SET occurrences=occurrences+excluded.occurrences),
    # not overwrite.
    db.record_sighting(conn, wid, ep1, occurrences=1, replacements=1)
    row = conn.execute(
        "SELECT occurrences, replacements FROM sightings WHERE word_id=? AND episode_id=?",
        (wid, ep1),
    ).fetchone()
    assert (row["occurrences"], row["replacements"]) == (3, 3)  # 2+1, 2+1 -- not 1,1
    assert db.all_words(conn)["猫"]["exposures"] == 6  # 5 + 1


def test_stamp_learned_is_idempotent(conn):
    wid = db.upsert_word(conn, "覚える", "おぼえる", "oboeru", "to memorize", "verb", "e01.ja.srt")
    ep = db.add_episode(conn, "e01.ja.srt", new_words=1, replacements=0)
    db.record_sighting(conn, wid, ep, occurrences=10, replacements=10)  # exposures -> 10

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
    assert second["learned_at_episode"] == "e01.ja.srt"  # NOT e02 -- unchanged


def test_clock_sums_episode_tokens(conn):
    assert db.clock(conn) == 0
    db.add_episode(conn, "e01.ja.srt", new_words=1, replacements=0, tokens=120)
    db.add_episode(conn, "e02.ja.srt", new_words=0, replacements=0, tokens=80)
    assert db.clock(conn) == 200


def test_touch_last_seen_updates_position(conn):
    wid = db.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun", "e01.ja.srt")
    assert db.all_words(conn)["猫"]["last_seen_pos"] == 0  # schema default
    db.touch_last_seen(conn, wid, 200)
    assert db.all_words(conn)["猫"]["last_seen_pos"] == 200


# --- AT6: connect()-time migration + backfill from a hand-built legacy schema ---
#
# `words` loses exactly the four columns `_migration_v1_baseline` adds (last_seen_pos, note,
# learned_at, learned_at_episode); `episodes` loses exactly the three it adds
# (tokens, show, episode_no). `sightings` is reproduced at its current,
# unchanged definition (nothing in `_migration_v1_baseline` alters it) since its rows are
# what `_backfill_learned` replays -- if it doesn't already exist when
# db.connect() calls executescript(SCHEMA), CREATE TABLE IF NOT EXISTS would
# create it empty and the seed rows below would never make it in.
# `variants`/`align_cache` are omitted entirely -- nothing here touches them
# and executescript creates them fresh (harmlessly) regardless.
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

# Three episodes (ids 1/2/3 by insertion order), named so ONE (e04.ja.srt)
# matches the _backfill_episode_meta regex exactly as the issue suggested --
# but all three deliberately use the same eNN convention, which backfills all
# three at once and is a stronger proof that the regex loop (not a one-off
# fluke) is what's under test.
EPISODES = [
    # (name, processed_at, new_words, replacements)
    ("e01.ja.srt", "2026-01-01T00:00:00", 3, 4),
    ("e02.ja.srt", "2026-01-02T00:00:00", 1, 5),
    ("e04.ja.srt", "2026-01-03T00:00:00", 2, 3),
]

# Two words: 覚える ("to memorize") reaches the threshold; 迷う ("to
# hesitate") never does. exposures is seeded consistent with the real
# invariant (sum of that word's sightings.replacements).
WORDS = [
    # (lemma, reading, romaji, gloss, pos, exposures)
    ("覚える", "おぼえる", "oboeru", "to memorize", "verb", 12),  # 4+5+3, crosses 10 at ep3
    ("迷う", "まよう", "mayou", "to hesitate", "verb", 9),  # 3+3+3, never crosses 10
]

# Sightings seeded in episode-id order so the cumulative replay genuinely has
# to walk episodes 1->2->3 to find the crossing point, not just read row 1 in
# isolation.
SIGHTINGS = [
    # (lemma, episode_name, occurrences, replacements)
    ("覚える", "e01.ja.srt", 5, 4),  # cumulative after e01: 4
    ("覚える", "e02.ja.srt", 5, 5),  # cumulative after e02: 9  (still under)
    ("覚える", "e04.ja.srt", 3, 3),  # cumulative after e04: 12 -- CROSSES 10 HERE
    ("迷う", "e01.ja.srt", 2, 3),  # cumulative: 3
    ("迷う", "e02.ja.srt", 2, 3),  # cumulative: 6
    ("迷う", "e04.ja.srt", 2, 3),  # cumulative: 9  -- never reaches 10
]


def test_connect_migrates_and_backfills_legacy_schema(tmp_path):
    path = str(tmp_path / "legacy.db")

    # Build the legacy on-disk DB with a raw connection, independent of
    # db.py's own schema/migration path. Commit + close it BEFORE calling
    # db.connect() on the same path -- that is what makes this "two
    # independent look-ins at the same on-disk state" rather than two
    # connections racing on one open handle.
    raw = sqlite3.connect(path)
    raw.executescript(LEGACY_SCHEMA)
    ep_ids = {}
    for name, processed_at, new_words, replacements in EPISODES:
        cur = raw.execute(
            "INSERT INTO episodes (name, processed_at, new_words, replacements) VALUES (?,?,?,?)",
            (name, processed_at, new_words, replacements),
        )
        ep_ids[name] = cur.lastrowid
    word_ids = {}
    for lemma, reading, romaji, gloss, pos, exposures in WORDS:
        cur = raw.execute(
            "INSERT INTO words (lemma, reading, romaji, gloss, pos, exposures, added_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (lemma, reading, romaji, gloss, pos, exposures, "2026-01-01T00:00:00"),
        )
        word_ids[lemma] = cur.lastrowid
    for lemma, episode_name, occurrences, replacements in SIGHTINGS:
        raw.execute(
            "INSERT INTO sightings (word_id, episode_id, occurrences, replacements) "
            "VALUES (?,?,?,?)",
            (word_ids[lemma], ep_ids[episode_name], occurrences, replacements),
        )
    raw.commit()
    raw.close()

    conn = db.connect(path)  # runs _migration_v1_baseline() + both backfills

    words_cols = {r["name"] for r in conn.execute("PRAGMA table_info(words)")}
    assert {"last_seen_pos", "note", "learned_at", "learned_at_episode"} <= words_cols

    episodes_cols = {r["name"] for r in conn.execute("PRAGMA table_info(episodes)")}
    assert {"tokens", "show", "episode_no"} <= episodes_cols

    # episode_no backfilled from the filename. NOTE: the regex's `0*` is
    # greedy and consumes the leading zero before `(\d+)` captures the rest,
    # so "e04.ja.srt" backfills to "4", NOT "04" (confirmed by execution --
    # the _backfill_episode_meta docstring's own "-> '04'" example comment
    # does not match its regex's actual captured value; assert the real one).
    eps = conn.execute("SELECT name, episode_no FROM episodes ORDER BY id").fetchall()
    assert [(r["name"], r["episode_no"]) for r in eps] == [
        ("e01.ja.srt", "1"),
        ("e02.ja.srt", "2"),
        ("e04.ja.srt", "4"),
    ]

    words = db.all_words(conn)
    assert words["覚える"]["learned_at"] == "2026-01-03T00:00:00"  # e04's processed_at
    assert words["覚える"]["learned_at_episode"] == "e04.ja.srt"  # first-crossing episode
    assert words["覚える"]["last_seen_pos"] == 0  # new column, defaulted
    assert words["覚える"]["note"] is None  # new column, defaulted

    assert words["迷う"]["learned_at"] is None  # never crossed 10 -- stays unstamped
    assert words["迷う"]["learned_at_episode"] is None

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    words_after = db.all_words(conn)
    assert all(r["lang"] == "ja" for r in words_after.values())


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


# --- issue #28 (epic #16, slice A): lang plumbing through words call-sites ---
#
# These three tests exercise the new `lang` parameter on upsert_word/all_words/
# set_status/set_note directly (unit-level, not end-to-end). Every pre-existing
# call above this point in the file keeps its original argument count and
# keeps passing unmodified -- that unmodified suite is itself the primary
# byte-invariance regression check for db.py (see DESIGN_ISSUE_28_LANG_PLUMBING.md).


def test_upsert_word_same_lemma_different_lang_coexist(conn):
    wid_ja = db.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun", "e01.ja.srt")
    wid_zh = db.upsert_word(conn, "猫", None, None, "cat (zh)", "noun", "e01.zh.srt", lang="zh-TW")
    assert wid_ja != wid_zh  # two distinct rows -- same lemma did not upsert-over across langs

    default_scope = db.all_words(conn)  # no lang arg -> defaults to "ja"
    ja_scope = db.all_words(conn, lang="ja")
    zh_scope = db.all_words(conn, lang="zh-TW")

    assert set(default_scope) == {"猫"} == set(ja_scope)  # default == explicit "ja" (invariance)
    assert default_scope["猫"]["id"] == ja_scope["猫"]["id"] == wid_ja
    assert set(zh_scope) == {"猫"}
    assert zh_scope["猫"]["id"] == wid_zh
    assert ja_scope["猫"]["gloss"] == "cat"
    assert zh_scope["猫"]["gloss"] == "cat (zh)"  # neither scope leaks into the other


def test_set_status_scoped_by_lang_leaves_other_lang_row_untouched(conn):
    db.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun", "e01.ja.srt")
    db.upsert_word(conn, "猫", None, None, "cat (zh)", "noun", "e01.zh.srt", lang="zh-TW")

    rowcount = db.set_status(conn, "猫", "known", lang="ja")

    assert rowcount == 1
    assert db.all_words(conn, lang="ja")["猫"]["status"] == "known"
    assert db.all_words(conn, lang="zh-TW")["猫"]["status"] == "learning"  # untouched


def test_set_note_scoped_by_lang_leaves_other_lang_row_untouched(conn):
    db.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun", "e01.ja.srt")
    db.upsert_word(conn, "猫", None, None, "cat (zh)", "noun", "e01.zh.srt", lang="zh-TW")

    rowcount = db.set_note(conn, "猫", "override note", lang="ja")

    assert rowcount == 1
    assert db.all_words(conn, lang="ja")["猫"]["note"] == "override note"
    assert db.all_words(conn, lang="zh-TW")["猫"]["note"] is None  # untouched
