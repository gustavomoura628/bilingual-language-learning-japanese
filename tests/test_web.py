"""TestClient coverage for the /watch page and its supporting endpoints (issue #18):
GET /watch (AT-001), GET /api/video Range handling (AT-002), GET /api/vtt SRT->VTT
conversion (AT-003), and a regression check of the existing /api/words lang path (AT-004).

Run: pytest tests/test_web.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient

from bll import db as dbm
from bll import web

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
JA_SUB = os.path.join(FIXTURES, "promise_secret.ja.srt")

client = TestClient(web.app)


def test_watch_page_returns_key_dom_markers():
    r = client.get("/watch")
    assert r.status_code == 200
    body = r.text
    assert "<video" in body and 'id="player"' in body
    assert 'id="words-panel"' in body


def test_video_range_returns_206_with_content_range(tmp_path):
    video_path = tmp_path / "sample.mp4"
    payload = bytes(range(256)) * 40  # 10240 deterministic bytes
    video_path.write_bytes(payload)

    r = client.get(f"/api/video?path={video_path}", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 100-199/{len(payload)}"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content == payload[100:200]


def test_video_no_range_returns_200_full_body(tmp_path):
    video_path = tmp_path / "sample.mp4"
    payload = b"x" * 4096
    video_path.write_bytes(payload)

    r = client.get(f"/api/video?path={video_path}")
    assert r.status_code == 200
    assert r.content == payload


def test_video_invalid_range_returns_416(tmp_path):
    video_path = tmp_path / "sample.mp4"
    video_path.write_bytes(b"x" * 100)

    r = client.get(f"/api/video?path={video_path}", headers={"Range": "bytes=500-600"})
    assert r.status_code == 416


def test_srt_to_vtt_conversion():
    r = client.get(f"/api/vtt?path={JA_SUB}")
    assert r.status_code == 200
    assert r.text.startswith("WEBVTT")
    assert "-->" in r.text
    assert "今日は特別な約束がある。" in r.text  # cue text survives conversion
    assert "00:00:01.000" in r.text  # SRT's comma separator becomes VTT's period


def test_learned_words_endpoint_lang_param(tmp_path, monkeypatch):
    db_path = str(tmp_path / "vocab.db")
    conn = dbm.connect(db_path)
    dbm.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun", "e01.ja.srt")
    conn.commit()
    monkeypatch.setattr(web.STATE, "active_db", db_path)

    r = client.get("/api/words?lang=ja")
    assert r.status_code == 200
    assert "猫" in [w["lemma"] for w in r.json()["words"]]
