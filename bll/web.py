"""bll operator console - a local web UI that exposes the whole workflow.

Every *pipeline* action (process an episode, batch a season, render, bootstrap
the model, install next to the video) runs the real `bll` CLI as a streamed
subprocess, so the GUI has full parity with the command line - every knob,
per-episode and per-season, with no reimplementation. Read/curate actions
(vocabulary, notes, status, stats, campaigns) talk to the SQLite DB directly
for speed.

No GPU code here; the heavy alignment lives in ollama, reached over HTTP.
Run:  bll serve   (see cli.cmd_serve)
"""

import glob
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

import pysubs2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from . import bootstrap as bootstrapm
from . import db as dbm
from . import timeline as tl

STATIC = os.path.join(os.path.dirname(__file__), "static")
DATA_DIR = os.path.dirname(os.path.abspath(dbm.DEFAULT_DB))

# Argparse defaults, surfaced so the UI can prefill every form field. The GUI
# defaults backend to ollama (the local, no-API-key path the release ships).
DEFAULTS = {
    "new_words": 2,
    "max_active": 2,
    "learning_threshold": 10,
    "min_count": 2,
    "min_zipf": 3.0,
    "max_per_cue": 0,
    "kana_threshold": 15,
    "decay_tokens": 6000,
    "note_threshold": 5,
    "include_pos": "noun,adj",
    "backend": "ollama",
    "romaji": False,
    "no_dict": False,
}

# ---------------------------------------------------------------- app state


class State:
    def __init__(self):
        self.active_db = os.environ.get("BLL_DB", dbm.DEFAULT_DB)


STATE = State()


def conn():
    return dbm.connect(STATE.active_db)


def read_conn(at=None):
    """A connection for read-only views: the live campaign DB, or - when `at` is a
    timeline position - the snapshot of that episode (for seek previews)."""
    if at:
        p = tl.snapshot_path(STATE.active_db, at)
        if p and os.path.exists(p):
            return dbm.connect(p)
    return conn()


# ---------------------------------------------------------------- jobs


class Job:
    """A streamed subprocess run of the bll CLI."""

    _seq = 0
    _lock = threading.Lock()

    def __init__(self, label, argv, db=None):
        with Job._lock:
            Job._seq += 1
            self.id = f"job{Job._seq}"
        self.label = label
        self.argv = argv
        self.db = db or STATE.active_db
        self.lines = []
        self.status = "running"  # running | done | failed
        self.returncode = None
        self.started = datetime.now().isoformat(timespec="seconds")

    def to_meta(self):
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "returncode": self.returncode,
            "started": self.started,
            "nlines": len(self.lines),
        }


JOBS = {}


def _bll_argv(*args):
    """Invoke the very same bll this server runs under (PATH-independent)."""
    return [sys.executable, "-m", "bll.cli", *map(str, args)]


def run_job(label, argv, db=None, then=None):
    """Start a streamed subprocess job. `then` (optional) is a callable run
    after the process exits with the Job, for chaining (e.g. season loops)."""
    job = Job(label, argv, db=db)
    JOBS[job.id] = job

    def _worker():
        env = dict(os.environ, BLL_DB=job.db, PYTHONUNBUFFERED="1")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=os.getcwd(),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in proc.stdout:
                job.lines.append(line.rstrip("\n"))
            proc.wait()
            job.returncode = proc.returncode
            job.status = "done" if proc.returncode == 0 else "failed"
        except Exception as e:  # noqa: BLE001 - surface to the UI, never crash
            job.lines.append(f"[job error] {e}")
            job.status = "failed"
            job.returncode = -1
        if then:
            try:
                then(job)
            except Exception as e:  # noqa: BLE001
                job.lines.append(f"[chain error] {e}")

    threading.Thread(target=_worker, daemon=True).start()
    return job


# ---------------------------------------------------------------- API

