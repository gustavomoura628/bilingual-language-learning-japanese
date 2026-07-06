"""bll command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import typing
from typing import Any

import pysubs2

from . import db as dbm
from . import gloss as glossm
from . import jmdict, jp

# ---------------------------------------------------------------- helpers


def plaintext(event):
    return event.plaintext.replace("\n", " ").strip()


def overlapping(en_events, start, end, slack=1200):
    """Indexes of English events overlapping [start, end] ms.

    If none overlap (slightly offset subtitles, a known cause of missed swaps),
    fall back to the nearest event within `slack` ms."""
    hits = [i for i, ev in enumerate(en_events) if max(ev.start, start) < min(ev.end, end)]
    if hits:
        return hits
    best, best_gap = None, slack + 1
    for i, ev in enumerate(en_events):
        gap = max(ev.start - end, start - ev.end)
        if 0 <= gap < best_gap:
            best_gap, best = gap, i
    return [best] if best is not None else []


def injectable_counts(ja_subs, en_subs, words, jm):
    """For each candidate word, how many of its JA occurrences land on an
    EN cue that contains a canonical JMdict gloss token. No LLM is used: this
    is a fast mechanical proxy for how many occurrences will actually inject,
    which is what drives consolidation. It is a better selection signal than
    raw occurrence count: a word may appear 40x while its English ("fast/early")
    is rarely in the line, so it injects only a handful of times and can never
    consolidate. Injectable count exposes that; occurrence count hides it."""
    en_plain = [plaintext(ev) for ev in en_subs]
    en_toksets = [None] * len(en_subs)  # lazily tokenized EN cues

    def toks(ei):
        if en_toksets[ei] is None:
            s = set()
            for t in re.findall(r"[A-Za-z']+", en_plain[ei].lower()):
                s.add(t)
                s.add(jmdict._lemmatize(t))
            en_toksets[ei] = s
        return en_toksets[ei]

    out = {}
    for lemma, w in words.items():
        ent = jm.get(lemma) if jm else None
        if not ent:
            out[lemma] = 0
            continue
        g = jmdict._gloss_tokens(lemma, ent)
        c = 0
        for ci in w["cues"]:
            ev = ja_subs[ci]
            if any(g & toks(ei) for ei in overlapping(en_subs, ev.start, ev.end)):
                c += 1
        out[lemma] = c
    return out


def _en_counterpart(ja_path, season_dir):
    """Find the EN subtitle sibling of a JA file (foo.ja.srt -> foo.en.*)."""
    import glob

    base = os.path.basename(ja_path)
    stem = base.split(".ja.")[0] if ".ja." in base else os.path.splitext(base)[0]
    for c in sorted(glob.glob(os.path.join(season_dir, stem + ".en.*"))):
        return c
    return None


_kana_only_re = re.compile(r"^[぀-ゟ゠-ヿー]+$")


def fmt_injection(inj, layer, threshold, decay=None, romaji=False):
    """Render one plan injection for a layer.

    plain    -> 約束
    kana     -> 約束 (やくそく)      [romaji=True: 約束 (yakusoku)]
    answers  -> like kana (every reading shown); the maximal-help "all answers"
                layer. render_plan also gives it every note, uncapped.
    adaptive -> reading while still being learned, bare once consolidated.
                A word counts as "still learning" if its lifetime exposures
                are below `threshold` (live-counted) or it has gone stale,
                meaning not seen in `decay` tokens of content (a forgetting
                curve), so a faded word resurfaces as a refresher (e.g. after
                switching to a new show).

    romaji=True annotates with romaji instead of kana - for learners who don't
    read kana yet. Kana-only words (すごい), which render bare in kana mode
    (annotating すごい(すごい) is pointless), DO get annotated in romaji mode
    (すごい (sugoi)) since bare kana is unreadable to a romaji learner.
    """
    bare = inj["lemma"]
    if _kana_only_re.match(bare) and not romaji:
        return bare
    reading = jp.to_romaji(inj["reading"]) if romaji else inj["reading"]
    annotated = f"{bare} ({reading})"
    if layer == "plain":
        return bare
    if layer in ("kana", "answers"):
        return annotated
    learning = inj["exposure_before"] < threshold
    stale = decay is not None and inj.get("recency_gap", 0) >= decay
    return annotated if (learning or stale) else bare


LAYERS = ("plain", "kana", "adaptive", "answers")


def layer_paths(out):
    """adaptive keeps the given name; siblings get .plain/.kana/.answers infixes."""
    base, ext = os.path.splitext(out)
    return {
        "adaptive": out,
        "plain": f"{base}.plain{ext}",
        "kana": f"{base}.kana{ext}",
        "answers": f"{base}.answers{ext}",
    }


def _note_width(s):
    """Rough on-screen width: CJK/full-width glyphs count as 2, the rest as 1."""
    return sum(2 if ord(c) > 0x2E7F else 1 for c in s)


def _wrap_note(text, width=30):
    """Break a translator note into \\N-separated lines so a long one wraps at the
    top of the screen instead of running off the right edge. Breaks on spaces."""
    lines, cur = [], []
    for w in text.split(" "):
        if cur and _note_width(" ".join(cur + [w])) > width:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    return r"\N".join(lines)


def render_plan(
    en_path, plan, out, threshold, decay=None, romaji=False, notes=None, note_threshold=5
):
    """Render the subtitle layers from an injection plan.
    Returns {layer: path}. Injections are applied in plan order so repeated
    words in one cue resolve identically across layers.

    romaji=True annotates in romaji instead of kana. notes (dict lemma->str)
    adds a top-positioned ({\\an8}) translator-note line while a word is still
    new: lifetime exposures below `note_threshold` (the same exposure counter
    the kana fade uses, just an earlier cutoff). So one ramp: note+reading ->
    reading -> bare. The 'answers' layer ignores the cutoff and shows every
    note.
    """
    paths = layer_paths(out)
    for layer, path in paths.items():
        subs = pysubs2.load(en_path)
        for inj in plan:
            lemma = inj["lemma"]
            repl = fmt_injection(inj, layer, threshold, decay, romaji)
            ok = replace_in_event(subs[inj["cue"]], inj["en_word"], repl)
            if not ok:  # cannot happen if plan matches en_path
                raise RuntimeError(f"plan/en mismatch: {inj} not applicable to {en_path}")
            # Note fires while the word is still new (lifetime exposures below
            # note_threshold) on any layer that shows its reading; the answers
            # layer shows every note. repl != lemma ensures it's actually
            # annotated here (covers kana-only words / faded adaptive).
            show = layer == "answers" or inj["exposure_before"] < note_threshold
            # Per-injection note (sense-first, heteronym-aware) wins; otherwise
            # fall back to the lemma-keyed map baked into the plan.
            note_text = inj.get("note") or (notes.get(lemma) if notes else None)
            # repeat entries are the same exposure already noted by the primary
            # injection in this cue -- don't stack a second {\an8} note line.
            if note_text and layer != "plain" and repl != lemma and show and not inj.get("repeat"):
                ev = subs[inj["cue"]]
                subs.append(
                    pysubs2.SSAEvent(
                        start=ev.start, end=ev.end, text=r"{\an8\i1}" + _wrap_note(note_text)
                    )
                )
        subs.save(path)
    return paths


def parse_show_episode(path):
    """Best-effort (show, episode_no) from a subtitle filename. Strips release
    tags ([Group Name], (1080p)...), finds the episode marker, and takes the
    title before it as the show - e.g. "[Group] A Show - 04.ja.ass" ->
    ("A Show", "04"). Falls back to the parent folder when the name carries no
    title. Both are editable in the UI / overridable with --show/--episode."""
    base = os.path.basename(path)
    stem = re.sub(r"\.(ja|en)\..+$", "", base)  # drop .ja./.en.<ext>
    stem = re.sub(r"\.(srt|ass|ssa|vtt|bll)$", "", stem)  # or a bare extension
    clean = re.sub(r"[\[(][^\])]*[\])]", " ", stem)  # drop [..] and (..) tags
    clean = re.sub(r"[._]+", " ", clean)
    m = (
        re.search(r"s\d+\s*e\s*0*(\d+)", clean, re.I)
        or re.search(r"(?:\bep|\bepisode|[-–—]\s*|\bx|\be)\s*0*(\d+)\b", clean, re.I)
        or re.search(r"\b0*(\d{1,3})\b", clean)
    )
    episode_no = m.group(1) if m else None
    title = clean[: m.start()] if m else clean
    title = re.sub(r"\s+", " ", title).strip(" -–—_")
    show = title or os.path.basename(os.path.dirname(os.path.abspath(path))) or None
    return show, episode_no


def load_notes(path):
    """Load a translator-notes file: JSON dict of lemma -> explanation."""
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def default_note(lemma, kana, romaji, gloss, use_romaji=True):
    """The auto-generated translator note for a word, in the same convention as
    the on-screen injection: "kanji (reading) = meaning" -- e.g.
    "魔女 (majo) = witch" (romaji config) or "魔女 (まじょ) = witch" (kana config).
    Built mechanically from data the DB already stores - no model, no human in
    the loop. Returns None if there's no gloss to show."""
    if not gloss:
        return None
    reading = romaji if use_romaji else kana
    if reading and reading != lemma:
        return f"{lemma} ({reading}) = {gloss}"
    return f"{lemma} = {gloss}"  # kana-only word (no distinct reading to show)


