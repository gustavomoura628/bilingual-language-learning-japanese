"""Behavioral test coverage for bll/timeline.py's git-style branching model
over a campaign DB (issue #8, AT7-AT10): init()/record() head-snapshotting,
branch() fork-and-switch with source-branch survival, switch()'s three paths
(including a white-box branch-without-snapshot case), and state()'s
per-branch summary.

The `campaign_db` fixture below is a real on-disk file built exclusively
through bll/db.py's own public API (db.connect) -- timeline.py only reads
the `episodes` table's row count and each row's name/id ordering, so a full
db.connect() is exactly enough surface and no more, never a hand-rolled
schema. Every test advances it via the _add_episode() helper, which must
commit + close its own connection -- see its docstring for why that is
load-bearing, not stylistic: db.add_episode() itself never commits, and
every timeline.py read opens an INDEPENDENT sqlite3.connect() to the same
file, so a second connection only ever sees committed data.

Run: pytest tests/test_timeline.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bll import db, timeline


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
    # Fresh, 0-episode campaign_db: no episode, so no snapshot could ever
    # exist at position 1; init()'s own auto-snapshot-current-head check is
    # `if pos and ...` (falsy at pos=0), so it can't accidentally create one
    # either. Minimal setup that reaches the guard clause.
    with pytest.raises(ValueError, match="no snapshot"):
        timeline.branch(campaign_db, 1, "nope")


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


def test_switch_to_branch_without_snapshot_raises_value_error(campaign_db):
    # The third failure mode -- a branch that exists in the registry but has
    # no snapshot -- is not reachable through init/record/branch/switch
    # alone, because branch()'s last step always snapshots the branch it
    # just created. Reaching this state requires directly writing the
    # registry JSON.
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
