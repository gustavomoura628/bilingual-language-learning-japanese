"""Gloss selected words and align them to the exact English word to replace.

Uses one `claude -p` call per episode. The model sees each occurrence's
Japanese line and the overlapping English line, and must return, per
occurrence, the exact substring of the English line that translates the word
(or null if the English line doesn't contain a clean equivalent).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

PROMPT_HEADER = """\
You are a Japanese-English subtitle alignment assistant for a language-learning tool.
The tool replaces one English word in a subtitle with the Japanese word it translates.

For each Japanese word below you are given numbered occurrences. Each occurrence has:
- ja: the Japanese subtitle line containing the word
- en: the English subtitle line shown at the same time

Tasks:
1. Give a short English gloss for the word (1-3 words, dictionary-style, lowercase).
2. Give the word's natural hiragana reading as spoken in these lines (e.g. 明日 is
   usually あした in conversation, not あす). The reading shown in parentheses after
   each word is the dictionary's guess; correct it if a different reading is more
   natural here.
3. For EACH occurrence, find the exact contiguous substring of the "en" line that
   expresses that Japanese word in this context (e.g. for 約束 in "I made a promise",
   answer "promise"; a conjugated/inflected form like "promised" or "friends" is fine
   and preferred when that is what the line contains). The substring must appear
   verbatim in the en line, with the same capitalization. Prefer a single word;
   a short phrase (max 3 words) is acceptable. If the en line has no clean
   equivalent (free translation, word dropped), answer null. Never pick a word that
   actually translates a DIFFERENT Japanese word in the line.