def resolve_notes(conn, lemmas, file_notes=None, use_romaji=True):
    """Build the lemma->note map used by the renderer. Precedence, low to high:
      1. auto-generated default ("kanji (reading) = gloss") for every word with
         a gloss; reading is romaji or kana per `use_romaji` (matches config)
      2. the per-word operator override (words.note column), edited in the UI/CLI
      3. an explicit --notes file (bulk import / one-off override)
    `lemmas` limits the map to the words actually in the plan."""
    want = set(lemmas)
    notes = {}
    for r in conn.execute("SELECT lemma, reading, romaji, gloss, note FROM words"):
        if r["lemma"] not in want:
            continue
        n = r["note"] or default_note(r["lemma"], r["reading"], r["romaji"], r["gloss"], use_romaji)
        if n:
            notes[r["lemma"]] = n
    if file_notes:
        notes.update({k: v for k, v in file_notes.items() if k in want})
    return notes


def apply_sense_first(injections, jm, conn=None, file_notes=None, use_romaji=False):
    """Per-injection heteronym disambiguation, display only.

    For a genuine heteronym - a surface with more than one distinct primary
    reading (角 -> かく/かど/つの/すみ) - pick the reading of the JMdict sense the
    injected English word maps to, and re-derive that injection's note from the
    same sense. So 角 reads つの ("角 (tsuno) = horn; antler") over 'horn' but
    stays かど over 'corner'. Acts only when the reading actually flips and one
    sense clearly wins; every other injection is left byte-identical, so the
    change is confined to exactly the heteronyms that were wrong.

    Note precedence mirrors resolve_notes (--notes file > operator override >
    auto sense-note). Mutates injections in place; returns how many it changed.

    A deeper fix is left for later: 角(つの) "horn" and 角(かど) "corner" are
    really distinct vocab items sharing a kanji, yet they still share one DB
    row and exposure counter here. Splitting per-sense is the principled fix
    for when a heteronym's other reading shows up for real and the shared
    lifecycle mismatches."""
    overrides = {}
    if conn is not None:
        for r in conn.execute("SELECT lemma, note FROM words WHERE note IS NOT NULL"):
            overrides[r["lemma"]] = r["note"]
    file_notes = file_notes or {}
    changed = 0
    for inj in injections:
        lemma = inj["lemma"]
        ent = jm.get(lemma) if jm else None
        if not ent or len({e["r"][0] for e in ent}) < 2:
            continue  # single reading -> nothing to disambiguate
        e = jmdict.entry_for_en(lemma, ent, inj["en_word"])
        if not e:
            continue  # ambiguous / no gloss match -> keep existing reading
        reading = e["r"][0]
        if reading == inj.get("reading"):
            continue  # sense agrees with the stored reading -> no-op
        inj["reading"] = reading
        if lemma in file_notes:
            note = file_notes[lemma]
        elif lemma in overrides:
            note = overrides[lemma]
        else:
            note = default_note(
                lemma,
                reading,
                jp.to_romaji(reading),
                jmdict.canonical_gloss([e], reading=reading),
                use_romaji,
            )
        if note:
            inj["note"] = note
        changed += 1
    return changed


def replace_in_event(event, en_word, replacement):
    """Replace first standalone occurrence of en_word in event text.

    Tries the raw text first (preserves styling tags), falls back to
    plaintext if tags break the match. Returns True on success.
    """

    def attempt(text, flags=0):
        pat = re.compile(r"(?<![A-Za-z])" + re.escape(en_word) + r"(?![A-Za-z])", flags)
        return pat.subn(replacement.replace("\\", "\\\\"), text, count=1)

    new, n = attempt(event.text)
    if not n:
        new, n = attempt(event.text, re.IGNORECASE)
    if not n:
        plain = event.plaintext
        new_plain, n = attempt(plain)
        if not n:
            new_plain, n = attempt(plain, re.IGNORECASE)
        if n:
            event.plaintext = new_plain
            return True
        return False
    event.text = new
    return True


# ---------------------------------------------------------------- process


