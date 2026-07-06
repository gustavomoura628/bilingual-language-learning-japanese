"""SQLite word database."""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime

DEFAULT_DB = os.environ.get(
    "BLL_DB",
    os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
        "bll",
        "bll.db",
    ),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
    id INTEGER PRIMARY KEY,
    lemma TEXT UNIQUE NOT NULL,
    reading TEXT,
    romaji TEXT,
    gloss TEXT,
    note TEXT,  -- operator override for the translator-note overlay; NULL = use
                -- the auto-generated "{romaji} = {gloss}" default
    pos TEXT,
    status TEXT NOT NULL DEFAULT 'learning',  -- learning | known | ignored
    exposures INTEGER NOT NULL DEFAULT 0,
    -- forgetting clock: cumulative JA source-tokens of content watched (across
    -- everything, in order) at this word's last injection. Content-based,
    -- NOT injection-based, so staleness is independent of vocabulary size.
    last_seen_pos INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT,                 -- episode where the word was introduced
    learned_at TEXT,                 -- timestamp it consolidated (exposures>=threshold)
    learned_at_episode TEXT,         -- episode where it consolidated ("learned")
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    new_words INTEGER NOT NULL DEFAULT 0,
    replacements INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,  -- JA tokens this file adds to the clock
    show TEXT,                          -- series this episode belongs to
    episode_no TEXT                     -- episode number within the show (text: "12.5", "OVA")
);
CREATE TABLE IF NOT EXISTS sightings (
    word_id INTEGER NOT NULL REFERENCES words(id),
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    occurrences INTEGER NOT NULL DEFAULT 0,
    replacements INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (word_id, episode_id)
);
CREATE TABLE IF NOT EXISTS variants (
    word_id INTEGER NOT NULL REFERENCES words(id),
    en_word TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (word_id, en_word)
);
CREATE TABLE IF NOT EXISTS align_cache (
    ja_line TEXT NOT NULL,
    en_text TEXT NOT NULL,
    lemma TEXT NOT NULL,
    en_word TEXT,  -- NULL = confirmed no-match
    PRIMARY KEY (ja_line, en_text, lemma)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created (SQLite CREATE
    IF NOT EXISTS won't alter existing tables)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(words)")}
    if "last_seen_pos" not in have:
        conn.execute("ALTER TABLE words ADD COLUMN last_seen_pos INTEGER NOT NULL DEFAULT 0")
    if "note" not in have:
        conn.execute("ALTER TABLE words ADD COLUMN note TEXT")
    if "learned_at" not in have:
        conn.execute("ALTER TABLE words ADD COLUMN learned_at TEXT")
        conn.execute("ALTER TABLE words ADD COLUMN learned_at_episode TEXT")
    have = {r[1] for r in conn.execute("PRAGMA table_info(episodes)")}
    if "tokens" not in have:
        conn.execute("ALTER TABLE episodes ADD COLUMN tokens INTEGER NOT NULL DEFAULT 0")
    if "show" not in have:
        conn.execute("ALTER TABLE episodes ADD COLUMN show TEXT")
        conn.execute("ALTER TABLE episodes ADD COLUMN episode_no TEXT")
    # Backfill on a data-condition (idempotent): also recovers a DB whose columns
    # exist but were never populated. Cheap no-op once everything is stamped.
    if conn.execute("SELECT 1 FROM episodes WHERE episode_no IS NULL LIMIT 1").fetchone():
        _backfill_episode_meta(conn)
    if conn.execute(
        "SELECT 1 FROM words WHERE learned_at IS NULL AND exposures>=10 LIMIT 1"
    ).fetchone():
        _backfill_learned(conn)


def _backfill_episode_meta(conn: sqlite3.Connection) -> None:
    """Parse an episode number out of each episode's filename that doesn't have
    one yet (e.g. e04.ja.srt -> "04"). Show is left for the operator to set."""
    for ep in conn.execute("SELECT id, name FROM episodes WHERE episode_no IS NULL"):
        m = re.search(r"(?:e|ep|episode|\bx)\s*0*(\d+)", ep["name"], re.I) or re.search(
            r"\b0*(\d{1,3})\b", ep["name"]
        )
        if m:
            conn.execute("UPDATE episodes SET episode_no=? WHERE id=?", (m.group(1), ep["id"]))


def _backfill_learned(conn: sqlite3.Connection, threshold: int = 10) -> None:
    """One-time: replay the sightings history in episode order and stamp each
    word's learned_at at the episode where its cumulative injections first
    reached the (default) learning threshold."""
    eps = list(conn.execute("SELECT id, name, processed_at FROM episodes ORDER BY id"))
    for w in conn.execute("SELECT id FROM words"):
        cum = 0
        for ep in eps:
            s = conn.execute(
                "SELECT replacements FROM sightings WHERE word_id=? AND episode_id=?",
                (w["id"], ep["id"]),
            ).fetchone()
            if s:
                cum += s["replacements"]
            if cum >= threshold:
                conn.execute(
                    "UPDATE words SET learned_at=?, learned_at_episode=? WHERE id=?",
                    (ep["processed_at"], ep["name"], w["id"]),
                )
                break


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or DEFAULT_DB
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()  # persist schema migrations + one-time backfills
    return conn


def clock(conn: sqlite3.Connection) -> int:
    """Current global forgetting clock: total JA tokens of content processed
    so far (== position before the next file is added)."""
    return conn.execute("SELECT COALESCE(SUM(tokens),0) FROM episodes").fetchone()[0]


def backup(path: str | None = None, keep: int = 10) -> str | None:
    """Snapshot the DB file before a mutating run (the DB holds the user's
    learning history). Rotates the `keep` most recent snapshots
    in a backups/ dir next to the DB. No-op if the DB doesn't exist yet."""
    import shutil

    path = path or DEFAULT_DB
    if not os.path.exists(path):
        return None
    bdir = os.path.join(os.path.dirname(os.path.abspath(path)), "backups")
    os.makedirs(bdir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(bdir, f"{os.path.basename(path)}.{ts}")
    shutil.copy2(path, dest)
    snaps = sorted(f for f in os.listdir(bdir) if f.startswith(os.path.basename(path) + "."))
    for old in snaps[:-keep]:
        os.remove(os.path.join(bdir, old))
    return dest


def all_words(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """lemma -> row for every word in the DB."""
    return {r["lemma"]: r for r in conn.execute("SELECT * FROM words")}


def add_episode(
    conn: sqlite3.Connection,
    name: str,
    new_words: int,
    replacements: int,
    tokens: int = 0,
    show: str | None = None,
    episode_no: str | None = None,
) -> int | None:
    cur = conn.execute(
        "INSERT INTO episodes (name, processed_at, new_words, replacements, "
        "tokens, show, episode_no) VALUES (?,?,?,?,?,?,?)",
        (
            name,
            datetime.now().isoformat(timespec="seconds"),
            new_words,
            replacements,
            tokens,
            show,
            episode_no,
        ),
    )
    return cur.lastrowid


def stamp_learned(
    conn: sqlite3.Connection, word_id: int, episode_name: str, threshold: int
) -> None:
    """Record when a word consolidates: the first time its lifetime exposures
    reach the learning threshold, stamp the episode it happened in. Idempotent -
    only stamps once (learned_at stays NULL until then)."""
    row = conn.execute("SELECT exposures, learned_at FROM words WHERE id=?", (word_id,)).fetchone()
    if row and row["learned_at"] is None and row["exposures"] >= threshold:
        conn.execute(
            "UPDATE words SET learned_at=?, learned_at_episode=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), episode_name, word_id),
        )


def touch_last_seen(conn: sqlite3.Connection, word_id: int, pos: int) -> None:
    """Mark a word as last injected at clock position `pos` (recency tracking)."""
    conn.execute("UPDATE words SET last_seen_pos=? WHERE id=?", (pos, word_id))


def upsert_word(
    conn: sqlite3.Connection,
    lemma: str,
    reading: str | None,
    romaji: str | None,
    gloss: str | None,
    pos: str | None,
    episode_name: str,
) -> int | None:
    row = conn.execute("SELECT id FROM words WHERE lemma=?", (lemma,)).fetchone()
    if row:
        if gloss:  # latest canonical gloss wins (sense-rotation may improve it)
            conn.execute("UPDATE words SET gloss=? WHERE id=?", (gloss, row["id"]))
        return row["id"]
    cur = conn.execute(
        "INSERT INTO words (lemma, reading, romaji, gloss, pos, first_seen, added_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            lemma,
            reading,
            romaji,
            gloss,
            pos,
            episode_name,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    return cur.lastrowid


def record_sighting(
    conn: sqlite3.Connection, word_id: int, episode_id: int, occurrences: int, replacements: int
) -> None:
    conn.execute(
        "INSERT INTO sightings (word_id, episode_id, occurrences, replacements) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(word_id, episode_id) DO UPDATE SET "
        "occurrences=occurrences+excluded.occurrences, "
        "replacements=replacements+excluded.replacements",
        (word_id, episode_id, occurrences, replacements),
    )
    conn.execute(
        "UPDATE words SET exposures = exposures + ? WHERE id=?",
        (replacements, word_id),
    )


def record_variant(conn: sqlite3.Connection, word_id: int, en_word: str) -> None:
    """Remember which EN word an injection replaced (variant history)."""
    conn.execute(
        "INSERT INTO variants (word_id, en_word, count) VALUES (?,?,1) "
        "ON CONFLICT(word_id, en_word) DO UPDATE SET count=count+1",
        (word_id, en_word),
    )


def word_variants(conn: sqlite3.Connection, word_id: int) -> dict[str, int]:
    """en_word -> count from the variant history."""
    return {
        r["en_word"]: r["count"]
        for r in conn.execute("SELECT en_word, count FROM variants WHERE word_id=?", (word_id,))
    }


def cache_get(
    conn: sqlite3.Connection, ja_line: str, en_text: str, lemma: str
) -> sqlite3.Row | None:
    """Returns the cached row (en_word may be NULL = confirmed
    no-match) or None if this triple was never judged."""
    return conn.execute(
        "SELECT en_word FROM align_cache WHERE ja_line=? AND en_text=? AND lemma=?",
        (ja_line, en_text, lemma),
    ).fetchone()


def cache_put(
    conn: sqlite3.Connection, ja_line: str, en_text: str, lemma: str, en_word: str | None
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO align_cache (ja_line, en_text, lemma, en_word) VALUES (?,?,?,?)",
        (ja_line, en_text, lemma, en_word),
    )


def set_status(conn: sqlite3.Connection, lemma: str, status: str) -> int:
    cur = conn.execute("UPDATE words SET status=? WHERE lemma=?", (status, lemma))
    return cur.rowcount


def set_note(conn: sqlite3.Connection, lemma: str, note: str | None) -> int:
    """Set (or clear, with note=None) the operator's translator-note override
    for a word. Returns rowcount (0 = lemma not in DB)."""
    cur = conn.execute("UPDATE words SET note=? WHERE lemma=?", (note, lemma))
    return cur.rowcount
