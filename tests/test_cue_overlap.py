"""Unit coverage for bll.cli.overlapping() -- the helper that aligns JA/EN
subtitle cues by time overlap, falling back to the nearest EN event within
a slack window when none overlap directly (issue #11, deferred out of #8's
timeline.py-focused suite since this helper lives in the CLI module).

Covers: overlapping-cue match (single + multiple), non-overlapping-but-
within-slack fallback, outside-slack no-match, tie-breaking between
equidistant candidates, and the slack/slack+1 inclusive boundary.

Fully offline: en_events are built from a local SimpleNamespace stand-in,
no pysubs2 objects, no file I/O, no network.

Run: pytest tests/test_cue_overlap.py
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bll import cli


def ev(start, end):
    return SimpleNamespace(start=start, end=end)


def test_overlapping_cue_match():
    # JA window (1500, 1800) sits fully inside event 0's (1000, 2000); event
    # 1 (5000, 6000) is far away and not a candidate at all.
    en_events = [ev(1000, 2000), ev(5000, 6000)]
    assert cli.overlapping(en_events, 1500, 1800) == [0]


def test_fallback_within_slack():
    # No overlap: max(1000, 2100)=2100 is not < min(2000, 2300)=2000.
    # gap = max(1000 - 2300, 2100 - 2000) = max(-1300, 100) = 100 <= default
    # slack (1200), so the fallback branch returns the nearest event.
    en_events = [ev(1000, 2000)]
    assert cli.overlapping(en_events, 2100, 2300) == [0]


def test_outside_slack_no_match():
    # gap = max(1000 - 4500, 4000 - 2000) = max(-3500, 2000) = 2000, which
    # exceeds the default slack (1200) -- no candidate qualifies.
    en_events = [ev(1000, 2000)]
    assert cli.overlapping(en_events, 4000, 4500) == []


def test_tie_break_first_index_wins():
    # Neither event overlaps (2400, 2600) directly. Both gaps are exactly
    # 400: event 0 -> max(1000-2600, 2400-2000)=400; event 1 ->
    # max(3000-2600, 2400-4000)=400. The fallback loop's update condition is
    # a strict `gap < best_gap`, so on an exact tie the later candidate's
    # equal gap never overwrites the earlier one -- the first-encountered
    # (lowest index) event wins.
    en_events = [ev(1000, 2000), ev(3000, 4000)]
    assert cli.overlapping(en_events, 2400, 2600) == [0]


def test_multiple_simultaneous_overlaps():
    # Both events genuinely overlap the JA window (1400, 2100), so the
    # `hits` branch returns every overlapping index, not just one.
    en_events = [ev(1000, 2000), ev(1500, 2500)]
    assert cli.overlapping(en_events, 1400, 2100) == [0, 1]


def test_slack_boundary_inclusive_vs_exclusive():
    # gap == slack (1200) still matches: `best_gap` is initialised to
    # `slack + 1`, and the comparison is `gap < best_gap`, so exactly-slack
    # is the inclusive upper edge.
    en_events = [ev(1000, 2000)]
    assert cli.overlapping(en_events, 3200, 3300) == [0]
    # One ms further out (gap == slack + 1 == 1201) no longer matches.
    assert cli.overlapping(en_events, 3201, 3301) == []