@typing.no_type_check
def cmd_process(args: argparse.Namespace) -> int:
    if args.backend == "ollama" and not args.model:
        # The aligner-tuned profile (ctx2048, temp0; see deploy/bll-align.Modelfile).
        # Default name is portable ("bll-align", auto-fits any GPU); override with
        # BLL_ALIGN_MODEL (e.g. a hand-tuned num_gpu variant like gemma4-align).
        args.model = os.environ.get("BLL_ALIGN_MODEL", "bll-align")
    if not args.dry_run:
        dbm.backup(args.db)  # the DB is canon; snapshot before mutating
    conn = dbm.connect(args.db)
    ja_subs = pysubs2.load(args.ja_sub)
    en_subs = pysubs2.load(args.en_sub)
    episode = os.path.basename(args.ja_sub)

    # JMdict canonical-pair dictionary (conjunction filter; --no-dict to
    # skip). Merged view: common subset + full-dict compounds. --dict-json
    # loads a committed offline fixture instead (deterministic testing).
    jm = None
    if not args.no_dict:
        jm = jmdict.load_merged(fixture_path=args.dict_json)  # auto-downloads unless --dict-json

    db_words = dbm.all_words(conn)
    known = {lemma for lemma, r in db_words.items() if r["status"] in ("known", "ignored")}
    learning = {lemma for lemma, r in db_words.items() if r["status"] == "learning"}

    ja_lines = [jp.clean_sdh(plaintext(ev)) for ev in ja_subs]
    # jm enables compound merging; keep=learning so already-learned
    # words bypass the compound-fragment filter and inject everywhere they
    # legitimately appear (elided-particle adjacency etc.).
    words = jp.analyze(ja_lines, jm=jm, keep=learning)

    # Forgetting clock: position before this file (cumulative JA tokens
    # of content watched so far) and this file's token contribution.
    clock_before = dbm.clock(conn)
    ep_tokens = jp.count_tokens(ja_lines)

    # Words still being learned that appear in this episode -> always inject.
    active = [words[lemma] for lemma in learning if lemma in words]
    for w in active:  # prefer DB reading/romaji (may have been LLM-corrected)
        row = db_words[w["lemma"]]
        if row["reading"]:
            w["reading"], w["romaji"] = row["reading"], row["romaji"]

    # Load-gated introduction. Incidental acquisition is weak (under ~20%
    # of words learned per exposure-set; words need ~12-20 encounters), so
    # the limiter is the number of still-fuzzy words demanding attention at
    # once, not a fixed words/episode rate. active_load = learning words
    # appearing this episode that are still below --learning-threshold
    # exposures (i.e. still annotated, still being processed). A new word is
    # admitted only if that on-screen unconsolidated set has room under
    # --max-active. Binge-robust (per-episode content, not per-time),
    # self-pacing (room reopens as words consolidate), no stall (dormant
    # words don't appear, so don't block).
    active_load = sum(
        1 for w in active if db_words[w["lemma"]]["exposures"] < args.learning_threshold
    )
    # Per-episode introduction cap. Defaults to --max-active so the load gate
    # is the sole limiter: freed slots can be refilled the same episode, which
    # is what lets value-density scheduling raise throughput. Set --new-words
    # explicitly to also cap how many can enter in one episode.
    per_ep_cap = args.new_words if args.new_words is not None else args.max_active
    new_budget = max(0, args.max_active - active_load)
    new_budget = min(per_ep_cap, new_budget)
    if active_load >= args.max_active:
        print(
            f"Pace gate: {active_load} unconsolidated words already on "
            f"screen (cap {args.max_active}); 0 new admitted."
        )
    # Injection-aware counts for this episode: the number of occurrences
    # that will actually inject (overlapping EN cue has a canonical gloss),
    # used as the selection signal instead of raw occurrences. No LLM.
    inj_now = {} if args.no_dict else injectable_counts(ja_subs, en_subs, words, jm)
    for lemma, w in words.items():
        w["inj"] = inj_now.get(lemma, w["count"])

    # Season lookahead. Build the per-future-episode trajectory (in order,
    # after this file) in injectable terms so time-to-consolidate reflects
    # real injection speed, not phantom occurrence frequency.
    future = None  # flat rest-of-season injectable totals (--pick)
    future_traj = None  # lemma -> [injectable per future episode]
    if args.season_dir:
        import glob

        sibs = sorted(glob.glob(os.path.join(args.season_dir, "*.ja.*")))
        cur = os.path.abspath(args.ja_sub)
        after, seen = [], False
        for p in sibs:
            if os.path.abspath(p) == cur:
                seen = True
            elif seen:
                after.append(p)
        if not seen:  # current file not under season_dir -> all are "future"
            after = [p for p in sibs if os.path.abspath(p) != cur]
        future, future_traj = {}, {}
        for i, p in enumerate(after):
            try:
                fja = pysubs2.load(p)
            except Exception:
                continue
            flines = [jp.clean_sdh(plaintext(ev)) for ev in fja]
            fwords = jp.analyze(flines, jm=jm)
            # injectable needs the future EN sub; fall back to occurrences
            enp = _en_counterpart(p, args.season_dir)
            finj = None
            if enp and not args.no_dict:
                try:
                    finj = injectable_counts(fja, pysubs2.load(enp), fwords, jm)
                except Exception:
                    finj = None
            for lemma, info in fwords.items():
                c = finj.get(lemma, 0) if finj is not None else info["count"]
                future[lemma] = future.get(lemma, 0) + c
                future_traj.setdefault(lemma, [0] * len(after))[i] = c
        print(f"Lookahead: {len(after)} future episodes (injection-aware value-density)")

    # Fresh picks: oversample 2x, because some candidates will turn out to be
    # tokenization artifacts or unalignable; the best n that align are kept.
    # new_budget caps how many are actually kept.
    pool = new_budget if args.dry_run else max(new_budget * 2, new_budget + 2)
    allowed_pos = {jp.POS_MAP[p.strip()] for p in args.include_pos.split(",")}
    new = (
        jp.select_new(
            words,
            exclude=known | learning,
            n=pool,
            min_count=args.min_count,
            min_zipf=args.min_zipf,
            allowed_pos=allowed_pos,
            future=future,
            future_traj=future_traj,
            load_threshold=args.learning_threshold,
            count_field=("count" if args.no_dict else "inj"),
        )
        if new_budget
        else []
    )
    # New words must exist in the curated dictionary at all - a lemma JMdict
    # has never heard of is usually a tokenizer artifact (a fragment split off
    # a proper noun).
    if jm is not None:
        no_entry = [w["lemma"] for w in new if w["lemma"] not in jm]
        if no_entry:
            print(f"Skipped (not in JMdict): {', '.join(no_entry)}")
        new = [w for w in new if w["lemma"] in jm]

    # Surface ignored words the scheduler WOULD have picked this episode, so the
    # curator can reconsider (e.g. a character name suppressed too aggressively,
    # or one you want back). Re-runs selection with ignored words allowed in.
    if jm is not None and new_budget:
        ignored = {lemma for lemma, r in db_words.items() if r["status"] == "ignored"}
        known_only = {lemma for lemma, r in db_words.items() if r["status"] == "known"}
        if ignored:
            would = jp.select_new(
                words,
                exclude=known_only | learning,
                n=new_budget,
                min_count=args.min_count,
                min_zipf=args.min_zipf,
                allowed_pos=allowed_pos,
                future=future,
                future_traj=future_traj,
                load_threshold=args.learning_threshold,
                count_field=("count" if args.no_dict else "inj"),
            )
            hit = [w for w in would if w["lemma"] in ignored and w["lemma"] in jm]
            if hit:
                tags = ", ".join(f"{w['lemma']} (x{w.get('inj', w['count'])})" for w in hit)
                print(
                    f"IGNORED WORDS (would be picked — override with `bll learning <word>`): {tags}"
                )

    # Interactive pick: the learner chooses which candidates to learn
    # (interest predicts retention). Numbered list; empty input keeps the
    # automatic top-n.
    if args.pick and new:
        print(f"\nCandidates (pick numbers, empty = auto top {new_budget}):")
        for i, w in enumerate(new, 1):
            fnote = f", season x{future[w['lemma']]}" if future else ""
            print(
                f"  {i}. {w['lemma']} ({w['reading']})  "
                f"x{w['count']} this episode{fnote}, zipf {w['zipf']:.1f}"
            )
        try:
            raw = input("> ").strip()
        except EOFError:
            raw = ""
        if raw:
            idxs = [int(t) for t in re.findall(r"\d+", raw)]
            new = [new[i - 1] for i in idxs if 1 <= i <= len(new)]
            new_budget = len(new)  # explicit user choice overrides the gate
    selected = active + new
    if not selected:
        print("No suitable words found (try lowering --min-count / --min-zipf).")
        return 0

    # Build occurrences: one per (word, JA cue), with all time-overlapping
    # EN cues concatenated into a single en text (releases that split EN
    # lines would otherwise multiply occurrences several-fold; concatenating
    # EN for matching avoids that).
    occ_id = 0
    entries = []  # for the gloss/alignment prompt
    occurrences = {}  # id -> (lemma, tuple of en idxs)
    occ_text = {}  # id -> (ja_line, en_text), for the align cache
    for w in selected:
        occs = []
        seen = set()
        for ci in sorted(w["cues"]):
            ja_ev = ja_subs[ci]
            ens = tuple(overlapping(en_subs, ja_ev.start, ja_ev.end))
            if not ens or ens in seen:
                continue
            seen.add(ens)
            occ_id += 1
            occs.append(
                {
                    "id": occ_id,
                    "ja": ja_lines[ci],
                    "en": " ".join(plaintext(en_subs[ei]) for ei in ens),
                }
            )
            occurrences[occ_id] = (w["lemma"], ens)
            occ_text[occ_id] = (occs[-1]["ja"], occs[-1]["en"])
        if occs:
            entries.append({"lemma": w["lemma"], "reading": w["reading"], "occurrences": occs})
    sel_by_lemma = {w["lemma"]: w for w in selected}
    entries = [e for e in entries if e["occurrences"]]

    new_lemmas = {w["lemma"] for w in new}
    print(f"Episode: {episode}")
    label = "candidate" if not args.dry_run else "new"
    print(f"Selected {len(new)} {label} + {len(active)} in-progress words:")
    for w in selected:
        tag = "NEW " if w["lemma"] in new_lemmas else "     "
        print(
            f"  {tag}{w['lemma']} ({w['romaji']})  x{w['count']} in episode, zipf {w['zipf']:.1f}"
        )

    if args.dry_run:
        print("\n--dry-run: no subtitle written, no DB changes, no gloss call.")
        return 0

    # Stability gate: established nouns whose entire variant history is one
    # lemmatized cluster, canonical within one JMdict reading. Their
    # occurrences resolve mechanically; any guard failure escalates to the
    # model. Backtesting showed false positives came only from words this
    # gate excludes, and recall on stable words beats the model (no lazy nulls).
    STABLE_MIN = 15
    stable = {}  # lemma -> set of variant strings
    claims = {}  # variant(lower) -> lemmas with it in their history
    if jm is not None:
        for w in selected:
            row = db_words.get(w["lemma"])
            if not row:
                continue
            vs = dbm.word_variants(conn, row["id"])
            for v in vs:
                claims.setdefault(v, set()).add(w["lemma"])
            if row["pos"] != "名詞" or sum(vs.values()) < STABLE_MIN:
                continue
            ent = jm.get(w["lemma"])
            if not ent:
                continue
            sub = jmdict.reading_entries(ent, row["reading"])
            if len({jmdict._lemmatize(v) for v in vs}) > 2:
                continue  # wide cluster -> polysemy risk -> oracle forever
            if all(jmdict.gloss_match(f"{w['lemma']}@{row['reading']}", sub, v) for v in vs):
                stable[w["lemma"]] = set(vs)

    def tier_match(lemma, ja_line, en_text):
        """Mechanical match with escalation guards. Returns cased EN word
        or None (None = send to model)."""
        if ja_line.count(lemma) != 1:
            return None  # word not unique in JA cue
        for v in stable[lemma]:
            pat = re.compile(r"(?<![A-Za-z])" + re.escape(v) + r"(?![A-Za-z])", re.IGNORECASE)
            hits = pat.findall(en_text)
            if len(hits) != 1:
                if hits:
                    return None  # variant not unique in EN cue
                continue
            rivals = claims.get(v, set()) - {lemma}
            if any(r in ja_line for r in rivals):
                return None  # contested variant
            return hits[0]
        return None

    # Gloss + align.
    cached = {}  # oid -> en_word|None, served from the align cache
    tiered = {}  # oid -> en_word, resolved mechanically
    if args.gloss_json:
        with open(args.gloss_json) as f:
            aligned = glossm.parse_result(json.load(f))
    else:
        # Content repeats lines (openings/endings, previews, catchphrases),
        # so serve previously judged (ja_line, en_text, lemma) triples from
        # the cache.
        lean = []
        for e in entries:
            left = []
            for occ in e["occurrences"]:
                row = dbm.cache_get(conn, occ["ja"], occ["en"], e["lemma"])
                if row is not None:
                    cached[occ["id"]] = row["en_word"]
                    continue
                if e["lemma"] in stable:
                    m = tier_match(e["lemma"], occ["ja"], occ["en"])
                    if m is not None:
                        tiered[occ["id"]] = m
                        continue
                left.append(occ)
            if left:
                lean.append({**e, "occurrences": left})
        if cached:
            print(f"Cache: {len(cached)} occurrences served from align_cache")
        if tiered:
            print(
                f"Tier: {len(tiered)} occurrences resolved mechanically "
                f"({len(stable)} stable words)"
            )
        aligned = {}
        if lean:
            n_occ = sum(len(e["occurrences"]) for e in lean)
            # Backend gate: never spend paid tokens silently. 'claude' shells
            # out to the Claude CLI and bills the user, so it is an explicit,
            # warned opt-in; 'ollama' is the local no-cost default. If the local
            # path is the choice but unreachable, stop with guidance rather than
            # silently falling back to a paid backend.
            if args.backend == "claude":
                print(
                    "WARNING: --backend claude uses the Claude CLI and "
                    "spends your Claude tokens to align this episode. Use "
                    "--backend ollama for the local, no-cost path.",
                    file=sys.stderr,
                )
            else:
                from . import bootstrap

                if not bootstrap.reachable(args.ollama_url):
                    print(
                        f"error: no reachable ollama at {args.ollama_url}; "
                        "cannot align this episode.\n"
                        "  - start it:  ollama serve   "
                        "(install: https://ollama.com), then `bll bootstrap`\n"
                        "  - or opt in to the paid path:  --backend claude  "
                        "(spends your Claude tokens)",
                        file=sys.stderr,
                    )
                    return 1
            print(f"\nAligning {n_occ} occurrences via {args.backend}...")
            aligned = glossm.gloss_and_align(
                lean,
                model=args.model,
                backend=args.backend,
                ollama_url=args.ollama_url,
                think=args.think,
            )
        # Merge cached judgments (already post-veto when stored) and
        # mechanically tiered matches (canonical by construction).
        for oid, enw in list(cached.items()) + list(tiered.items()):
            lem = occurrences[oid][0]
            info = aligned.setdefault(lem, {"gloss": "", "reading": None, "matches": {}})
            info["matches"].setdefault(oid, enw)

    # Dictionary conjunction: a match fires only if the aligner and JMdict
    # agree it's a canonical translation pair. Kills contextual equivalences
    # (a kanji mapped to a proper noun, or to a non-canonical equivalent)
    # regardless of how confident the model was.
    vetoed = {}
    if jm is not None:
        for lemma, info in aligned.items():
            ent = jm.get(lemma)
            if not ent:
                continue
            for oid, m in list(info["matches"].items()):
                if not m:
                    continue
                # Veto if no canonical token matches; otherwise TRIM the span to
                # the matching core so surrounding meaning is never replaced
                # (a negation/article/clause around the word stays in English).
                # "not bad" -> inject 悪い over "bad"; "in front of" -> "front".
                span = jmdict.gloss_span(lemma, ent, m)
                if span is None:
                    info["matches"][oid] = None
                    vetoed.setdefault(lemma, set()).add(m)
                else:
                    info["matches"][oid] = m[span[0] : span[1]]
            # Prefer the curated gloss over the model's for storage/report,
            # from the entry matching this word's reading, rotated so the
            # sense actually matched in this episode comes first.
            w = sel_by_lemma.get(lemma)
            survivors = {m for m in info["matches"].values() if m}
            cg = jmdict.canonical_gloss(ent, w["reading"] if w else None, prefer=survivors)
            if cg:
                info["gloss"] = cg
    if vetoed:
        for lemma, ms in vetoed.items():
            print(f"Dictionary veto: {lemma} -/-> {', '.join(sorted(ms))}")

    # Persist post-veto judgments so repeated lines (songs, previews) never
    # hit the model again. Nulls are cached too - "confirmed no-match" is
    # exactly the judgment lazy models keep re-flubbing.
    if not args.gloss_json:
        for lemma, info in aligned.items():
            for oid, m in info["matches"].items():
                if oid in cached or oid not in occ_text:
                    continue
                dbm.cache_put(conn, *occ_text[oid], lemma, m)

    # Apply reading corrections from the alignment step (e.g. 明日: あす -> あした),
    # but only accept readings JMdict knows for that surface form.
    for lemma, info in aligned.items():
        w = sel_by_lemma.get(lemma)
        r = info.get("reading")
        if not (w and r and r != w["reading"]):
            continue
        ent = jm.get(lemma) if jm else None
        if ent:
            valid = jmdict.readings(ent)
            if valid and r not in valid:
                continue  # model hallucinated a reading; keep tokenizer's
        w["reading"] = r
        w["romaji"] = jp.to_romaji(r)

    # Keep the best n new words that actually aligned; discard artifacts.
    kept, unaligned = [], []
    for w in new:
        info = aligned.get(w["lemma"])
        if info and any(info["matches"].values()):
            if len(kept) < new_budget:  # introduction cap
                kept.append(w)
        else:
            unaligned.append(w["lemma"])
    if unaligned:
        print(f"Discarded (no alignment): {', '.join(unaligned)}")
    new = kept
    new_lemmas = {w["lemma"] for w in new}
    selected = active + new
    sel_by_lemma = {w["lemma"]: w for w in selected}

    # Build the injection plan. en_subs serves as a probe copy: trial
    # replacements consume matched spans so repeated words in a cue resolve
    # to successive positions; the probe itself is never saved.
    per_cue = {}  # en_idx -> count
    replaced = {}  # lemma -> count
    plan = []  # {cue, en_word, lemma, reading, exposure_before, recency_gap}
    exposure_ctr = {
        w["lemma"]: (db_words[w["lemma"]]["exposures"] if w["lemma"] in db_words else 0)
        for w in selected
    }
    # Per-word staleness at the start of this file = tokens of content
    # since the word was last injected. New words have gap 0.
    recency_gap = {
        w["lemma"]: (
            clock_before - db_words[w["lemma"]]["last_seen_pos"] if w["lemma"] in db_words else 0
        )
        for w in selected
    }
    # 0 / negative / None means unlimited swaps per cue.
    cue_cap = args.max_per_cue if args.max_per_cue and args.max_per_cue > 0 else float("inf")
    for oid, (lemma, ens) in sorted(occurrences.items()):
        if lemma not in sel_by_lemma:  # oversampled candidate not kept
            continue
        info = aligned.get(lemma)
        if not info:
            continue
        word = sel_by_lemma[lemma]
        # Candidate English words: the per-occurrence match first, then other
        # confirmed matches for this word, then the gloss. Fallbacks only fire
        # if the word literally appears in the line (boundary-guarded), which
        # recovers occurrences the model skipped or nulled lazily.
        cands = []
        primary = info["matches"].get(oid)
        if primary:
            cands.append(primary)
        # Variant/gloss fallback only for nouns: a literal boundary-guarded hit
        # on a concrete noun is safe, but function words (どう/ない...) map to
        # different English words per context and must not be propagated.
        if word["pos"] == "名詞":
            for v in info["matches"].values():
                if v and v not in cands:
                    cands.append(v)
            g = info.get("gloss")
            if g and g not in cands:
                cands.append(g)
        done = False
        for c in cands:
            for ei in ens:  # try each overlapping EN cue
                if per_cue.get(ei, 0) >= cue_cap:
                    continue
                if replace_in_event(en_subs[ei], c, lemma):  # probe (primary)
                    exp0 = exposure_ctr[lemma]
                    plan.append(
                        {
                            "cue": ei,
                            "en_word": c,
                            "lemma": lemma,
                            "reading": word["reading"],
                            "exposure_before": exp0,
                            "recency_gap": recency_gap[lemma],
                        }
                    )
                    exposure_ctr[lemma] += 1
                    per_cue[ei] = per_cue.get(ei, 0) + 1
                    replaced[lemma] = replaced.get(lemma, 0) + 1
                    # Swap identical in-line repeats in the same cue too, up to
                    # the per-cue cap. These are display only: same exposure, so
                    # they do not bump the learning counter (exposure_ctr/
                    # replaced) and share exp0's fade stage.
                    while per_cue.get(ei, 0) < cue_cap and replace_in_event(en_subs[ei], c, lemma):
                        plan.append(
                            {
                                "cue": ei,
                                "en_word": c,
                                "lemma": lemma,
                                "reading": word["reading"],
                                "exposure_before": exp0,
                                "recency_gap": recency_gap[lemma],
                                "repeat": True,
                            }
                        )
                        per_cue[ei] = per_cue.get(ei, 0) + 1
                    done = True
                    break
            if done:
                break

    out = args.output or re.sub(r"(\.[^.]+)$", r".bll\1", args.en_sub)
    # Auto-notes: every injected word gets a "romaji = gloss" hint mechanically,
    # overridable per-word (words.note) or via --notes. Baked into the plan so
    # `bll render` reproduces them with no DB.
    notes = resolve_notes(
        conn, {inj["lemma"] for inj in plan}, load_notes(args.notes), use_romaji=args.romaji
    )
    # Per-injection heteronym reading/note, anchored on the English each
    # occurrence was aligned to (角 -> つの over "horn", かど over "corner").
    # No-op for every non-heteronym injection.
    apply_sense_first(plan, jm, conn, load_notes(args.notes), use_romaji=args.romaji)
    paths = render_plan(
        args.en_sub,
        plan,
        out,
        args.kana_threshold,
        args.decay_tokens,
        romaji=args.romaji,
        notes=notes,
        note_threshold=args.note_threshold,
    )
    plan_path = os.path.splitext(out)[0] + ".plan.json"
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "episode": episode,
                "en_file": args.en_sub,
                "kana_threshold": args.kana_threshold,
                "decay_tokens": args.decay_tokens,
                "romaji": args.romaji,
                "notes": notes,
                "injections": plan,
            },
            f,
            ensure_ascii=False,
            indent=1,
        )

    # DB bookkeeping.
    # New words with zero successful injections are usually tokenization
    # artifacts (split compounds, name fragments) or untranslatable in
    # context -- don't pollute the DB with them.
    dropped = [w["lemma"] for w in new if not replaced.get(w["lemma"])]
    persisted = [w for w in selected if w["lemma"] not in dropped]

    total_repl = sum(replaced.values())
    # Show/episode metadata: explicit flags win, else auto-detect from the path.
    auto_show, auto_ep = parse_show_episode(args.ja_sub)
    show = getattr(args, "show", None) or auto_show
    episode_no = getattr(args, "episode", None) or auto_ep
    ep_id = dbm.add_episode(
        conn,
        episode,
        len(persisted) - len(active),
        total_repl,
        tokens=ep_tokens,
        show=show,
        episode_no=episode_no,
    )
    clock_after = clock_before + ep_tokens  # position after this file
    for w in persisted:
        lemma = w["lemma"]
        info = aligned.get(lemma, {})
        wid = dbm.upsert_word(
            conn, lemma, w["reading"], w["romaji"], info.get("gloss"), w["pos"], episode
        )
        dbm.record_sighting(conn, wid, ep_id, w["count"], replaced.get(lemma, 0))
        for inj in plan:  # matched-variant history
            if inj["lemma"] == lemma:
                dbm.record_variant(conn, wid, inj["en_word"].lower())
        if replaced.get(lemma, 0):  # reset staleness clock for injected
            dbm.touch_last_seen(conn, wid, clock_after)
        # lifecycle: stamp the episode where this word consolidates ("learned")
        dbm.stamp_learned(conn, wid, episode, args.learning_threshold)
    conn.commit()

    print(f"\nWrote {paths['adaptive']} (+.plain/.kana layers, plan sidecar)")
    print(f"  {total_repl} replacements in {len(per_cue)} cues")
    if dropped:
        print(f"Dropped (no alignment found): {', '.join(dropped)}")
    print("\nWord report:")
    for w in persisted:
        lemma = w["lemma"]
        g = aligned.get(lemma, {}).get("gloss") or db_words.get(lemma, {})
        if not isinstance(g, str):
            g = db_words[lemma]["gloss"] if lemma in db_words else ""
        row = conn.execute("SELECT exposures FROM words WHERE lemma=?", (lemma,)).fetchone()
        exp = row["exposures"] if row else 0
        # Vocabulary is permanent - no nudges toward `bll known`.
        print(f'  {lemma} ({w["romaji"]}) "{g}": {replaced.get(lemma, 0)} injected, {exp} lifetime')
    return 0