app = FastAPI(title="bll operator console")


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/watch", response_class=HTMLResponse)
def watch():
    with open(os.path.join(STATIC, "watch.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/config")
def get_config():
    campaigns = []
    for p in sorted(
        glob.glob(os.path.join(DATA_DIR, "*.db")) + glob.glob(os.path.join(DATA_DIR, "*", "*.db"))
    ):
        if f"{os.sep}backups{os.sep}" in p:  # snapshots aren't campaigns
            continue
        campaigns.append({"path": p, "name": os.path.relpath(p, DATA_DIR)})
    return {
        "active_db": STATE.active_db,
        "data_dir": DATA_DIR,
        "campaigns": campaigns,
        "defaults": DEFAULTS,
        "cwd": os.getcwd(),
    }


@app.post("/api/config")
def set_config(body: dict):
    db = body.get("active_db")
    if db:
        os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
        STATE.active_db = db
    return get_config()


@app.get("/api/stats")
def stats(learning_threshold: int = DEFAULTS["learning_threshold"], at: int = None):
    c = read_conn(at)
    counts = dict(c.execute("SELECT status, COUNT(*) FROM words GROUP BY status"))
    # A word is "learned" once it consolidates out of the active slot - i.e. its
    # lifetime exposures reach the learning threshold (it no longer counts
    # against max-active). That, not the manual `known` flag, is graduation.
    learned = c.execute(
        "SELECT COUNT(*) FROM words WHERE status='learning' AND exposures>=?", (learning_threshold,)
    ).fetchone()[0]
    active = counts.get("learning", 0) - learned
    eps = [dict(r) for r in c.execute("SELECT * FROM episodes ORDER BY id DESC LIMIT 20")]
    total = c.execute("SELECT COALESCE(SUM(replacements),0) FROM episodes").fetchone()[0]
    return {
        "learned": learned,
        "active": active,
        "learning": counts.get("learning", 0),
        "known": counts.get("known", 0),
        "ignored": counts.get("ignored", 0),
        "episodes": len(list(c.execute("SELECT id FROM episodes"))),
        "injections": total,
        "recent": eps,
        "clock": dbm.clock(c),
    }


@app.get("/api/words")
def words(
    status: str = "all",
    lang: str = "ja",
    kana_threshold: int = DEFAULTS["kana_threshold"],
    note_threshold: int = DEFAULTS["note_threshold"],
    learning_threshold: int = DEFAULTS["learning_threshold"],
    at: int = None,
):
    c = read_conn(at)
    clk = dbm.clock(c)  # current forgetting-clock position (total JA tokens watched)
    q = "SELECT * FROM words WHERE lang=?"
    params: tuple = (lang,)
    if status != "all":
        q += " AND status=?"
        params = (lang, status)
    q += " ORDER BY status, exposures DESC, lemma"
    out = []
    for r in c.execute(q, params):
        exp = r["exposures"]
        # fade position: 1 = freshly learning (fully annotated), 0 = graduated
        fade = max(0.0, 1.0 - (exp / kana_threshold)) if kana_threshold else 0.0
        from .cli import default_note

        out.append(
            {
                "lemma": r["lemma"],
                "reading": r["reading"],
                "romaji": r["romaji"],
                "gloss": r["gloss"],
                "pos": r["pos"],
                "status": r["status"],
                "exposures": exp,
                "note": r["note"],
                "note_effective": r["note"]
                or default_note(r["lemma"], r["reading"], r["romaji"], r["gloss"]),
                "fade": round(fade, 3),
                "recency": r["last_seen_pos"],  # forgetting-clock pos at last injection
                "gap": clk - r["last_seen_pos"],  # JA tokens watched since last injection
                "added": r["id"],  # monotonic insert order (add-to-DB recency)
                "introduced_in": r["first_seen"],
                "learned_in": r["learned_at_episode"],
                "learned": r["status"] == "learning" and exp >= learning_threshold,
                "stage": (
                    "bare"
                    if exp >= kana_threshold
                    else "reading"
                    if exp >= note_threshold
                    else "note+reading"
                ),
            }
        )
    return {"words": out}


@app.post("/api/word/{lemma}")
def update_word(lemma: str, body: dict):
    c = conn()
    lang = body.get("lang", "ja")
    if not c.execute("SELECT 1 FROM words WHERE lemma=? AND lang=?", (lemma, lang)).fetchone():
        raise HTTPException(404, f"{lemma} not in database")
    if "status" in body:
        dbm.set_status(c, lemma, body["status"], lang=lang)
    if "note" in body:  # "" or null clears the override
        dbm.set_note(c, lemma, body["note"] or None, lang=lang)
    c.commit()
    return {"ok": True}


def _ep_label(show, episode_no, name):
    """Human label for an episode: '<show> E04' if metadata is set, else the file."""
    if show and episode_no:
        return f"{show} E{episode_no}"
    if episode_no:
        return f"E{episode_no}"
    return name


@app.get("/api/episodes")
def episodes():
    """The watch-order timeline of processed episodes (active branch)."""
    c = conn()
    out = []
    for i, r in enumerate(c.execute("SELECT * FROM episodes ORDER BY id"), 1):
        out.append(
            {
                "id": r["id"],
                "position": i,
                "name": r["name"],
                "show": r["show"],
                "episode_no": r["episode_no"],
                "label": _ep_label(r["show"], r["episode_no"], r["name"]),
                "processed_at": r["processed_at"],
                "new_words": r["new_words"],
                "replacements": r["replacements"],
                "tokens": r["tokens"],
            }
        )
    return {"episodes": out}


@app.post("/api/episode/{ep_id}")
def update_episode(ep_id: int, body: dict):
    """Edit an episode's show/number metadata (the auto-detect is editable)."""
    c = conn()
    if not c.execute("SELECT 1 FROM episodes WHERE id=?", (ep_id,)).fetchone():
        raise HTTPException(404, "no such episode")
    if "show" in body:
        c.execute("UPDATE episodes SET show=? WHERE id=?", (body["show"] or None, ep_id))
    if "episode_no" in body:
        c.execute(
            "UPDATE episodes SET episode_no=? WHERE id=?", (body["episode_no"] or None, ep_id)
        )
    c.commit()
    return {"ok": True}


@app.get("/api/word/{lemma}/history")
def word_history(lemma: str, lang: str = "ja"):
    """Full lifecycle of one word: where introduced, when learned, and its
    per-episode appearance/injection history + the English words it replaced."""
    c = conn()
    w = c.execute("SELECT * FROM words WHERE lemma=? AND lang=?", (lemma, lang)).fetchone()
    if not w:
        raise HTTPException(404, f"{lemma} not in database")
    hist = [
        {
            "episode": _ep_label(r["show"], r["episode_no"], r["name"]),
            "name": r["name"],
            "occurrences": r["occurrences"],
            "replacements": r["replacements"],
            "processed_at": r["processed_at"],
        }
        for r in c.execute(
            "SELECT e.name, e.show, e.episode_no, e.processed_at, "
            "s.occurrences, s.replacements FROM sightings s "
            "JOIN episodes e ON e.id=s.episode_id WHERE s.word_id=? ORDER BY e.id",
            (w["id"],),
        )
    ]
    return {
        "lemma": lemma,
        "reading": w["reading"],
        "romaji": w["romaji"],
        "gloss": w["gloss"],
        "status": w["status"],
        "exposures": w["exposures"],
        "introduced_in": w["first_seen"],
        "learned_in": w["learned_at_episode"],
        "learned_at": w["learned_at"],
        "episodes_seen": len(hist),
        "variants": dbm.word_variants(c, w["id"]),
        "history": hist,
    }


@app.get("/api/browse")
def browse(dir: str = None):
    d = dir or os.getcwd()
    d = os.path.abspath(os.path.expanduser(d))
    if not os.path.isdir(d):
        raise HTTPException(400, f"not a directory: {d}")
    dirs, subs = [], []
    try:
        for name in sorted(os.listdir(d)):
            full = os.path.join(d, name)
            if os.path.isdir(full):
                dirs.append(name)
            elif name.lower().endswith((".srt", ".ass", ".ssa", ".vtt")):
                subs.append(name)
    except PermissionError:
        raise HTTPException(403, f"permission denied: {d}") from None
    return {"dir": d, "parent": os.path.dirname(d), "dirs": dirs, "subs": subs}


def _parse_range(range_header: str, size: int) -> tuple[int, int] | None:
    """Parse a single-range 'bytes=start-end' Range header value.

    Supports 'bytes=start-end', 'bytes=start-' (open-ended), and the suffix
    form 'bytes=-N' (last N bytes). Multi-range specs are rejected (only the
    first range is considered -- <video> elements never send multi-range).
    Returns an inclusive (start, end) clamped to `size`, or None if the
    header is malformed or unsatisfiable.
    """
    if not range_header.startswith("bytes="):
        return None
    spec = range_header[len("bytes=") :].split(",")[0].strip()
    if "-" not in spec:
        return None
    start_s, end_s = spec.split("-", 1)
    try:
        if start_s == "":
            n = int(end_s)
            if n <= 0:
                return None
            start, end = max(0, size - n), size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start > end or start >= size or start < 0:
        return None
    return start, min(end, size - 1)


CHUNK = 1 << 20  # 1 MiB read/yield size


@app.get("/api/video")
def video(path: str, request: Request):
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(p):
        raise HTTPException(400, f"not a file: {p}")
    try:
        size = os.path.getsize(p)
    except PermissionError:
        raise HTTPException(403, f"permission denied: {p}") from None
    media_type = mimetypes.guess_type(p)[0] or "application/octet-stream"

    range_header = request.headers.get("range")
    if range_header:
        parsed = _parse_range(range_header, size)
        if parsed is None:
            raise HTTPException(416, "invalid or unsatisfiable range")
        start, end = parsed
        length = end - start + 1

        def iter_range():
            with open(p, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        }
        return StreamingResponse(
            iter_range(), status_code=206, headers=headers, media_type=media_type
        )

    def iter_full():
        with open(p, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                yield chunk

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(size)}
    return StreamingResponse(iter_full(), headers=headers, media_type=media_type)


@app.get("/api/vtt")
def vtt(path: str):
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(p):
        raise HTTPException(400, f"not a file: {p}")
    try:
        subs = pysubs2.load(p)
    except PermissionError:
        raise HTTPException(403, f"permission denied: {p}") from None
    except Exception as e:  # noqa: BLE001 -- any pysubs2 parse failure is a 400, not a 500
        raise HTTPException(400, f"could not parse subtitle file: {e}") from e
    return Response(content=subs.to_string("vtt"), media_type="text/vtt; charset=utf-8")


@app.get("/api/layers")
def layers(path: str):
    from .cli import layer_paths  # deferred: keep cli.py's NLP-stack import off web.py's path

    resolved = os.path.abspath(os.path.expanduser(path))
    paths = layer_paths(resolved)
    out = {name: {"path": p, "exists": os.path.isfile(p)} for name, p in paths.items()}
    plan_path = os.path.splitext(paths["adaptive"])[0] + ".plan.json"  # cli.py's own formula
    return {"layers": out, "plan": {"path": plan_path, "exists": os.path.isfile(plan_path)}}


def _knob_args(body):
    """Translate UI knob values into bll process/season CLI flags."""
    a = []
    g = body.get
    if g("new_words") is not None:
        a += ["-n", g("new_words")]
    if g("max_active") is not None:
        a += ["--max-active", g("max_active")]
    if g("learning_threshold") is not None:
        a += ["--learning-threshold", g("learning_threshold")]
    if g("min_count") is not None:
        a += ["--min-count", g("min_count")]
    if g("min_zipf") is not None:
        a += ["--min-zipf", g("min_zipf")]
    if g("max_per_cue") is not None:
        a += ["--max-per-cue", g("max_per_cue")]
    if g("kana_threshold") is not None:
        a += ["--kana-threshold", g("kana_threshold")]
    if g("decay_tokens") is not None:
        a += ["--decay-tokens", g("decay_tokens")]
    if g("note_threshold") is not None:
        a += ["--note-threshold", g("note_threshold")]
    if g("include_pos"):
        a += ["--include-pos", g("include_pos")]
    if g("backend"):
        a += ["--backend", g("backend")]
    if g("model"):
        a += ["--model", g("model")]
    if g("notes"):
        a += ["--notes", g("notes")]
    if g("romaji"):
        a += ["--romaji"]
    if g("no_dict"):
        a += ["--no-dict"]
    if g("dry_run"):
        a += ["--dry-run"]
    return a


def _snap_after(job):
    """Snapshot the active branch's head once a process job succeeds, so the new
    episode becomes a seekable timeline position."""
    if job.returncode == 0:
        try:
            tl.record(job.db)
        except Exception as e:  # noqa: BLE001 - never fail the job over a snapshot
            job.lines.append(f"[timeline] snapshot skipped: {e}")


@app.get("/api/timeline")
def timeline_state():
    return tl.state(STATE.active_db)


@app.post("/api/timeline/branch")
def timeline_branch(body: dict):
    pos, name = body.get("position"), (body.get("name") or "").strip()
    if not pos or not name:
        raise HTTPException(400, "position and name are required")
    try:
        bid = tl.branch(STATE.active_db, int(pos), name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "branch": bid}


@app.post("/api/timeline/switch")
def timeline_switch(body: dict):
    try:
        tl.switch(STATE.active_db, body.get("branch"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@app.post("/api/process")
def process(body: dict):
    ja, en = body.get("ja_sub"), body.get("en_sub")
    if not ja or not en:
        raise HTTPException(400, "ja_sub and en_sub are required")
    argv = _bll_argv("process", ja, en)
    if body.get("output"):
        argv += ["-o", body["output"]]
    if body.get("season_dir"):
        argv += ["--season-dir", body["season_dir"]]
    if body.get("show"):
        argv += ["--show", body["show"]]
    if body.get("episode"):
        argv += ["--episode", body["episode"]]
    argv += _knob_args(body)
    job = run_job(f"process {os.path.basename(ja)}", argv, then=_snap_after)
    return {"job_id": job.id}


@app.post("/api/season")
def season(body: dict):
    """Batch a whole season: every JA sub in `dir` (paired with its EN
    counterpart), in order, each with the rest-of-season lookahead. Mirrors the
    CLI's per-episode run but loops, so per-season pacing is fully exposed."""
    d = body.get("dir")
    if not d or not os.path.isdir(d):
        raise HTTPException(400, f"not a directory: {d}")
    jas = sorted(glob.glob(os.path.join(d, "*.ja.*")))
    pairs = []
    for ja in jas:
        stem = os.path.basename(ja).split(".ja.")[0]
        ens = sorted(glob.glob(os.path.join(d, stem + ".en.*")))
        if ens:
            pairs.append((ja, ens[0]))
    if not pairs:
        raise HTTPException(400, f"no JA/EN subtitle pairs found in {d}")

    knobs = _knob_args(body)
    if body.get("show"):  # one show for the whole season; episode auto-parses
        knobs += ["--show", body["show"]]
    install_dir = body.get("install_dir")
    # one umbrella job whose worker drives the per-episode CLI calls in order
    job = Job(
        f"season {os.path.basename(d.rstrip('/'))} ({len(pairs)} eps)",
        ["<season-loop>"],
        db=STATE.active_db,
    )
    JOBS[job.id] = job

    def _worker():
        env = dict(os.environ, BLL_DB=job.db, PYTHONUNBUFFERED="1")
        ok = True
        for i, (ja, en) in enumerate(pairs, 1):
            job.lines.append(f"=== [{i}/{len(pairs)}] {os.path.basename(ja)} ===")
            out = None
            if install_dir:
                out = os.path.join(install_dir, os.path.basename(en))
            argv = _bll_argv("process", ja, en, "--season-dir", d, *knobs)
            if out:
                argv += ["-o", out]
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=os.getcwd(),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                for line in proc.stdout:
                    job.lines.append(line.rstrip("\n"))
                proc.wait()
                if proc.returncode != 0:
                    ok = False
                    job.lines.append(f"[episode failed rc={proc.returncode}] stopping")
                    break
                try:  # snapshot this episode as a seekable position
                    tl.record(job.db)
                except Exception as e:  # noqa: BLE001
                    job.lines.append(f"[timeline] snapshot skipped: {e}")
            except Exception as e:  # noqa: BLE001
                ok = False
                job.lines.append(f"[error] {e}")
                break
        job.status = "done" if ok else "failed"
        job.returncode = 0 if ok else 1
        job.lines.append("=== season batch complete ===" if ok else "=== season batch FAILED ===")

    threading.Thread(target=_worker, daemon=True).start()
    return {"job_id": job.id, "episodes": [os.path.basename(j) for j, _ in pairs]}


@app.post("/api/render")
def render(body: dict):
    plan = body.get("plan")
    out = body.get("output")
    if not plan or not out:
        raise HTTPException(400, "plan and output are required")
    argv = _bll_argv("render", plan, "-o", out)
    if body.get("en_sub"):
        argv += [body["en_sub"]]
    if body.get("romaji"):
        argv += ["--romaji"]
    if body.get("note_threshold") is not None:
        argv += ["--note-threshold", str(body["note_threshold"])]
    if body.get("notes"):
        argv += ["--notes", body["notes"]]
    job = run_job(f"render {os.path.basename(plan)}", argv)
    return {"job_id": job.id}


@app.post("/api/bootstrap")
def bootstrap(body: dict = None):
    body = body or {}
    argv = _bll_argv("bootstrap")
    if body.get("ollama_url"):
        argv += ["--ollama-url", body["ollama_url"]]
    if body.get("wait"):
        argv += ["--wait", str(body["wait"])]
    job = run_job("bootstrap aligner model", argv)
    return {"job_id": job.id}


@app.get("/api/ollama")
def ollama_status():
    """Is the aligner reachable + provisioned?"""
    try:
        url = bootstrapm.resolve_url(os.environ.get("OLLAMA_URL"))
    except Exception:
        return {"reachable": False, "profile_ready": False, "profile": bootstrapm.PROFILE}
    names = bootstrapm._tags(url)
    ready = bootstrapm.PROFILE in names or bootstrapm.PROFILE.split(":")[0] in names
    return {
        "reachable": True,
        "url": url,
        "profile": bootstrapm.PROFILE,
        "profile_ready": ready,
        "base": bootstrapm.BASE_MODEL,
        "base_present": bootstrapm.BASE_MODEL in names,
    }


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": [j.to_meta() for j in JOBS.values()]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "no such job")
    return {**j.to_meta(), "lines": j.lines}


@app.get("/api/jobs/{job_id}/stream")
def stream_job(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "no such job")

    def gen():
        i = 0
        while True:
            while i < len(j.lines):
                yield f"data: {json.dumps(j.lines[i])}\n\n"
                i += 1
            if j.status != "running":
                yield (
                    f"event: end\ndata: {json.dumps({'status': j.status, 'rc': j.returncode})}\n\n"
                )
                return
            time.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream")