4. IMPORTANT: if the "word" is really a fragment of a proper noun in these lines
   (e.g. 西 extracted from the surname 西住, or 道 from 戦車道 used as an art's name),
   or its only English counterpart is a person/place/organization name, do NOT
   align it: set "skip": true and leave matches empty. A learner must never see a
   character's name replaced by a vocabulary word. Likewise skip if the tool
   mis-segmented and this is not a real standalone word in context.
5. Never align to a romanized Japanese term the translators left untranslated
   (e.g. "Senshado", "senpai", "-chan"): that is not an English translation.
   And if in an occurrence the word is actually part of a longer Japanese
   compound (e.g. 戦車 inside 戦車道), answer null for THAT occurrence -- only
   align occurrences where the word stands alone with its own meaning.

Return ONLY a JSON object, no markdown fences, exactly this shape:
{"words": [{"lemma": "...", "gloss": "...", "reading": "...", "skip": false, "matches": [{"id": 1,\
 "en_word": "..." or null}]}]}

Words:
"""


# Lean header for small local models. The big header's compound/proper-noun
# rules cite specific compounds and surnames as examples, and a small (~12B)
# model latches onto those examples and skips those very words. Those hazards
# are handled mechanically now (compound-internal occurrence filter + JMdict
# conjunction veto), so the local model only needs the core task.
LEAN_HEADER = """\
You match Japanese words to the English words that translate them in subtitles.
Each occurrence has the Japanese line (ja) and the English text shown at the
same time (en).

For EACH occurrence id: answer with the exact substring of the en text that
expresses the Japanese word in that context. Copy it verbatim including
capitalization; an inflected form ("tanks", "promised") is good. If the en
text has no direct equivalent, answer null. Never answer with a person's
name or an untranslated romanized Japanese term.
Also give a short lowercase English gloss (1-3 words) and the word's natural
hiragana reading as spoken in these lines.

Return ONLY JSON, exactly:
{"words": [{"lemma": "...", "gloss": "...", "reading": "...", "skip": false, "matches": [{"id": 1,\
 "en_word": "..." or null}]}]}
Set "skip": true ONLY if the word is a mis-segmented fragment, not a real word.

Words:
"""


def build_prompt(entries: list[dict[str, Any]], header: str | None = None) -> str:
    """entries: list of {lemma, reading, occurrences:[{id, ja, en}]}"""
    parts = [header or PROMPT_HEADER]
    for e in entries:
        parts.append(f"\n### {e['lemma']} ({e['reading']})")
        for occ in e["occurrences"]:
            parts.append(f"- id {occ['id']}: ja: {occ['ja']}")
            parts.append(f"         en: {occ['en']}")
    return "\n".join(parts)


def _extract_json(text: str) -> Any:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in model output:\n{text[:500]}")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text[start:])
    return obj


def run_claude(prompt: str, model: str | None = None, timeout: int = 600) -> str:
    cmd = ["claude", "-p"]
    if model:
        cmd += ["--model", model]
    try:
        res = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(
            "`claude` CLI not found. Install Claude Code, or pass --gloss-json "
            "with a manually prepared gloss/alignment file."
        ) from None
    if res.returncode != 0:
        raise RuntimeError(f"claude -p failed ({res.returncode}): {res.stderr[:500]}")
    return res.stdout


def run_ollama(
    prompt: str,
    model: str | None,
    url: str = "http://localhost:11434",
    timeout: int = 120,
    think: bool = False,
    stats: list[dict[str, Any]] | None = None,
    temperature: float = 0,
) -> str:
    """One chat call. Normal chunk latency is 8-15s; a long hang is a
    transport stall (a call can hang past the timeout under sustained load),
    so fail fast and retry the transport once. Persistent failure still raises
    rather than degrading silently.

    Context-overflow guard: a small num_ctx (default 2048) is used because the
    aligner-tuned model fits more layers on the GPU at small ctx. A small ctx is
    fast but a prompt/answer that exceeds it would be SILENTLY truncated ->
    wrong/dropped alignment -> zero-FP violation. So if the prompt nears the
    window or the answer is cut (done_reason == 'length'), num_ctx is doubled and
    the call retried until it fits (capped at OLLAMA_MAX_CTX). The common small
    chunk never pays; only a rare big chunk triggers one reload at a larger ctx."""
    import os
    import time
    import urllib.request

    base_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "2048"))
    max_ctx = int(os.environ.get("OLLAMA_MAX_CTX", "16384"))
    ANSWER_HEADROOM = 256  # tokens reserved for the JSON answer
    if think:
        timeout = max(timeout, 600)  # thinking runs ~15x longer
    t0 = time.time()
    num_ctx = base_ctx
    while True:
        opts = {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": int(os.environ.get("OLLAMA_NUM_PREDICT", "1024")),
        }
        if num_ctx > base_ctx:
            # grown ctx => bigger KV; shed GPU layers so the aligner-tuned
            # num_gpu (maxed at base ctx) doesn't OOM on the grow-retry. Slower,
            # but the grow path is rare and correctness beats speed here.
            opts["num_gpu"] = int(os.environ.get("OLLAMA_GROW_NUM_GPU", "28"))
        body = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json",
                "think": think,
                "options": opts,
            }
        ).encode()
        last = None
        for attempt in (1, 2):
            req = urllib.request.Request(
                f"{url}/api/chat",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    resp = json.load(r)
                break
            except Exception as e:
                last = e
                if attempt == 1:
                    print(f"  ! ollama transport error ({e}); retrying once...")
                    time.sleep(3)
        else:
            raise RuntimeError(f"ollama call failed ({model} at {url}): {last}")
        # overflow guard: grow ONLY for real PROMPT overflow (prompt + answer
        # headroom exceeds the window) -> the prompt would be silently truncated.
        # A done_reason=='length' with a small prompt is RUNAWAY GENERATION (some
        # small models don't stop), now bounded by num_predict; growing ctx there
        # just feeds the runaway into a timeout, so we let parse-retry/skip handle
        # the truncated answer instead.
        pe = resp.get("prompt_eval_count", 0)
        if pe + ANSWER_HEADROOM > num_ctx and num_ctx < max_ctx:
            new_ctx = min(num_ctx * 2, max_ctx)
            print(
                f"  ! prompt near context limit (prompt={pe} tok, "
                f"ctx={num_ctx}) -> retrying at num_ctx={new_ctx}"
            )
            num_ctx = new_ctx
            continue
        break
    if stats is not None:
        stats.append(
            {
                "dt": time.time() - t0,
                "out_tokens": resp.get("eval_count", 0),
                "in_tokens": resp.get("prompt_eval_count", 0),
                "eval_ns": resp.get("eval_duration", 0),
                "prompt_ns": resp.get("prompt_eval_duration", 0),
                "load_ns": resp.get("load_duration", 0),
                "num_ctx": num_ctx,
            }
        )
    return resp["message"]["content"]


OLLAMA_CHUNK = 10  # small batches: local models lazy-null on long tails


def gloss_and_align(
    entries: list[dict[str, Any]],
    model: str | None = None,
    backend: str = "ollama",
    ollama_url: str = "http://localhost:11434",
    think: bool = False,
) -> dict[str, dict[str, Any]]:
    """Returns dict lemma -> {gloss, reading, matches: {id: en_word|None}}.

    claude backend: one batch call for the whole episode.
    ollama backend: one call per word per chunk of <=OLLAMA_CHUNK occurrences
    (small models stay accurate on small batches; results are merged).

    Defaults to the local 'ollama' path: a caller that omits `backend` must
    never silently spend paid tokens on the 'claude' path.
    """
    if backend == "claude":
        raw = run_claude(build_prompt(entries), model=model)
        return parse_result(_extract_json(raw))

    if backend != "ollama":
        raise ValueError(f"unknown backend: {backend}")

    stats: list[dict[str, Any]] = []
    out: dict[str, dict[str, Any]] = {}
    for e in entries:
        occs = e["occurrences"]
        merged = None
        for i in range(0, len(occs), OLLAMA_CHUNK):
            chunk_entry = {**e, "occurrences": occs[i : i + OLLAMA_CHUNK]}
            prompt = build_prompt([chunk_entry], header=LEAN_HEADER)
            part = None
            for _attempt, temp in enumerate((0, 0.4)):  # retry w/ temp nudge:
                raw = run_ollama(
                    prompt, model=model, url=ollama_url, think=think, stats=stats, temperature=temp
                )
                try:
                    part = parse_result(_extract_json(raw))
                    break
                except (ValueError, KeyError) as exc:
                    err = exc
            if part is None:
                print(
                    f"  ! {e['lemma']} chunk {i // OLLAMA_CHUNK}: unparseable after retry ({err})"
                )
                continue
            info = part.get(e["lemma"])
            if info is None:  # model said skip for this chunk
                merged = None
                break
            if merged is None:
                merged = info
            else:
                merged["matches"].update(info["matches"])
        if merged is not None:
            out[e["lemma"]] = merged

    # Variance squeeze: a word whose matches came back ALL null is often a
    # sampling fluke (the same word aligns fine in another run). Re-ask once at
    # a different temperature; this only fills nulls, and the dictionary veto
    # still applies downstream - recall-only, FP-safe.
    for e in entries:
        info = out.get(e["lemma"])
        if info is None or any(info["matches"].values()):
            continue
        occs = e["occurrences"][:OLLAMA_CHUNK]  # one chunk is enough signal
        raw = run_ollama(
            build_prompt([{**e, "occurrences": occs}], header=LEAN_HEADER),
            model=model,
            url=ollama_url,
            think=think,
            stats=stats,
            temperature=0.45,
        )
        try:
            part = parse_result(_extract_json(raw))
        except (ValueError, KeyError):
            continue
        retry = part.get(e["lemma"])
        if retry:
            for oid, m in retry["matches"].items():
                if m and not info["matches"].get(oid):
                    info["matches"][oid] = m
    if stats:
        total = sum(s["dt"] for s in stats)
        toks = sum(s["out_tokens"] for s in stats)
        in_toks = sum(s["in_tokens"] for s in stats)
        gen_ns = sum(s.get("eval_ns", 0) for s in stats)
        pp_ns = sum(s.get("prompt_ns", 0) for s in stats)
        gen_rate = toks / (gen_ns / 1e9) if gen_ns else 0
        pp_rate = in_toks / (pp_ns / 1e9) if pp_ns else 0
        ctx = stats[-1].get("num_ctx", "?")
        print(
            f"  ollama: {len(stats)} calls, {total:.0f}s wall, "
            f"{total / len(stats):.1f}s/call | "
            f"gen {gen_rate:.1f} tok/s ({toks} out), "
            f"prompt {pp_rate:.0f} tok/s ({in_toks} in), ctx={ctx}"
            f"{' (think)' if think else ''}"
        )
        # machine-readable line for the bench harness to grep
        if os.environ.get("BLL_BENCH"):
            print(
                f"BENCHSTATS calls={len(stats)} wall={total:.2f} "
                f"gen_tok_s={gen_rate:.2f} prompt_tok_s={pp_rate:.2f} "
                f"out_tok={toks} in_tok={in_toks} ctx={ctx}"
            )
    return out


def parse_result(data: Any) -> dict[str, dict[str, Any]]:
    out = {}
    for w in data.get("words", []):
        if not isinstance(w, dict) or not w.get("lemma") or w.get("skip"):
            continue
        out[w["lemma"]] = {
            "gloss": w.get("gloss", ""),
            "reading": w.get("reading") or None,
            # lenient: skip malformed match entries (missing id etc.) rather
            # than failing the whole chunk - small models garble occasionally
            "matches": {
                m["id"]: m.get("en_word")
                for m in w.get("matches", [])
                if isinstance(m, dict) and isinstance(m.get("id"), int)
            },
        }
    return out
