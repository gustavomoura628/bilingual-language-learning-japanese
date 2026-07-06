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
