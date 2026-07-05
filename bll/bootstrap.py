"""One-shot model provisioning for the aligner backend.

Performs the startup readiness check: find a reachable ollama, make sure
the base model is pulled, and make sure the aligner profile exists - creating it
if not. Used by `bll bootstrap` and by `bll serve` on startup, so a fresh machine
(or a fresh container) becomes ready with no manual `ollama pull` / `ollama create`.

No GPU code here: this only speaks the ollama HTTP API, so it works whether
ollama runs on the host, in a sibling container, or on a remote box.
"""

import json
import os
import time
import urllib.error
import urllib.request

# The aligner base + profile. BASE is the upstream model the profile is built
# FROM; PROFILE is the tuned variant bll calls. Keep PARAMS in sync with
# deploy/bll-align.Modelfile (this module is the source of truth at runtime).
BASE_MODEL = os.environ.get("BLL_BASE_MODEL", "gemma4:12b-it-qat")
PROFILE = os.environ.get("BLL_ALIGN_MODEL", "bll-align")
# No num_gpu -> ollama auto-fits each GPU (portable). See the Modelfile comment.
PROFILE_TEMPLATE = "{{ .Prompt }}"
PROFILE_PARAMS = {"num_ctx": 2048, "temperature": 0, "top_k": 64, "top_p": 0.95}

# Where ollama might be, in priority order, when OLLAMA_URL isn't set. In a
# container, the host is reachable at host.docker.internal (mapped in compose).
URL_CANDIDATES = [
    os.environ.get("OLLAMA_URL"),
    "http://localhost:11434",
    "http://host.docker.internal:11434",
    "http://ollama:11434",  # the sibling service name in compose
]


def _api(url, path, payload=None, timeout=10):
    """Minimal ollama API call (stdlib only, no requests dependency)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def reachable(url, timeout=3):
    try:
        _api(url, "/api/tags", timeout=timeout)
        return True
    except Exception:
        return False


def resolve_url(explicit=None, wait=0):
    """Return the first reachable ollama URL (probing the candidates), or raise.
    `wait` seconds lets a just-started sibling ollama come up."""
    cands = [explicit] if explicit else []
    cands += [c for c in URL_CANDIDATES if c]
    deadline = time.monotonic() + wait
    while True:
        for u in cands:
            if u and reachable(u):
                return u
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    raise RuntimeError(
        "no reachable ollama found. Install it (https://ollama.com) and "
        "`ollama serve`, or set OLLAMA_URL. Tried: " + ", ".join(c for c in cands if c)
    )


def _tags(url):
    models = _api(url, "/api/tags").get("models", [])
    # match with or without an explicit :latest / :tag
    names = set()
    for m in models:
        names.add(m["name"])
        names.add(m["name"].split(":")[0])
    return names


def _pull(url, model, log=print):
    log(f"  pulling {model} (one-time large download)...")
    # streamed to report progress and avoid timing out on a multi-GB pull
    data = json.dumps({"model": model, "stream": True}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/api/pull", data=data, headers={"Content-Type": "application/json"}
    )
    last = ""
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            if ev.get("error"):
                raise RuntimeError(f"pull failed: {ev['error']}")
            status = ev.get("status", "")
            if status and status != last:
                log(f"    {status}")
                last = status


def _create_profile(url, log=print):
    log(f"  creating aligner profile {PROFILE} from {BASE_MODEL}...")
    # ollama >= 0.x /api/create: from/template/parameters (no Modelfile string)
    payload = {
        "model": PROFILE,
        "from": BASE_MODEL,
        "template": PROFILE_TEMPLATE,
        "parameters": PROFILE_PARAMS,
        "stream": False,
    }
    _api(url, "/api/create", payload, timeout=120)


def ensure_ready(url=None, wait=0, log=print):
    """Make the aligner usable and return the resolved ollama URL.
      1. find a reachable ollama (reuse the host's if it's there)
      2. pull the base model if missing
      3. create the tuned profile if missing
    Idempotent: a fully-provisioned machine returns instantly."""
    url = resolve_url(url, wait=wait)
    log(f"ollama: using {url}")
    names = _tags(url)
    if PROFILE in names or PROFILE.split(":")[0] in names:
        log(f"  aligner profile {PROFILE} ready.")
        return url
    if BASE_MODEL not in names and BASE_MODEL.split(":")[0] not in names:
        _pull(url, BASE_MODEL, log=log)
    _create_profile(url, log=log)
    log(f"  aligner profile {PROFILE} ready.")
    return url
