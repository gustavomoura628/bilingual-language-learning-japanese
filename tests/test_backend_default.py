"""Regression test: the alignment backend must never silently default to the
paid `claude` path.

`bll process` with no `--backend` used to default to `claude`, which shells out
to the Claude CLI and spends the user's tokens with no warning. Both the CLI
argument and the `gloss_and_align()` signature now default to the local, no-cost
`ollama` path; `claude` is an explicit, disclaimed opt-in (see the backend gate
in `cli.cmd_process`).

Run: pytest tests/test_backend_default.py
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bll import cli, gloss


def test_cli_process_defaults_backend_to_ollama(monkeypatch):
    # The CLI `process` command must default --backend to ollama. This is the
    # active bug: it defaulted to claude, so a bare `bll process` spent tokens.
    # Patch the dispatch target (bound via set_defaults when main() builds the
    # parser) to capture the resolved args without running a real process.
    captured = {}

    def _fake_process(args):
        captured["backend"] = args.backend
        return 0

    monkeypatch.setattr(cli, "cmd_process", _fake_process)
    cli.main(["process", "a.ja.srt", "b.en.srt"])
    assert captured.get("backend") == "ollama"


def test_gloss_and_align_defaults_backend_to_ollama():
    # The library entry point must not silently default to claude either --
    # defense in depth for any caller that omits the backend.
    sig = inspect.signature(gloss.gloss_and_align)
    assert sig.parameters["backend"].default == "ollama"
