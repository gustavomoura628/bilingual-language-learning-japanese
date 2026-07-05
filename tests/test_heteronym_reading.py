"""Regression tests for heteronym per-injection readings.

Heteronyms share one surface form but have multiple readings depending on
sense (e.g. 角 = つの "horn" vs かど "corner/edge" vs かく "angle"). bll stores
ONE reading per lemma, so it could not flip the reading by context: a heteronym
injected over a given English word always showed the stored reading even when
the matched sense called for a different one. Fix: derive the reading
PER-INJECTION from the JMdict sense the aligned English word maps to
(jmdict.entry_for_en + cli.apply_sense_first), display only -- a strict-winner
rule that no-ops on ties / single-reading words.

Run: pytest tests/test_heteronym_reading.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bll import cli, jmdict

lk = jmdict.load()


def reading(lemma, en):
    e = jmdict.entry_for_en(lemma, lk.get(lemma), en)
    return e["r"][0] if e else None


def test_heteronym_sense_first_reading():
    # The core case: 角 reads つの over horn (and its plural), but かど/かく for
    # the other senses -- and ties / no-match return None so the caller keeps
    # the stored reading.
    assert reading("角", "horn") == "つの"
    assert reading("角", "horns") == "つの"        # plural via lemmatizer
    assert reading("角", "angle") == "かく"
    assert reading("角", "edge") == "かど"
    assert reading("角", "corner") is None          # かど/すみ tie -> keep fallback
    assert reading("方", "way") is None             # かた/ほう tie -> keep fallback
    assert reading("方", "person") == "かた"

    # The "horn" trap: entry かく has 'Chinese "horn" constellation' (quoted,
    # not parenthesised) so a naive token match would tie. Exact-gloss-match on
    # the つの entry must still win uniquely.
    assert reading("角", "horn") == "つの"


def test_apply_sense_first_plan_mutation():
    # apply_sense_first end-to-end: only flips genuine heteronyms whose
    # reading actually changes; everything else is a byte-for-byte no-op.
    plan = [
        {"cue": 0, "en_word": "horn", "lemma": "角", "reading": "かど",
         "exposure_before": 0, "recency_gap": 0},
        {"cue": 1, "en_word": "corner", "lemma": "角", "reading": "かど",
         "exposure_before": 0, "recency_gap": 0},
        {"cue": 2, "en_word": "today", "lemma": "今日", "reading": "きょう",
         "exposure_before": 0, "recency_gap": 0},
    ]
    changed = cli.apply_sense_first(plan, lk, conn=None, use_romaji=True)

    assert changed == 1
    assert plan[0]["reading"] == "つの"       # horn injection -> つの
    assert plan[0].get("note") == "角 (tsuno) = horn; antler"
    assert plan[1]["reading"] == "かど"       # corner injection unchanged
    assert plan[1].get("note") is None
    assert plan[2]["reading"] == "きょう"     # 今日 single-reading untouched
    assert plan[2].get("note") is None
