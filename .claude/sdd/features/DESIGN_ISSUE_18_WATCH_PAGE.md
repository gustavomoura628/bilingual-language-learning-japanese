# DESIGN: Issue #18 — In-app UI: watch video + subtitles inside the app, learned-words view

> Technical design for a new `/watch` page inside the existing `bll serve` FastAPI app: a native
> `<video>` element with switchable WebVTT subtitle layers (converted server-side from the rendered
> SRT layers) and the existing learned-words panel alongside, plus the plumbing (Range-aware video
> streaming, SRT→VTT conversion, layer discovery) needed to make that work — all localhost-only,
> zero player framework, zero external CDN assets.

## Metadata

| Attribute | Value |
|-----------|-------|
| **Feature** | ISSUE_18_WATCH_PAGE |
| **Date** | 2026-07-06 |
| **Method** | `agentspec:workflow:design` (Skill tool) returned its own reference/instruction markdown rather than autonomously executing — same behavior precedent already logged in `BUILD_REPORT_ISSUE_17_DB_VERSIONED_MIGRATIONS.md` and `BUILD_REPORT_ISSUE_28_LANG_PLUMBING.md` (both Decision #1). This document was produced by manually following that returned process end-to-end: load DEFINE, explore the codebase for the named patterns, make the architecture/decision/file-manifest calls, write this file. |
| **DEFINE** | [DEFINE_ISSUE_18_WATCH_PAGE.md](../_synthesized/DEFINE_ISSUE_18_WATCH_PAGE.md) |
| **Issue** | Future-Gadgets-AI/bilingual-language-learning#18 |
| **Status** | Ready for Build |

---

## Invariant — hard rails (non-negotiable, restated from the issue and DEFINE doc)

1. **Extend, don't replace.** Everything lives inside the existing `bll serve` FastAPI app
   (`bll/web.py`) and `bll/static/`. No new process, no new port, no new framework.
2. **Native `<video>` + WebVTT only.** No embedded player library (video.js, plyr, hls.js, …).
   Layer switching must be achieved with plain `<track>` elements and `TextTrack.mode` toggling —
   not by swapping `<video src>` or re-fetching per switch.
3. **No external CDN assets on `/watch`.** `bll/static/index.html` loads Google Fonts over a CDN
   `<link>`; the new page must not add a second one (see Decision 2) — self-contained static files
   only, per the issue's explicit non-goal.
4. **Security posture unchanged.** Every new path-taking endpoint reuses `/api/browse`'s guard
   style (`os.path.abspath(os.path.expanduser(...))`, existence checks, `PermissionError` → 403) —
   no new class of traversal surface beyond what `/api/browse`/`/api/process` already expose today.
5. **`bll/db.py` is untouched.** No schema or migration change — this issue is UI + two new
   read-only-of-disk endpoints, nothing DB-shaped beyond the already-existing `/api/words`.
6. **Suite floor is 35** (this branch's base, confirmed by running it) **+ new tests**, with
   `ruff check`, `ruff format --check`, and `mypy bll/` all clean — `bll/web.py` stays under its
   existing mypy `ignore_errors` carve-out, but new code is written fully typed anyway.

---

## Architecture Overview

```text
┌────────────────────────────────────────────────────────────────────────────────┐
│                 /watch — IN-APP VIDEO + SUBTITLES  (issue #18)                 │
├────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Browser — bll/static/watch.html (NEW, sibling to index.html, self-contained)   │
│  ┌────────────────────────────────────────────────────────────────────────┐    │
│  │ [ video path input ] [ subtitles (adaptive) path input ] [ Load ]       │    │
│  │                                                                          │    │
│  │  <video id="player" controls>          <div id="words-panel">           │    │
│  │    <track label="adaptive" ...>          猫(ネコ) cat      [learning]   │    │
│  │    <track label="plain"    ...>          約束(やくそく) promise [known] │    │
│  │    <track label="kana"     ...>          ...                            │    │
│  │    <track label="answers"  ...>                                         │    │
│  │  [adaptive] [plain] [kana] [answers]   <- layer switcher: toggles       │    │
│  │                                            TextTrack.mode, no reload    │    │
│  └───────────────┬───────────────────────────────────┬────────────────────┘    │
│                  │ GET /api/video?path=   (Range)     │ GET /api/words?lang=ja  │
│                  │ GET /api/layers?path=               │  (existing, PR #33)   │
│                  │ GET /api/vtt?path=                  │                       │
│                  ▼                                     ▼                       │
│  bll/web.py  (FastAPI app, extended in place)                                  │
│  ┌───────────────────────────────────────┐   ┌────────────────────────────┐   │
│  │ GET /watch          -> watch.html      │   │ GET /api/words (unchanged) │   │
│  │ GET /api/video       -> 206/200 stream │   │ (lang-aware since #33)     │   │
│  │ GET /api/vtt         -> pysubs2 to_vtt │   └────────────────────────────┘   │
│  │ GET /api/layers      -> layer_paths()  │                                    │
│  │                          (bll.cli,      │                                    │
│  │                          deferred import)│                                   │
│  └─────────────────┬───────────────────────┘                                   │
│                    │ same guard style as /api/browse                          │
│                    │ (abspath+expanduser, isfile, PermissionError -> 403)      │
│                    ▼                                                           │
│         local filesystem: <video file> + <base>.srt / .plain.srt /            │
│         .kana.srt / .answers.srt / .plan.json  (bll/cli.py layer_paths())      │
│                                                                                  │
└────────────────────────────────────────────────────────────────────────────────┘
```

Nothing upstream changes: `bll process`, `bll/cli.py`'s rendering, and `bll/db.py`'s schema are
untouched. This is a pure *presentation + file-serving* addition sitting beside the existing
operator console, reading files the CLI already produces and rows the DB already stores.

---

## Components

| Component | Purpose | Technology |
|-----------|---------|------------|
| `bll/static/watch.html` (new) | Self-contained `/watch` page: load form, `<video>` + layer switcher, words panel | HTML/CSS/JS, vanilla — no framework, no CDN |
| `bll/web.py` — `GET /watch` | Serves `watch.html`, same pattern as `GET /` → `index.html` | FastAPI, `HTMLResponse` |
| `bll/web.py` — `GET /api/video` | Range-aware byte streaming of a local video file | FastAPI, `StreamingResponse`, stdlib `mimetypes` |
| `bll/web.py` — `GET /api/vtt` | Converts a rendered subtitle layer (`.srt`/`.ass`/`.ssa`/`.vtt`) to WebVTT text | `pysubs2` (already a core dependency) |
| `bll/web.py` — `GET /api/layers` | Reports which of the 4 rendered layers + the plan sidecar exist for a given adaptive path | `bll.cli.layer_paths` (deferred import) |
| `bll/web.py` — `GET /api/words` (existing) | Learned-words panel data, `lang` query param | FastAPI (unchanged since PR #33) |
| `tests/test_web.py` (new) | `TestClient`-level coverage for AT-001..AT-004 | pytest, `fastapi.testclient`, `httpx` |
| `.github/workflows/tests.yml` | CI dependency install gains the `web` extra (Decision 8) | GitHub Actions, `uv sync` |
| `pyproject.toml` + `uv.lock` | `httpx` added to the `dev` dependency group (Decision 8) | `uv` |

---

## Key Decisions

### Decision 1: `/watch` is a new static file + a new thin endpoint, mirroring `GET /` exactly

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** `bll/web.py` already has exactly one precedent for serving HTML:

```python
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        return f.read()
```

There is no `StaticFiles` mount anywhere in the app — `index.html` is the only static asset, and
it is served by hand-reading the file inside the route function.

**Choice:** Add `bll/static/watch.html` (a second, independent, self-contained file — its own
inline `<style>` and `<script>`, no shared includes) and a second route, `GET /watch`, that reads
and returns it exactly like `index()` does.

**Rationale:** This is the smallest possible extension of an already-established, one-file-per-page
pattern. Introducing `StaticFiles`/Jinja templates/a shared layout for two pages would be new
infrastructure this issue doesn't need — `index.html` itself doesn't factor out shared CSS/JS
today, so `watch.html` duplicating the handful of CSS custom properties it needs (Decision 2) is
consistent with, not a regression from, the existing convention.

**Alternatives Rejected:**
1. `app.mount("/static", StaticFiles(directory=STATIC))` + serve `watch.html` as a static file
   directly at `/static/watch.html` — rejected: changes how the *existing* `index.html` is
   reachable would need to change too for consistency (or the app ends up with two inconsistent
   serving mechanisms), and the issue only asks for one new page under `/watch`, not a general
   static-file mount.
2. Server-side templating (Jinja2) to share a layout between `index.html` and `watch.html` —
   rejected: a new dependency and a new pattern for a two-page app; `index.html` has zero
   precedent for templating today.

**Consequences:** `watch.html` is a second, fully independent file. Any future shared chrome
(nav, fonts, CSS variables) between the two pages is a deliberate, separate refactor — not
something this issue's minimal diff should force.

---

### Decision 2: `/watch` reuses index.html's color palette but drops the CDN font links

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** This is the tension the DEFINE doc explicitly flags: `bll/static/index.html` loads
`Zen Kaku Gothic New` + `IBM Plex Mono` from `fonts.googleapis.com`/`fonts.gstatic.com`, but the
issue's own non-goals say the new page must be "self-contained static files... no external CDN
assets." Its CSS variables are otherwise just hex colors with no network dependency:
`--ink:#14110E; --paper:#F5F2EA; --amber:#E8A33D; --jade:#5B8C7B; --rule:#3A352E; ...`, and its
font-family variables already carry system-font fallback chains alongside the CDN fonts:
`--jp:"Zen Kaku Gothic New",system-ui,"Hiragino Kaku Gothic ProN","Noto Sans JP",sans-serif;`
`--mono:"IBM Plex Mono",ui-monospace,monospace;`.

**Choice:** `watch.html` defines the identical color custom properties byte-for-byte (so the two
pages read as siblings), but its own `--jp`/`--mono` drop only the leading CDN-only font name and
keep every fallback already present in `index.html`'s own chain:
`--jp:system-ui,"Hiragino Kaku Gothic ProN","Noto Sans JP",sans-serif;`
`--mono:ui-monospace,monospace;`
No `<link rel="preconnect">` / `fonts.googleapis.com` tags are added to `watch.html`.

**Rationale:** This satisfies "no external CDN assets" literally, without inventing a new font
stack — every fallback name used already exists in `index.html`'s own chain (this design doesn't
pick new fonts, it drops the one CDN-only entry), so JA glyph rendering degrades gracefully to
whatever CJK-capable system font the OS provides (Hiragino on macOS, Noto Sans JP on most Linux
desktops) rather than looking like a mismatched fallback nobody chose deliberately.

**Alternatives Rejected:**
1. Self-host the two font files (`.woff2`) under `bll/static/` and reference them with local
   `@font-face` rules — rejected: adds two binary asset files and a licensing/vendoring question
   to a v1-utilitarian page; "no external CDN assets" reads as "don't add a network dependency,"
   not "match index.html's exact typography," and AT-007 already defers visual taste to Lucas
   post-merge.
2. Also strip the CDN links from `index.html` for consistency — rejected: out of scope (issue #18
   doesn't ask for changes to `index.html`; see Decision 3), and would be an unrelated behavior
   change the DEFINE doc doesn't authorize.

**Consequences:** `/watch`'s Japanese text renders in a different (system) font than
`index.html`'s (webfont) — a visible, acceptable, and explicitly-flagged-as-latitude difference
between the two pages until/unless Lucas's post-merge taste pass says otherwise.

---

### Decision 3: No file-browser modal on `/watch`, and `index.html` is not modified

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** `index.html`'s `browse()`/`openBrowser()` functions (lines ~742-765) drive a file-pick
modal wired to `/api/browse`, but they are tightly coupled to that page's own DOM ids (`$("modal")`,
`$("br-path")`, `$("br-list")`, a global `BR_TARGET`/`BR_DIRONLY`, and a call out to `autoMeta()`).
`/api/browse` itself only lists directories and `.srt/.ass/.ssa/.vtt` files (`subs`), not video
files, so it can't drive a video-file picker without a filter change.

**Choice:** `/watch`'s "video path" and "subtitles (adaptive) path" inputs are plain
`<input type="text">` fields — the user pastes an absolute path and clicks Load. No browse modal
is ported into `watch.html`, `/api/browse` is not modified, and `index.html` receives no changes
(no nav link to `/watch` either — reached by typing the URL).

**Rationale:** Porting the modal means duplicating ~25 lines of DOM-id-specific JS/CSS/HTML into a
second file for a convenience the issue explicitly defers: AT-007 states the UI is
"v1-utilitarian" and taste follow-ups are Lucas's post-merge call. Extending `/api/browse` with a
video-extension filter is a real option but touches an *existing*, already-consumed endpoint
(`index.html`'s picker for `ja_sub`/`en_sub`/`output`/`season_dir`) for a benefit this issue doesn't
require — the smallest-blast-radius choice is to leave it alone entirely.

**Alternatives Rejected:**
1. Port `browse()`/`openBrowser()` + the modal markup into `watch.html` — rejected: meaningful
   duplicated surface (JS + CSS + HTML) for a v1 that explicitly doesn't need to be polished yet.
2. Add `kind: str = "subs"` to `/api/browse` so `?kind=video` filters on video extensions instead
   — rejected for this issue: modifies a shared, already-relied-upon endpoint's behavior for a
   convenience only the new page would use; revisit if/when Lucas's taste pass asks for it.
3. Add a one-line nav link from `index.html`'s header to `/watch` — rejected: `index.html` is a
   large (785-line), already-shipped file; even a one-line addition is a real touch to a file this
   issue's own scope (per the DEFINE doc's file list) never names, and discoverability is exactly
   the kind of "taste follow-up" AT-007 defers.

**Consequences:** v1 `/watch` requires the user to already know (or copy from their shell/terminal)
the absolute paths to the video file and the adaptive subtitle output — acceptable for a
localhost operator tool whose primary user is the person who just ran `bll process` and has both
paths on hand; a follow-up issue can wire discoverability (e.g., linking straight from
`/api/episodes` metadata) once Lucas's own usage surfaces what's actually annoying.

---

### Decision 4: `/api/video` — hand-rolled single-range HTTP Range support, no extension gate

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** The DEFINE doc pins the behavior ("seeking breaks without 206 partial-content
handling; implement and test it") but leaves the exact endpoint shape to design latitude. FastAPI
has no built-in Range-aware file response; Starlette's own `FileResponse` does not implement
`Accept-Ranges`/`Content-Range` either as of the version pinned here. `StreamingResponse` (already
imported in `web.py`, already used by `stream_job()`) is the primitive available.

**Choice:** `GET /api/video?path=<abs path>`, taking the raw Starlette `Request` to read the
`Range` header. A small helper, `_parse_range(range_header, size)`, supports the three real-world
forms browsers send — `bytes=start-end`, `bytes=start-`, and the suffix form `bytes=-N` — for a
*single* range only (multi-range `bytes=0-99,200-299` is not supported; `<video>` elements never
send it). No Range header → `200` with the full body; a Range header → `206` with
`Content-Range`/`Accept-Ranges`/`Content-Length`; an unparseable/unsatisfiable Range → `416`. Path
validation matches `/api/browse`: `os.path.abspath(os.path.expanduser(path))`, `os.path.isfile`
(not `isdir` — this endpoint serves a file, not a listing), `PermissionError` → `403`. There is
**no video-extension allowlist** — any existing, readable file the given path resolves to is
served.

**Rationale:** Range support is the one genuinely load-bearing piece of new server logic in this
design (everything else is thin glue) — `<video>` seeking is unusable without it, which is exactly
why the DEFINE doc calls it out as pinned behavior, not latitude. Skipping an extension allowlist
is deliberate: this is a localhost-only, single-user tool where `/api/process` already accepts
arbitrary `ja_sub`/`en_sub` paths and runs a subprocess against them, and `/api/browse` already
lets the same caller enumerate arbitrary directories — serving the bytes of a file at a path the
same local caller supplied is not a new trust boundary, it's the same one, applied to video.
Gating on extension would also fight the acceptance test directly: AT-002 exercises "a real temp
file," which a test has no reason to name with a real video container extension.

**Alternatives Rejected:**
1. `starlette.responses.FileResponse` — rejected: no Range/206 support without hand-rolling the
   exact same logic on top of it anyway (in the FastAPI/Starlette versions pinned by this repo's
   `uv.lock`), so it buys nothing over `StreamingResponse`, which the file already imports and uses.
2. A video-extension allowlist (`.mp4`, `.mkv`, `.webm`, …) — rejected: false sense of security
   (this tool has no auth boundary to defend in the first place — see Security Considerations) and
   actively works against realistic test fixtures and real-world containers this list would
   inevitably miss (`.ts`, `.m2ts`, arbitrary muxed formats IINA already plays today).
3. Multi-range support (`bytes=0-99,200-299/*` style, multipart `Content-Type:
   multipart/byteranges`) — rejected: no real `<video>` client sends this; supporting it is
   speculative complexity with zero acceptance-criteria backing.

**Consequences:** Any locally-readable file is servable through this endpoint given its path —
identical in spirit to what `/api/browse` and `/api/process` already expose, and explicitly
accepted as unchanged posture (Invariant 4). `_parse_range`'s 416 path is real but not directly
required by any AT (AT-002 only exercises the happy paths); it is included because a malformed
Range header should not `500`, not because a test demands it — see Testing Strategy.

---

### Decision 5: `/api/vtt` — always convert via `pysubs2`, never pass through raw bytes

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** The DEFINE doc already confirms locally that `pysubs2.load(path).to_string("vtt")`
produces a valid `WEBVTT` document with cue text and `HH:MM:SS.mmm` timings preserved. The four
rendered layers are always `.srt` today (per `layer_paths()`'s `ext` coming from whatever `-o`
extension `bll process` was given, conventionally `.srt`), but `pysubs2.load()` auto-detects
format from content, not just extension.

**Choice:** `GET /api/vtt?path=<abs path>` always does `pysubs2.load(p).to_string("vtt")` and
returns it as `Response(content=..., media_type="text/vtt; charset=utf-8")`, regardless of the
source file's actual extension — never a raw passthrough, even if the source happens to already
be `.vtt`. Path validation identical to Decision 4 (`isfile`, `PermissionError` → 403); a
`pysubs2` load failure (corrupt/unsupported content) is caught and re-raised as `400`, not `500`.

**Rationale:** A single code path (always parse, always re-serialize) is simpler than branching
on "is this already VTT" and is exactly as fast as this tool needs (subtitle files are tiny —
kilobytes, not megabytes). Catching the parse failure as `400` rather than letting `pysubs2`'s
exception bubble into FastAPI's default `500` handler gives the frontend (and AT-006's curl smoke)
a meaningful, actionable status code for "that path isn't a subtitle file."

**Alternatives Rejected:**
1. Pass through raw bytes with `media_type="text/vtt"` when the extension is already `.vtt` —
   rejected: adds a branch to save a millisecond-scale parse on files this small; also means two
   different code paths need to agree on cue-text-survives-conversion behavior, doubling the
   testing surface for zero real benefit.

**Consequences:** Every subtitle layer served through `/watch` is guaranteed well-formed WebVTT
(pysubs2's own serializer), independent of whatever format quirks the source `.srt` might carry.

---

### Decision 6: `/api/layers` reuses `bll.cli.layer_paths()` via a deferred (in-function) import

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** `bll/web.py` has exactly one existing precedent for calling into `bll/cli.py`: inside
`words()`, `from .cli import default_note` is imported *inside the function body*, not at module
level, even though `web.py` already does module-level `from . import db as dbm` /
`from . import bootstrap as bootstrapm` / `from . import timeline as tl`. `cli.py` pulls in the
heavy NLP stack (`fugashi[unidic-lite]`, `wordfreq[cjk]`, `pykakasi`, `simplemma`) at its own module
level — the existing lazy-import pattern keeps that cost out of `web.py`'s module load (and thus
`bll serve`'s startup and every request that never touches `words()`).

**Choice:** `GET /api/layers?path=<adaptive path>` imports `layer_paths` the same way:
`from .cli import layer_paths` *inside* the endpoint function, not at the top of `web.py`. It
computes the four sibling paths via `layer_paths(...)`, checks `os.path.isfile()` on each (plus
the plan sidecar, using cli.py's own `os.path.splitext(out)[0] + ".plan.json"` formula so the
naming rule lives in exactly one place, transcribed identically here rather than re-derived), and
returns **200 with per-layer `exists` booleans** — never a 400/404 for "some layers are missing,"
since a partially-rendered episode (e.g., `--no-dict` runs, or a `render_plan`-only re-render) is a
normal, not-erroneous state the frontend should degrade into gracefully (fewer buttons in the
layer switcher, not an error banner).

**Rationale:** Reusing `layer_paths()` instead of re-deriving the `{base}.plain{ext}` /
`{base}.kana{ext}` / `{base}.answers{ext}` naming convention in `web.py` keeps the naming rule in
exactly one place (`cli.py`), so a future change to that convention can't silently desync the two
modules. The deferred-import style matches the file's own established precedent rather than
introducing a second, inconsistent way of borrowing from `cli.py`.

**Alternatives Rejected:**
1. Module-level `from . import cli as clim` in `web.py` — rejected: pays `cli.py`'s full NLP-stack
   import cost on every `bll serve` boot, contradicting the existing lazy-import precedent this
   file already established for exactly this reason.
2. Copy/reimplement the four-suffix naming logic directly in `web.py` — rejected: DRY violation;
   two independently-maintained copies of "what are an episode's layer filenames" will drift.
3. 404 (or 400) when one or more layers don't exist — rejected: makes a normal partial-render state
   look like an error; booleans let the UI degrade gracefully (Decision 7's layer switcher only
   renders buttons for layers that exist).

**Consequences:** `/api/layers` never fails on a "some files missing" basis — only a genuinely
malformed request (missing `path` query param, caught by FastAPI's own required-parameter
validation → `422`) would produce a non-200 response.

---

### Decision 7: layer switching = multiple native `<track>` elements + `TextTrack.mode` toggling

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** Invariant 2 pins native `<video>` + WebVTT with no player framework. The standard
platform primitive for "one video, several selectable subtitle tracks" is the `<video>` element's
own `textTracks` list: multiple `<track kind="subtitles">` children, each independently toggled
between `"showing"` and `"hidden"` via JS, with **no reload of the video element itself**.

**Choice:** On "Load," JS calls `/api/layers?path=...`, then for every layer where `exists` is
true, appends one `<track kind="subtitles" label="{name}" srclang="ja"
src="/api/vtt?path={encoded layer path}">` to `#player`, and renders one button per existing layer
in a small switcher row. Clicking a button sets that layer's `TextTrack.mode = "showing"` and every
other track's `mode = "hidden"` — the `<video src>` itself is set once (to `/api/video?path=...`)
and never touched again when switching layers.

**Rationale:** This is the only zero-framework way to get instant, reload-free layer switching:
all four (or fewer) VTT documents are small text files the browser fetches once per `<track>` on
load, and toggling `.mode` is a synchronous, local operation with no network round-trip — switching
layers mid-playback doesn't even pause the video.

**Alternatives Rejected:**
1. Re-point a single `<track src=...>` at a different layer's VTT URL on switch — rejected: per
   the WHATWG HTML spec, mutating a `<track>` element's `src` does not reliably reload the track in
   all browsers without removing/re-appending the node, which is strictly more code than just
   pre-loading all layers as separate tracks and toggling `.mode` — and re-appending would also
   drop the user's playback position's association with the old track's already-parsed cues.
2. Server-side: bake all four layers into one VTT with custom cue classes and toggle CSS visibility
   — rejected: `<video>` native subtitle rendering doesn't expose cue-level CSS selection the way
   this would need; would require abandoning native track rendering for a hand-rolled overlay,
   which is much closer to "a player framework" than Invariant 2 allows.

**Consequences:** All existing layers for an episode are fetched (as text, kilobytes) up front on
Load, not lazily per-switch — a deliberate, negligible cost given subtitle file sizes.

---

### Decision 8: fix the latent CI gap that this issue is the first to expose

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** `fastapi`/`uvicorn` live under `[project.optional-dependencies] web` in
`pyproject.toml`, not `[dependency-groups] dev`. `.github/workflows/tests.yml`'s only install step
is `uv sync --locked --dev` — **no `--extra web` and no `--all-extras`** — confirmed directly from
this repo's own workflow file. `uv sync --dev` installs the `dev` dependency group; it does **not**
install optional extras unless `--extra <name>`/`--all-extras` is passed (confirmed against this
machine's installed `uv 0.11.7 --help`: `--extra <EXTRA>` — "Include optional dependencies from the
specified extra name" — is a separate flag from `--dev`). No test file today imports `bll.web` (`ls
tests/` has no `test_web.py`), so this gap has been latent and invisible: nothing before this issue
ever required `fastapi` to be importable in the CI environment. `tests/test_web.py` (this design,
Testing Strategy) is the **first** test module to `from bll import web` / use
`fastapi.testclient.TestClient`, which also requires `httpx` (confirmed missing:
`uv run python -c "import httpx"` fails on this branch's base) — currently in neither `web` nor
`dev`.

**Choice:** Two coordinated changes:
1. `pyproject.toml`: add `httpx>=0.27` to `[dependency-groups] dev` (test-only need — `TestClient`
   — not an end-user runtime need, so it does not belong in `[project.optional-dependencies] web`).
   Applied via `uv add --dev httpx` (or equivalent), which updates `uv.lock` in the same operation
   — never hand-edit `uv.lock` directly.
2. `.github/workflows/tests.yml`: change the install step from `uv sync --locked --dev` to
   `uv sync --locked --dev --extra web`, so `fastapi`/`uvicorn` (needed to import `bll.web` at all)
   are present in CI.

**Rationale:** `--locked` (confirmed via `uv sync --help`: "Assert that the `uv.lock` will remain
unchanged") means CI *fails* if `pyproject.toml` and `uv.lock` disagree — so step 1's `uv.lock`
update is not optional bookkeeping, it's required for `--locked` to keep passing at all once
`httpx` is added to `pyproject.toml`. Without step 2, `tests/test_web.py` collection itself raises
`ModuleNotFoundError: No module named 'fastapi'` in CI even though it may pass on a machine that
happens to already have the `web` extra installed locally — exactly the "works on my machine, red
in CI" failure mode a design should catch before build, not after a red PR check. This is
infrastructure this issue's own AT-005 ("suite green... in CI") cannot be satisfied without; it is
not scope creep — it is a load-bearing prerequisite this issue is the first to need and the first
to expose.

**Alternatives Rejected:**
1. Move `fastapi`/`uvicorn` into `[dependency-groups] dev` instead of adding `--extra web` to CI —
   rejected: conflates two different audiences (`web` = "what an end user needs to run `bll
   serve`," `dev` = "what a contributor needs to test/lint/typecheck"); an end user running
   `pip install 'bll[web]'` doesn't want `pytest`/`ruff`/`mypy`/`httpx`, and moving `fastapi` out of
   `web` would break that install's completeness.
2. Do nothing and let the build phase discover the CI failure after opening the PR — rejected:
   directly contradicts AT-005 and this workflow's own phased-delivery gate ("no PR without an
   executed test... smoke of the change"); the fix is one line in a workflow file plus one
   dependency-group entry, cheap to specify now.

**Consequences:** Every future CI run installs `fastapi`/`uvicorn` in addition to `dev` — a small,
permanent increase in CI install time (both packages are already resolved in `uv.lock` today, so no
new network resolution cost, only install time for two additional wheels already cached in the
lock).

---

### Decision 9: AT-004 is tested against a directly-constructed DB, not the full e2e pipeline

| Attribute | Value |
|-----------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-06 |

**Context:** `/api/words` is unchanged by this issue (already `lang`-aware since PR #33); AT-004
only requires that its data path is "exercised by a test — existing endpoint, no regressions."
`tests/test_process_e2e_smoke.py`'s `run_process()` helper produces a fully realistic populated DB
via the real CLI pipeline, but it's a heavier fixture (spins up `cli.main([...])`, needs
`--gloss-json`/`--no-dict`, network-guards) than this AT needs.

**Choice:** `tests/test_web.py`'s AT-004 test builds a minimal DB directly:
`dbm.connect(tmp_path/"vocab.db")` → `dbm.upsert_word(conn, "猫", "ネコ", "neko", "cat", "noun",
"e01.ja.srt")` → `conn.commit()`, points `web.STATE.active_db` at that path via
`monkeypatch.setattr`, then hits `/api/words?lang=ja` through `TestClient` and asserts the inserted
lemma comes back.

**Rationale:** `db.py`'s public `connect`/`upsert_word` functions are exactly the same primitives
`tests/test_db.py` already uses directly (see `DESIGN_ISSUE_28_LANG_PLUMBING.md`'s own test
sketches) — reusing that lighter-weight, already-established pattern is simpler and more
self-contained than importing another test module's fixture helper, and is still a genuine
regression check tied to real inserted data (not merely "the route returns 200 on an empty DB").

**Alternatives Rejected:**
1. Import and call `test_process_e2e_smoke.run_process()` from `test_web.py` — rejected:
   cross-test-module coupling for no real gain; this AT needs "one word exists and lang scoping
   works," not a full gloss/align/render pipeline run.

**Consequences:** None beyond keeping `test_web.py` self-contained and fast.

---

## File Manifest

| # | File | Action | Purpose | Dependencies |
|---|------|--------|---------|--------------|
| 1 | `pyproject.toml` | Modify | Add `httpx>=0.27` to `[dependency-groups] dev` (Decision 8) | None |
| 2 | `uv.lock` | Modify (regenerated) | Re-resolved by `uv add --dev httpx` / `uv lock` — never hand-edited (Decision 8) | 1 |
| 3 | `.github/workflows/tests.yml` | Modify | `uv sync --locked --dev` → `uv sync --locked --dev --extra web` (Decision 8) | 2 (so `--locked` still passes) |
| 4 | `bll/web.py` | Modify | New imports (`mimetypes`, `Request`, `Response`, `pysubs2`); new `_parse_range` helper; new `GET /watch`, `GET /api/video`, `GET /api/vtt`, `GET /api/layers` | None (uses existing `bll.cli.layer_paths` via deferred import) |
| 5 | `bll/static/watch.html` | Create | Self-contained `/watch` page: load form, `<video>` + layer switcher, words panel | 4 (must match its exact endpoint shapes) |
| 6 | `tests/test_web.py` | Create | `TestClient` coverage for AT-001..AT-004 (+ one non-gating `_parse_range` edge test) | 1, 2, 3 (importability), 4 (endpoints to test) |

**Total Files:** 6 (2 new: `bll/static/watch.html`, `tests/test_web.py`; 4 modified, one of which —
`uv.lock` — is tool-regenerated rather than hand-edited)

Estimated scope: `bll/web.py` +~110-130 lines; `bll/static/watch.html` ~160-220 lines (materially
smaller than `index.html`'s 785, since the load form + video + layer switcher + words panel is a
narrow slice of `index.html`'s full operator-console surface); `tests/test_web.py` ~120-150 lines;
`pyproject.toml`/`tests.yml` one line each; `uv.lock` diff is whatever `uv` produces for `httpx` +
its transitive deps.

---

## Agent Assignment Rationale

This design touches more files (6) than the repo's prior single-pass precedent
(`DESIGN_ISSUE_28_LANG_PLUMBING.md`, 4 files), but it is still **one coherent build pass, not a
specialist split**. The deciding factor is a three-way contract that must stay consistent across
files written in the same pass: `bll/web.py`'s exact endpoint shapes (query param names, response
JSON keys, status codes) must match both (a) what `watch.html`'s JS fetches and renders, and (b)
what `tests/test_web.py` asserts. Splitting the frontend (`watch.html`) from the backend
(`web.py`) across two parallel agents reproduces exactly the drift risk
`DESIGN_ISSUE_28_LANG_PLUMBING.md`'s own Agent Assignment Rationale warned about ("the risk of two
agents drifting on the exact signature shape outweighs any parallelism benefit") — here the shared
"signature" is an HTTP contract instead of a Python one, but the risk is identical. The CI/lockfile
fix (Decision 8, files 1-3) is small, mechanical connective tissue with no design ambiguity left to
resolve — not worth its own agent either.

| Agent | Files Assigned | Why This Agent |
|-------|-----------------|-----------------|
| (general — build phase, direct) | 1, 2, 3, 4, 5, 6 | Single coherent feature; the web.py/watch.html/test_web.py three-way endpoint contract must be held in one head across all three to avoid drift; the CI/dependency fix is small enough to ride along in the same pass. |

**Agent Discovery:** specialists exist in this environment (`agentspec:python:python-developer`,
`agentspec:test:test-generator`, `agentspec:python:code-reviewer`) but were not assigned, per the
rationale above — consistent with this repo's established precedent for features of this size.

---

## Code Patterns

### Pattern 1: `bll/web.py` — new imports (additions to the existing import block)

```python
# existing:
import glob
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

# add:
import mimetypes

import pysubs2
from fastapi import Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse  # Response added
```

### Pattern 2: `GET /watch` (identical shape to the existing `index()`)

```python
@app.get("/watch", response_class=HTMLResponse)
def watch():
    with open(os.path.join(STATIC, "watch.html"), encoding="utf-8") as f:
        return f.read()
```

### Pattern 3: `_parse_range` + `GET /api/video` (Decision 4)

```python
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
```

### Pattern 4: `GET /api/vtt` (Decision 5)

```python
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
```

### Pattern 5: `GET /api/layers` (Decision 6)

```python
@app.get("/api/layers")
def layers(path: str):
    from .cli import layer_paths  # deferred: keep cli.py's heavy NLP-stack import off web.py's path

    resolved = os.path.abspath(os.path.expanduser(path))
    paths = layer_paths(resolved)
    out = {name: {"path": p, "exists": os.path.isfile(p)} for name, p in paths.items()}
    plan_path = os.path.splitext(paths["adaptive"])[0] + ".plan.json"  # cli.py's own formula
    return {"layers": out, "plan": {"path": plan_path, "exists": os.path.isfile(plan_path)}}
```

### Pattern 6: `bll/static/watch.html` — skeleton (structure, not full styling)

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bll · watch</title>
<style>
:root{
  --ink:#14110E; --paper:#F5F2EA; --muted:#9b9489; --rule:#3A352E;
  --amber:#E8A33D; --jade:#5B8C7B;
  --jp:system-ui,"Hiragino Kaku Gothic ProN","Noto Sans JP",sans-serif;
  --mono:ui-monospace,monospace;
}
/* ... layout / video / switcher / words-panel rules, same spirit as index.html ... */
</style>
</head>
<body>
<header><span class="brand">bll · watch</span></header>
<main>
  <div class="loadbar">
    <input type="text" id="video_path" placeholder="/abs/path/to/episode.mp4">
    <input type="text" id="subs_path" placeholder="/abs/path/to/out.srt (adaptive layer)">
    <button class="btn go" onclick="loadEpisode()">Load</button>
  </div>
  <div class="stage">
    <video id="player" controls></video>
    <div id="layer-switcher"></div>
  </div>
  <aside>
    <div id="words-panel"></div>
  </aside>
</main>
<script>
const $=id=>document.getElementById(id);
async function api(path){const r=await fetch(path);if(!r.ok)throw new Error(await r.text());return r.json();}

async function loadEpisode(){
  const videoPath=$("video_path").value.trim(), subsPath=$("subs_path").value.trim();
  if(!videoPath||!subsPath) return;
  const player=$("player");
  player.querySelectorAll("track").forEach(t=>t.remove());
  player.src="/api/video?path="+encodeURIComponent(videoPath);

  const data=await api("/api/layers?path="+encodeURIComponent(subsPath));
  const sw=$("layer-switcher"); sw.innerHTML=""; let first=null;
  for(const [name,info] of Object.entries(data.layers)){
    if(!info.exists) continue;
    const track=document.createElement("track");
    track.kind="subtitles"; track.label=name; track.srclang="ja";
    track.src="/api/vtt?path="+encodeURIComponent(info.path);
    player.appendChild(track);
    const btn=document.createElement("button");
    btn.className="btn"; btn.textContent=name; btn.onclick=()=>selectLayer(name);
    sw.appendChild(btn);
    if(!first) first=name;
  }
  player.load();
  if(first) player.addEventListener("loadedmetadata",()=>selectLayer(first),{once:true});
}
function selectLayer(name){
  const tt=$("player").textTracks;
  for(let i=0;i<tt.length;i++) tt[i].mode = tt[i].label===name ? "showing" : "hidden";
  $("layer-switcher").querySelectorAll("button").forEach(b=>
    b.classList.toggle("active", b.textContent===name));
}

async function loadWords(){
  const data=await api("/api/words?lang=ja");
  const el=$("words-panel"); el.innerHTML="";
  data.words.forEach(w=>{
    const row=document.createElement("div");
    row.className="wrow "+w.status;
    row.innerHTML=`<ruby>${w.lemma}<rt>${w.reading||""}</rt></ruby>`+
      `<span class="gloss">${w.gloss||""}</span><span class="badge">${w.status}</span>`;
    el.appendChild(row);
  });
}
loadWords();
</script>
</body>
</html>
```

---

## Data Flow

```text
FLOW A -- loading an episode into the player
1. User pastes a video path + the adaptive-layer subtitle path into /watch, clicks Load.
   │
   ▼
2. JS sets <video id="player"> src = /api/video?path=<video path>  (not fetched yet -- <video>
   lazily requests bytes, including ranged requests as the user seeks)
   │
   ▼
3. JS calls GET /api/layers?path=<adaptive path>
   │
   ▼
4. bll/web.py: layer_paths(path) [bll.cli, deferred import] -> 4 sibling paths + plan.json path
   -> os.path.isfile() on each -> {layers: {...exists...}, plan: {...exists...}}
   │
   ▼
5. JS appends one <track src="/api/vtt?path=<layer path>"> per existing layer, selects the first
   as "showing" once metadata loads
   │
   ▼
6. Browser requests each track's VTT once; separately, requests video bytes (ranged, as playback/
   seeking demands) from /api/video -- pysubs2 conversion (step 4's sibling) and video streaming
   (Decision 4) are independent, parallel fetches from the browser's perspective

FLOW B -- learned-words panel (unchanged data path, PR #33)
1. /watch page load -> JS calls GET /api/words?lang=ja
   │
   ▼
2. bll/web.py: words(status="all", lang="ja", ...) -- existing, untouched by this issue
   │
   ▼
3. JS renders one row per word (lemma + reading + gloss + status) into #words-panel
```

---

## Integration Points

None new *external*. `bll/web.py` continues to talk only to: the local filesystem (video +
subtitle files, via the new endpoints), the same-process SQLite DB (via `bll/db.py`, unchanged,
`/api/words`), and now also `pysubs2` in-process (already a core dependency, previously only used
by `bll/cli.py`'s render path — `web.py` now calls it directly too). No network call, no
third-party API, no new subprocess type (the existing `Job`/`run_job` subprocess-streaming
machinery is untouched).

---

## Testing Strategy

| Test Type | Scope | Files | Tools | Coverage Goal |
|-----------|-------|-------|-------|----------------|
| Unit/integration (new) | `/watch` HTML shape, video Range behavior, VTT conversion, words regression | `tests/test_web.py` (new) | pytest, `fastapi.testclient.TestClient`, `httpx` | AT-001, AT-002, AT-003, AT-004 |
| Regression (unmodified) | every existing test keeps passing with no edits | `tests/*.py` (floor of 35) | pytest | AT-005 |
| Structural, optional | `_parse_range`'s 416 branch on a malformed/unsatisfiable Range | `tests/test_web.py` | pytest | not AT-gated; included for completeness, matches Decision 4's "not a 500" claim |
| Manual smoke (later phase, AT-006) | real `bll serve`/uvicorn boot, curl each new endpoint | n/a (not this phase) | curl | AT-006 |

### New tests (exact — `tests/test_web.py`, following this repo's existing no-conftest,
`sys.path.insert` import convention seen in every other test file)

```python
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
```

If the DESIGN's test sketches differ slightly in assertions once written against the real
implementation, follow this doc's *intent* (which AT each test targets), consistent with this
repo's own established convention (see `BUILD_REPORT_ISSUE_28_LANG_PLUMBING.md`, Autonomous
Decision #2, on trimming a comment for a `ruff` line-length gate without changing behavior).

### Manual smoke commands (for AT-006 — a later phase's job; endpoints are curl-trivial by design)

```bash
bll serve --port 8000 &
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/watch
curl -s -D - -o /dev/null -H "Range: bytes=0-99" "http://localhost:8000/api/video?path=$VIDEO"
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:8000/api/video?path=$VIDEO"
curl -s "http://localhost:8000/api/vtt?path=$OUT_SRT" | head -5
curl -s "http://localhost:8000/api/words?lang=ja" | head -c 300
```

---

## Error Handling

| Error Type | Handling Strategy | Status |
|------------|-------------------|--------|
| `/api/video`: path is not a file | `HTTPException` | 400 |
| `/api/video`: permission denied reading the file | `HTTPException` (from `PermissionError`) | 403 |
| `/api/video`: malformed or unsatisfiable `Range` header | `HTTPException` (`_parse_range` returns `None`) | 416 |
| `/api/vtt`: path is not a file | `HTTPException` | 400 |
| `/api/vtt`: permission denied | `HTTPException` (from `PermissionError`) | 403 |
| `/api/vtt`: `pysubs2.load()` fails to parse the file | `HTTPException` (caught, re-raised) | 400 |
| `/api/layers`: one or more layers/plan missing | Not an error — `exists: false` per layer (Decision 6) | 200 |
| `/api/layers`: `path` query param missing entirely | FastAPI's own required-param validation | 422 (unchanged framework behavior) |
| `/api/words`: lemma's `lang` scope has no rows | Not an error — empty `words: []` (existing, unchanged) | 200 |

---

## Configuration

| Config Key | Type | Default | Description |
|------------|------|---------|--------------|
| `path` (`/api/video`, `/api/vtt`, `/api/layers`) | `str` | required, no default | Absolute or `~`-expandable filesystem path, normalized via `os.path.abspath(os.path.expanduser(...))` — same convention as `/api/browse`'s `dir` param. |
| `Range` (`/api/video`, request header) | `str` | absent → full body | Standard HTTP `Range: bytes=...` header; single range only (Decision 4). |
| `lang` (`/api/words`, query param) | `str` | `"ja"` | Existing, unchanged since PR #33 — the learned-words panel calls it explicitly as `?lang=ja`. |

---

## Security Considerations

- **No new trust boundary, an extended one.** `/api/browse` already lets the caller enumerate
  arbitrary directories on disk; `/api/process` already accepts arbitrary `ja_sub`/`en_sub` paths
  and spawns a subprocess against them. `/api/video`/`/api/vtt` extend this same
  already-accepted, single-user, localhost-only, no-auth trust model to *serving file content*
  rather than just *listing/consuming* paths. This is a real, deliberate expansion of what's
  exposed (see Decision 4), not an oversight — and is exactly what "watch a local video with
  subtitles" requires in a tool with this trust model. `bll serve` has no authentication anywhere
  today; this design does not change that posture, per Invariant 4.
- **Path normalization, not sandboxing.** `os.path.abspath(os.path.expanduser(...))` (identical to
  `/api/browse`) normalizes paths for consistent error messages and cross-platform `~`
  expansion — it does **not** restrict access to a subtree. This matches the existing convention
  exactly; it is not a new gap relative to `/api/browse`/`/api/process`.
- **No new SQL surface.** None of the four new endpoints touch the database — `/api/words` is the
  only SQL-backed route reachable from this page, unchanged, and already uses parameterized `?`
  placeholders (no string interpolation anywhere in this design, same posture as the lang-plumbing
  design before it).
- **`Range` header parsing is defensive.** `_parse_range` only accepts the exact forms it
  recognizes and returns `None` (→ 416) on anything else — no `eval`, no unchecked slicing past
  file boundaries (`min(end, size - 1)` clamps explicitly).
- **`pysubs2.load()` failures are caught**, not allowed to surface a stack trace via FastAPI's
  default 500 handler — a malformed path can't be used to probe for parser internals beyond a
  generic 400 message.

---

## Observability

Same posture as the rest of this CLI + single-process FastAPI tool: no logging/metrics/tracing
infrastructure exists, and this issue does not add any (`print()`/HTTP status codes remain the only
signals). The one new observable-to-a-human-operator condition is `/api/video`'s 416 response for a
malformed Range header — in practice this should be unreachable in normal use, since browsers only
ever generate well-formed Range headers; it exists as a defensive, non-crashing fallback, not as a
signal anyone is expected to monitor.

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-07-06 | design phase (`agentspec:workflow:design`, executed manually per Method note) | Initial version, from issue #18 + DEFINE_ISSUE_18_WATCH_PAGE.md |

---

## Next Step

**Ready for:** `/build .claude/sdd/features/DESIGN_ISSUE_18_WATCH_PAGE.md`
