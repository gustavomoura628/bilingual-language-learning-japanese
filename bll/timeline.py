"""Branching timeline for a campaign DB (git-style).

A campaign's SQLite file always holds the ACTIVE branch's linear episode history.
This module keeps a sidecar registry next to it, in `<db>.timeline/`, that the
campaign DB never sees, so it survives branch swaps. The registry tracks:

  - branches: name, parent, the position they forked at, creation time
  - snapshots: a copy of the campaign DB taken after each episode, tagged with
    (branch, position) - the per-episode save-states that make seeking possible

Operations swap the campaign file by copying a snapshot over it:
  - branch(position): rewind to an earlier episode as a NEW branch and continue
  - switch(branch):   make another branch active
Both keep every branch's state intact (a "keep both" model).

Snapshots are taken going forward (after each process). For a campaign processed
before the timeline existed, init() imports any labeled run-batch backups
(`*.pre-eNN.db` / `*.post-eNN.db`) so earlier positions are seekable too.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime
from typing import Any


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def tdir(db: str) -> str:
    return os.path.abspath(db) + ".timeline"


def _snaps_dir(db: str) -> str:
    return os.path.join(tdir(db), "snaps")


def _registry_path(db: str) -> str:
    return os.path.join(tdir(db), "registry.json")


def _episode_count(db: str) -> int:
    if not os.path.exists(db):
        return 0
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        c.close()


def _last_episode_name(db: str) -> str | None:
    c = sqlite3.connect(db)
    try:
        r = c.execute("SELECT name FROM episodes ORDER BY id DESC LIMIT 1").fetchone()
        return r[0] if r else None
    except sqlite3.OperationalError:
        return None
    finally:
        c.close()


def _load(db: str) -> dict[str, Any]:
    p = _registry_path(db)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {
        "current": "main",
        "seq": 0,
        "branches": {"main": {"name": "main", "parent": None, "fork_pos": 0, "created": _now()}},
        "snaps": [],
    }  # each: {id, branch, position, episode, file, created}


def _save(db: str, reg: dict[str, Any]) -> None:
    os.makedirs(_snaps_dir(db), exist_ok=True)
    with open(_registry_path(db), "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=1)


def _take(
    db: str,
    reg: dict[str, Any],
    branch: str,
    position: int,
    episode: str | None,
    label: str | None = None,
) -> dict[str, Any]:
    """Copy the campaign DB into the snaps dir and append a registry record."""
    reg["seq"] += 1
    sid = reg["seq"]
    fname = f"snap-{sid:04d}.db"
    os.makedirs(_snaps_dir(db), exist_ok=True)
    shutil.copy2(db, os.path.join(_snaps_dir(db), fname))
    rec = {
        "id": sid,
        "branch": branch,
        "position": position,
        "episode": episode,
        "file": fname,
        "label": label,
        "created": _now(),
    }
    reg["snaps"].append(rec)
    return rec


def _import_backups(db: str, reg: dict[str, Any]) -> None:
    """Best-effort: register existing labeled run-batch backups as `main`
    snapshots so a pre-timeline campaign is still seekable. pre-eNN = state
    after episode N-1; post-eNN = state after episode N."""
    bdir = os.path.join(os.path.dirname(os.path.abspath(db)), "backups")
    stem = os.path.splitext(os.path.basename(db))[0]  # campaign.db -> "campaign"
    have = {(s["branch"], s["position"]) for s in reg["snaps"]}
    for f in sorted(
        glob.glob(os.path.join(bdir, stem + ".pre-e*.db"))
        + glob.glob(os.path.join(bdir, stem + ".post-e*.db"))
    ):
        m = re.search(r"\.(pre|post)-e(\d+)\.db$", f)
        if not m:
            continue
        pos = int(m.group(2)) - (1 if m.group(1) == "pre" else 0)
        if pos <= 0 or ("main", pos) in have:
            continue
        reg["seq"] += 1
        fname = f"snap-{reg['seq']:04d}.db"
        shutil.copy2(f, os.path.join(_snaps_dir(db), fname))
        reg["snaps"].append(
            {
                "id": reg["seq"],
                "branch": "main",
                "position": pos,
                "episode": None,
                "file": fname,
                "label": f"imported {os.path.basename(f)}",
                "created": _now(),
            }
        )
        have.add(("main", pos))


def init(db: str) -> dict[str, Any]:
    """Ensure a registry exists and the current head is snapshotted. Idempotent."""
    if not os.path.exists(db):
        return _load(db)
    reg = _load(db)
    fresh = not os.path.exists(_registry_path(db))
    if fresh:
        os.makedirs(_snaps_dir(db), exist_ok=True)
        _import_backups(db, reg)
    pos = _episode_count(db)
    cur = reg["current"]
    if pos and not any(s["branch"] == cur and s["position"] == pos for s in reg["snaps"]):
        _take(db, reg, cur, pos, _last_episode_name(db), label="head")
    _save(db, reg)
    return reg


def record(db: str) -> dict[str, Any]:
    """Snapshot the active branch's new head - call after each process."""
    reg = init(db)
    pos = _episode_count(db)
    cur = reg["current"]
    if not any(s["branch"] == cur and s["position"] == pos for s in reg["snaps"]):
        _take(db, reg, cur, pos, _last_episode_name(db))
        _save(db, reg)
    return reg


