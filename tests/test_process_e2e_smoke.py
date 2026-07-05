"""End-to-end smoke test: the real tokenize -> select -> align -> render
pipeline against a committed, fully-fabricated synthetic JA/EN subtitle pair
(tests/fixtures/promise_secret.{ja,en}.srt - a handful of invented cues, not
sourced from any real show).

Full offline determinism, by construction rather than by mocking a library
call:
  --no-dict     skips jmdict.load_merged() (the dictionary-veto path itself
                is therefore NOT exercised here - it needs either live
                network or a separate committed dictionary fixture, tracked
                as a separate, larger effort).
  --gloss-json  makes cmd_process take the `aligned = glossm.parse_result(...)`
                branch directly (see bll/cli.py cmd_process), which skips the
                whole `else` branch: the ollama-reachability gate AND
                gloss_and_align() itself never run, so there is no Ollama
                call and no Claude CLI spawn to mock in the first place.
A socket/subprocess guard below proves this, rather than merely hoping the
sandbox has no network: it fails the test loudly if either is attempted.

The gloss-json payload is not guessed: it was authored against ground truth
from an exploratory run of jp.analyze()/jp.select_new() over this exact
fixture text, which is what determines the candidate lemmas and the
occurrence ids cmd_process assigns to them. With --no-dict (jm=None), only
約束 "promise" (4 occurrences, cues 0/1/2/5) and 秘密 "secret" (2
occurrences, cues 3/4) clear the default min_count=2 / min_zipf=3.0 /
--include-pos noun,adj gates; occurrence ids are assigned in that order
(1-4, then 5-6). See gloss.parse_result / the shape documented in
gloss.PROMPT_HEADER for the JSON contract.

Run: pytest tests/test_process_e2e_smoke.py
"""
import json
import os
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bll import cli

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
JA_SUB = os.path.join(FIXTURES, "promise_secret.ja.srt")
EN_SUB = os.path.join(FIXTURES, "promise_secret.en.srt")

GLOSS_JSON = {
    "words": [
        {"lemma": "約束", "gloss": "promise", "reading": "やくそく", "skip": False,
         "matches": [{"id": 1, "en_word": "promise"},
                     {"id": 2, "en_word": "promise"},
                     {"id": 3, "en_word": "promise"},
                     {"id": 4, "en_word": "promise"}]},
        {"lemma": "秘密", "gloss": "secret", "reading": "ひみつ", "skip": False,
         "matches": [{"id": 5, "en_word": "secret"},
                     {"id": 6, "en_word": "secret"}]},
    ]
}


def block_network(monkeypatch):
    """Fail loudly (rather than silently succeed by sandbox happenstance) if
    this test ever opens a socket or spawns the Claude CLI."""
    def blocked(*a, **k):
        raise AssertionError("network access attempted during offline e2e test")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)

    real_run = subprocess.run

    def guarded_run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and cmd and "claude" in str(cmd[0]):
            raise AssertionError("Claude CLI spawn attempted during offline e2e test")
        return real_run(cmd, *a, **k)
    monkeypatch.setattr(subprocess, "run", guarded_run)


def run_process(tmp_path, run_name, gloss_json_path):
    """One clean `bll process` run: fresh --db, fresh output dir (matches the
    existing tests' fresh-tmp_path/--db pattern). Returns the four rendered
    layer paths and the plan sidecar path."""
    out_dir = tmp_path / run_name
    out_dir.mkdir()
    out_path = out_dir / "out.srt"
    rc = cli.main([
        "--db", str(out_dir / "vocab.db"), "process", JA_SUB, EN_SUB,
        "-o", str(out_path), "--no-dict", "--gloss-json", str(gloss_json_path),
    ])
    assert rc == 0
    layers = {
        "adaptive": out_dir / "out.srt",
        "plain": out_dir / "out.plain.srt",
        "kana": out_dir / "out.kana.srt",
        "answers": out_dir / "out.answers.srt",
    }
    plan = out_dir / "out.plan.json"
    return layers, plan


def test_process_e2e_smoke_fixture(tmp_path, monkeypatch):
    block_network(monkeypatch)
    gloss_json_path = tmp_path / "gloss.json"
    gloss_json_path.write_text(json.dumps(GLOSS_JSON), encoding="utf-8")

    layers, plan = run_process(tmp_path, "run1", gloss_json_path)

    # All four rendered layers + the plan sidecar are produced.
    for name, path in layers.items():
        assert path.exists(), f"{name} layer not produced: {path}"
    assert plan.exists(), "plan.json sidecar not produced"

    # At least one specific, named word from the fixture is actually
    # injected -- not merely that output files exist. 約束 replaces
    # "promise" in EN cue 0 ("Today we have a special promise.").
    plain_text = layers["plain"].read_text(encoding="utf-8")
    assert "約束" in plain_text
    assert "promise" not in plain_text.lower()  # fully replaced, bare layer
    assert "secret" not in plain_text.lower()

    kana_text = layers["kana"].read_text(encoding="utf-8")
    assert "約束 (やくそく)" in kana_text  # kana layer always annotates
    assert "秘密 (ひみつ)" in kana_text

    # The plan sidecar records the injections precisely (cue, lemma, en_word).
    plan_data = json.loads(plan.read_text(encoding="utf-8"))
    injections = plan_data["injections"]
    assert len(injections) == 6  # one per fixture cue: 4x約束 + 2x秘密
    assert any(inj["lemma"] == "約束" and inj["cue"] == 0
              and inj["en_word"] == "promise" for inj in injections)
    assert any(inj["lemma"] == "秘密" and inj["cue"] == 3
              and inj["en_word"] == "secret" for inj in injections)


def test_process_e2e_two_clean_runs_byte_identical(tmp_path, monkeypatch):
    block_network(monkeypatch)
    gloss_json_path = tmp_path / "gloss.json"
    gloss_json_path.write_text(json.dumps(GLOSS_JSON), encoding="utf-8")

    layers1, plan1 = run_process(tmp_path, "run1", gloss_json_path)
    layers2, plan2 = run_process(tmp_path, "run2", gloss_json_path)

    for name in layers1:
        assert layers1[name].read_bytes() == layers2[name].read_bytes(), \
            f"{name} layer differs between two clean runs"
    assert plan1.read_bytes() == plan2.read_bytes(), \
        "plan.json sidecar differs between two clean runs"