# ---------------------------------------------------------------- render


def cmd_render(args: argparse.Namespace) -> int:
    with open(args.plan, encoding="utf-8") as f:
        data = json.load(f)
    en_sub = args.en_sub or data["en_file"]
    threshold = (
        args.kana_threshold if args.kana_threshold is not None else data.get("kana_threshold", 15)
    )
    decay = args.decay_tokens if args.decay_tokens is not None else data.get("decay_tokens")
    romaji = args.romaji or data.get("romaji", False)
    # Notes precedence:
    #   --rebake-db : re-derive from the live DB (fixes plans baked before
    #                 auto-notes existed, or whose readings/glosses have since
    #                 improved); honours per-word operator overrides.
    #   --notes     : an explicit override file.
    #   else        : the notes baked into the plan at process time.
    if args.rebake_db:
        lemmas = {inj["lemma"] for inj in data["injections"]}
        conn = dbm.connect(args.rebake_db)
        notes = resolve_notes(conn, lemmas, load_notes(args.notes), use_romaji=romaji)
        # Heteronym-correct per-injection reading + note.
        n_het = apply_sense_first(
            data["injections"],
            jmdict.load_merged(),
            conn,
            load_notes(args.notes),
            use_romaji=romaji,
        )
        if n_het:
            print(f"Sense-first reading/note fixed on {n_het} heteronym injection(s)")
        if args.update_plan:
            data["notes"] = notes
            with open(args.plan, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            print(f"Re-baked {len(notes)} notes into {args.plan}")
    else:
        notes = load_notes(args.notes) or data.get("notes")
    paths = render_plan(
        en_sub,
        data["injections"],
        args.output,
        threshold,
        decay,
        romaji=romaji,
        notes=notes,
        note_threshold=args.note_threshold,
    )
    print(f"Rendered {len(data['injections'])} injections from {args.plan}:")
    for layer, path in paths.items():
        print(f"  {layer:<8} {path}")
    return 0


# ---------------------------------------------------------------- words/stats


def cmd_words(args: argparse.Namespace) -> int:
    conn = dbm.connect(args.db)
    q = "SELECT * FROM words"
    params: tuple[Any, ...] = ()
    if args.status != "all":
        q += " WHERE status=?"
        params = (args.status,)
    q += " ORDER BY exposures DESC, added_at"
    rows = list(conn.execute(q, params))
    if not rows:
        print("No words yet.")
        return 0
    print(f"{'word':<10} {'reading':<12} {'romaji':<14} {'status':<9} {'exp':>4}  gloss")
    for r in rows:
        print(
            f"{r['lemma']:<10} {r['reading'] or '':<12} {r['romaji'] or '':<14} "
            f"{r['status']:<9} {r['exposures']:>4}  {r['gloss'] or ''}"
        )
    return 0


def cmd_mark(args: argparse.Namespace, status: str) -> int:
    conn = dbm.connect(args.db)
    for lemma in args.lemmas:
        if dbm.set_status(conn, lemma, status):
            print(f"{lemma} -> {status}")
        else:
            print(f"{lemma}: not in database", file=sys.stderr)
    conn.commit()
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    """Show / set / clear the translator-note override for a word.
    bll note 計画                      # show current (override or auto-default)
    bll note 計画 "計画 = plan"        # set an override
    bll note 計画 --clear              # revert to the auto "romaji = gloss" """
    conn = dbm.connect(args.db)
    row = conn.execute(
        "SELECT reading, romaji, gloss, note FROM words WHERE lemma=?", (args.lemma,)
    ).fetchone()
    if not row:
        print(f"{args.lemma}: not in database", file=sys.stderr)
        return 1
    auto = default_note(
        args.lemma, row["reading"], row["romaji"], row["gloss"], use_romaji=not args.kana
    )
    if args.clear:
        dbm.set_note(conn, args.lemma, None)
        conn.commit()
        print(f"{args.lemma}: override cleared -> auto: {auto!r}")
    elif args.text is not None:
        dbm.set_note(conn, args.lemma, args.text)
        conn.commit()
        print(f"{args.lemma}: note set -> {args.text!r}")
    else:
        cur = row["note"] or auto
        src = "override" if row["note"] else "auto"
        print(f"{args.lemma}: {cur!r}  ({src}; auto-default: {auto!r})")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Launch the operator-console web UI (exposes the whole workflow)."""
    if not args.no_bootstrap:
        try:
            from . import bootstrap

            bootstrap.ensure_ready(args.ollama_url, wait=args.wait)
        except RuntimeError as e:
            print(
                f"warning: aligner not ready ({e})\n"
                "  the UI will still start; run Bootstrap from it or set up "
                "ollama, then process.",
                file=sys.stderr,
            )
    try:
        import uvicorn
    except ImportError:
        print("the web UI needs the 'web' extra: pip install 'bll[web]'", file=sys.stderr)
        return 1
    if args.db:
        os.environ["BLL_DB"] = args.db
    print(f"bll operator console -> http://{args.host}:{args.port}")
    uvicorn.run("bll.web:app", host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Make the aligner backend ready: find ollama, pull the base model if
    needed, create the tuned profile if needed. Safe to re-run."""
    from . import bootstrap

    try:
        url = bootstrap.ensure_ready(args.ollama_url, wait=args.wait)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Ready. Aligner profile '{bootstrap.PROFILE}' available at {url}.")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = dbm.connect(args.db)
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM words GROUP BY status"))
    eps = list(conn.execute("SELECT * FROM episodes ORDER BY id DESC LIMIT 10"))
    total_repl = conn.execute("SELECT COALESCE(SUM(replacements),0) FROM episodes").fetchone()[0]
    print(
        f"Words:    {counts.get('learning', 0)} learning, "
        f"{counts.get('known', 0)} known, {counts.get('ignored', 0)} ignored"
    )
    print(
        f"Episodes: {len(list(conn.execute('SELECT id FROM episodes')))} processed, "
        f"{total_repl} total injections"
    )
    if eps:
        print("Recent episodes:")
        for e in eps:
            print(
                f"  {e['processed_at']}  {e['name']}  "
                f"(+{e['new_words']} words, {e['replacements']} injections)"
            )
    return 0


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="bll",
        description="Inject high-value Japanese words into English subtitles.",
    )
    p.add_argument("--db", default=None, help=f"database path (default {dbm.DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("process", help="process an episode's subtitle pair")
    pp.add_argument("ja_sub", help="Japanese (L2) subtitle file")
    pp.add_argument("en_sub", help="English (L1) subtitle file")
    pp.add_argument("-o", "--output", help="output path (default: <en_sub>.bll.<ext>)")
    pp.add_argument(
        "-n",
        "--new-words",
        type=int,
        default=None,
        help="per-episode cap on new words. Default = --max-active "
        "(the load gate is the sole limiter, which lets "
        "value-density scheduling refill freed slots). Set to "
        "1 for a gentler one-new-word-per-episode ramp.",
    )
    pp.add_argument(
        "--max-active",
        type=int,
        default=2,
        help="pace gate and primary limiter: max unconsolidated "
        "words on screen at once. Default 2; 3-4 introduces "
        "words faster.",
    )
    pp.add_argument(
        "--learning-threshold",
        type=int,
        default=10,
        help="exposures at which a word stops counting as "
        "unconsolidated 'load' for the pace gate (default 10)",
    )
    pp.add_argument(
        "--min-count",
        type=int,
        default=2,
        help="min occurrences in episode for new words (default 2)",
    )
    pp.add_argument(
        "--min-zipf", type=float, default=3.0, help="min general-frequency zipf score (default 3.0)"
    )
    pp.add_argument(
        "--max-per-cue",
        type=int,
        default=0,
        help="max replacements per subtitle cue; 0 = unlimited "
        "(incl. identical in-line repeats). Default 0",
    )
    pp.add_argument(
        "--season-dir",
        default=None,
        help="dir of sibling JA subs (*.ja.*); rest-of-season counts break selection ties",
    )
    pp.add_argument(
        "--pick",
        action="store_true",
        help="interactively choose new words from the candidate list before alignment",
    )
    pp.add_argument(
        "--kana-threshold",
        type=int,
        default=15,
        help="adaptive layer drops kana after this many lifetime exposures (default 15)",
    )
    pp.add_argument(
        "--decay-tokens",
        type=int,
        default=6000,
        help="forgetting curve: a faded word's kana returns "
        "if not seen in this many JA tokens of content "
        "(default 6000 ~= 2 episodes; handles content switches)",
    )
    pp.add_argument(
        "--show", default=None, help="show/series name for this episode (default: auto from path)"
    )
    pp.add_argument(
        "--episode", default=None, help="episode number (default: auto-parsed from filename)"
    )
    pp.add_argument(
        "--romaji",
        action="store_true",
        help="annotate with romaji instead of kana (for learners who don't read kana yet)",
    )
    pp.add_argument(
        "--notes",
        default=None,
        help="JSON file of lemma->explanation; adds a top-of-screen "
        "translator note on a noted word's first appearances",
    )
    pp.add_argument(
        "--note-threshold",
        type=int,
        default=5,
        help="show a word's translator note while its lifetime "
        "exposures are below this (same counter as the kana "
        "fade, earlier cutoff; default 5). .answers shows all.",
    )
    pp.add_argument(
        "--include-pos",
        default="noun,adj",
        help="POS classes for new words: noun,adj,verb,adv "
        "(default noun,adj - JA verbs sit awkwardly in EN frames)",
    )
    pp.add_argument(
        "--no-dict", action="store_true", help="disable the JMdict canonical-pair filter"
    )
    pp.add_argument(
        "--dict-json",
        default=None,
        help="load the JMdict lookup from this JSON file instead of "
        "downloading (offline/deterministic testing; must match "
        "load_merged()'s surface -> [{g: glosses, r: readings}] shape). "
        "Ignored if --no-dict is set.",
    )
    pp.add_argument(
        "--backend",
        choices=["claude", "ollama"],
        default="ollama",
        help="alignment backend (default ollama: local, no cost). "
        "'claude' shells out to the Claude CLI and spends "
        "Claude tokens.",
    )
    pp.add_argument(
        "--model", default=None, help="model name (claude model id, or ollama tag like gemma3:12b)"
    )
    pp.add_argument("--ollama-url", default="http://localhost:11434", help="ollama server URL")
    pp.add_argument(
        "--think",
        action="store_true",
        help="enable model thinking (ollama backend; ~15x slower "
        "for no quality gain in testing - off by default)",
    )
    pp.add_argument(
        "--gloss-json", default=None, help="use this JSON file instead of calling the aligner"
    )
    pp.add_argument("--dry-run", action="store_true", help="only show selected words")
    pp.set_defaults(func=cmd_process)

    pr = sub.add_parser(
        "render", help="re-render subtitle layers from a plan sidecar (no model, no DB)"
    )
    pr.add_argument("plan", help="path to a .plan.json sidecar")
    pr.add_argument("en_sub", nargs="?", help="original EN subtitle (default: en_file from plan)")
    pr.add_argument(
        "-o", "--output", required=True, help="adaptive-layer output path (.plain/.kana derived)"
    )
    pr.add_argument(
        "--kana-threshold", type=int, default=None, help="override the plan's kana threshold"
    )
    pr.add_argument(
        "--decay-tokens",
        type=int,
        default=None,
        help="override the plan's forgetting-curve threshold",
    )
    pr.add_argument("--romaji", action="store_true", help="annotate with romaji instead of kana")
    pr.add_argument(
        "--notes", default=None, help="JSON file of lemma->explanation for translator-note overlays"
    )
    pr.add_argument(
        "--note-threshold",
        type=int,
        default=5,
        help="show a note while lifetime exposures are below this (default 5)",
    )
    pr.add_argument(
        "--rebake-db",
        default=None,
        help="re-derive notes from this campaign DB (auto "
        "'kanji (reading) = gloss' + operator overrides) "
        "instead of using the notes baked into the plan. Fixes "
        "plans rendered before auto-notes existed.",
    )
    pr.add_argument(
        "--update-plan",
        action="store_true",
        help="with --rebake-db, write the re-baked notes back into "
        "the plan sidecar so it stays current.",
    )
    pr.set_defaults(func=cmd_render)

    pw = sub.add_parser("words", help="list the word database")
    pw.add_argument("--status", choices=["all", "learning", "known", "ignored"], default="all")
    pw.set_defaults(func=cmd_words)

    for name, status in (("known", "known"), ("ignore", "ignored"), ("learning", "learning")):
        pm = sub.add_parser(name, help=f"mark words as {status}")
        pm.add_argument("lemmas", nargs="+")
        pm.set_defaults(func=lambda a, s=status: cmd_mark(a, s))

    pn = sub.add_parser(
        "note",
        help="show/set/clear a word's translator note (auto-generated 'romaji = gloss' by default)",
    )
    pn.add_argument("lemma")
    pn.add_argument(
        "text", nargs="?", default=None, help="override text; omit to show the current note"
    )
    pn.add_argument(
        "--clear", action="store_true", help="remove the override, revert to the auto default"
    )
    pn.add_argument(
        "--kana", action="store_true", help="preview the auto default in kana instead of romaji"
    )
    pn.set_defaults(func=cmd_note)

    psv = sub.add_parser("serve", help="launch the operator-console web UI")
    psv.add_argument(
        "--host", default="127.0.0.1", help="bind address (0.0.0.0 to expose on the network)"
    )
    psv.add_argument("--port", type=int, default=8000)
    psv.add_argument("--ollama-url", default=None, help="ollama URL override")
    psv.add_argument(
        "--wait",
        type=int,
        default=0,
        help="seconds to wait for ollama on startup (container start)",
    )
    psv.add_argument(
        "--no-bootstrap", action="store_true", help="skip the model provisioning check on startup"
    )
    psv.set_defaults(func=cmd_serve)

    pb = sub.add_parser(
        "bootstrap", help="provision the aligner model (find ollama, pull base, create profile)"
    )
    pb.add_argument(
        "--ollama-url", default=None, help="ollama URL (default: probe localhost / host / sibling)"
    )
    pb.add_argument(
        "--wait",
        type=int,
        default=0,
        help="seconds to wait for ollama to come up (container start)",
    )
    pb.set_defaults(func=cmd_bootstrap)

    ps = sub.add_parser("stats", help="overview of progress")
    ps.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
