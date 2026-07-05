"""Regression test: the backend gate must STOP when the local (ollama) path is
selected but unreachable -- it must never silently fall through to the paid
`claude` backend.

`test_backend_default.py` locks the default *values*; this locks the gate
*behavior*. A future refactor that drops the reachability pre-flight, or routes
an unreachable-ollama run to claude, fails here -- which is the exact regression
class of the original bug.

Run: pytest tests/test_backend_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bll import bootstrap, cli, gloss

JA = ("1\n00:00:01,000 --> 00:00:03,000\n世界を変える約束だ。\n\n"
      "2\n00:00:04,000 --> 00:00:06,000\nその約束は世界の希望だ。\n")
EN = ("1\n00:00:01,000 --> 00:00:03,000\nA promise to change the world.\n\n"
      "2\n00:00:04,000 --> 00:00:06,000\nThat promise is the world's hope.\n")


def test_backend_gate_stops_on_unreachable_ollama(monkeypatch, tmp_path):
    # Simulate "ollama is not running", and trap any call into the aligner so
    # we can prove the run stopped at the gate before reaching either backend.
    monkeypatch.setattr(bootstrap, "reachable", lambda *a, **k: False)
    align_calls = []
    monkeypatch.setattr(
        gloss, "gloss_and_align",
        lambda *a, **k: align_calls.append(k.get("backend", "?")) or {},
    )

    ja, en = tmp_path / "ep.ja.srt", tmp_path / "ep.en.srt"
    ja.write_text(JA)
    en.write_text(EN)

    # No --backend -> defaults to ollama; ollama is "unreachable" (patched above).
    rc = cli.main([
        "--db", str(tmp_path / "t.db"), "process", str(ja), str(en),
        "-o", str(tmp_path / "o.ass"), "--no-dict", "--min-count", "1",
    ])

    assert rc == 1               # no-backend run stops when ollama is unreachable
    assert align_calls == []     # aligner never invoked on the default path