def _snap_at(reg: dict[str, Any], branch: str, position: int) -> dict[str, Any] | None:
    cand = [s for s in reg["snaps"] if s["branch"] == branch and s["position"] == position]
    return cand[-1] if cand else None


def snapshot_path(db: str, position: int | str) -> str | None:
    """Absolute path of the current branch's snapshot at `position` (the DB state
    as of that episode), or None. Used for read-only seek previews."""
    reg = init(db)
    s = _snap_at(reg, reg["current"], int(position))
    return os.path.join(_snaps_dir(db), s["file"]) if s else None


def _head_snap(reg: dict[str, Any], branch: str) -> dict[str, Any] | None:
    cand = [s for s in reg["snaps"] if s["branch"] == branch]
    return max(cand, key=lambda s: s["position"]) if cand else None


def state(db: str) -> dict[str, Any]:
    """Summary for the UI: branches (with head position + fork point) and which
    timeline positions on the current branch have a snapshot (are seekable)."""
    reg = init(db)
    cur = reg["current"]
    branches: list[dict[str, Any]] = []
    for bid, b in reg["branches"].items():
        head = _head_snap(reg, bid)
        branches.append(
            {
                "id": bid,
                "name": b["name"],
                "parent": b["parent"],
                "fork_pos": b["fork_pos"],
                "head_pos": head["position"] if head else b["fork_pos"],
                "created": b["created"],
                "active": bid == cur,
            }
        )
    seekable = sorted({s["position"] for s in reg["snaps"] if s["branch"] == cur})
    return {"current": cur, "branches": branches, "seekable": seekable}


def branch(db: str, position: int, name: str) -> str:
    """Rewind to `position` on the current branch as a NEW branch and make it
    active. Requires a snapshot at that position. Returns the new branch id."""
    reg = init(db)
    cur = reg["current"]
    snap = _snap_at(reg, cur, position)
    if not snap:
        raise ValueError(f"no snapshot at episode #{position} to branch from")
    bid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or f"branch-{reg['seq'] + 1}"
    if bid in reg["branches"]:
        bid = f"{bid}-{reg['seq'] + 1}"
    # snapshot current head first so nothing is lost, then swap the working DB
    record(db)
    shutil.copy2(os.path.join(_snaps_dir(db), snap["file"]), db)
    reg = _load(db)
    reg["branches"][bid] = {"name": name, "parent": cur, "fork_pos": position, "created": _now()}
    reg["current"] = bid
    _take(db, reg, bid, position, snap["episode"], label="branch base")
    _save(db, reg)
    return bid


def switch(db: str, branch_id: str) -> dict[str, Any]:
    """Make another branch active (snapshots the current head first)."""
    reg = init(db)
    if branch_id not in reg["branches"]:
        raise ValueError(f"no such branch: {branch_id}")
    if branch_id == reg["current"]:
        return reg
    record(db)
    head = _head_snap(reg, branch_id)
    if not head:
        raise ValueError(f"branch {branch_id} has no snapshot")
    reg = _load(db)
    shutil.copy2(os.path.join(_snaps_dir(db), head["file"]), db)
    reg["current"] = branch_id
    _save(db, reg)
    return reg
