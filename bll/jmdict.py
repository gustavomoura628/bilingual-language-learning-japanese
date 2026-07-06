"""JMdict canonical-pair dictionary check.

A swap fires only if the aligner AND a curated bilingual dictionary agree
(conjunction, not alternation). The dictionary is context-free, so it rejects
contextual equivalences the LLM may propose (e.g. a proper noun the model maps
onto a common word) that are not canonical translations.

Uses jmdict-simplified (eng-common subset, ~20K common words) from
https://github.com/scriptin/jmdict-simplified - small enough to parse fast,
and the zipf >= 3 selection floor means real candidates should be in it.
"""

import gzip
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import simplemma

DATA_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "bll"
)
# v2: per-entry structure (list of {g, r} per surface) so canonical_gloss can
# pick the entry matching the word's reading (人=ひと "person", not the じん
# suffix entry's "-ian; -ite").
LOOKUP_PATH = os.path.join(DATA_DIR, "jmdict-lookup-v2.json.gz")
LATEST_URL = "https://github.com/scriptin/jmdict-simplified/releases/latest"
ASSET_URL = (
    "https://github.com/scriptin/jmdict-simplified/releases/download/"
    "{tag}/jmdict-eng-common-{tag}.json.zip"
)

_lookup = None
_gloss_token_cache = {}

# Tokens in glosses that carry no lexical content.
STOP_TOKENS = {
    "to",
    "a",
    "an",
    "the",
    "of",
    "in",
    "on",
    "at",
    "for",
    "with",
    "and",
    "or",
    "be",
    "being",
    "one",
    "ones",
    "oneself",
    "something",
    "someone",
    "somebody",
    "etc",
    "esp",
    "usu",
    "e",
    "g",
    "i",
    "s",
    "t",
}

_word_re = re.compile(r"[a-z]+")


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "bll"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def _latest_tag():
    """Resolve the latest release tag from the /releases/latest redirect
    (avoids api.github.com, which some networks block)."""
    req = urllib.request.Request(LATEST_URL, method="HEAD", headers={"User-Agent": "bll"})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        resp = opener.open(req, timeout=60)
        location = resp.url
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location", "")
    m = re.search(r"/releases/tag/([^/]+)$", location)
    if not m:
        raise RuntimeError(f"could not resolve latest jmdict release: {location!r}")
    return m.group(1)


def build(verbose=True):
    """Download jmdict-simplified eng-common and build a compact lookup."""
    if verbose:
        print("Downloading JMdict (eng-common) from jmdict-simplified...")
    tag = _latest_tag()
    raw = _fetch(ASSET_URL.format(tag=urllib.parse.quote(tag)))
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        data = json.loads(z.read(z.namelist()[0]))

    lookup = {}
    for e in data["words"]:
        kana = [k["text"] for k in e.get("kana", [])]
        kanji = [k["text"] for k in e.get("kanji", [])]
        glosses = [
            g["text"]
            for s in e.get("sense", [])
            for g in s.get("gloss", [])
            if g.get("lang", "eng") == "eng"
        ]
        if not glosses:
            continue
        for surface in kanji + kana:
            lookup.setdefault(surface, []).append({"g": glosses, "r": kana})

    os.makedirs(DATA_DIR, exist_ok=True)
    with gzip.open(LOOKUP_PATH, "wt", encoding="utf-8") as f:
        json.dump(lookup, f, ensure_ascii=False)
    if verbose:
        print(f"JMdict lookup built: {len(lookup)} surfaces -> {LOOKUP_PATH}")
    return lookup


def load(auto_build=True):
    """Returns the surface -> {g: glosses, r: readings} dict (cached)."""
    global _lookup
    if _lookup is not None:
        return _lookup
    if os.path.exists(LOOKUP_PATH):
        with gzip.open(LOOKUP_PATH, "rt", encoding="utf-8") as f:
            _lookup = json.load(f)
        return _lookup
    if not auto_build:
        return None
    _lookup = build()
    return _lookup


COMPOUNDS_PATH = os.path.join(DATA_DIR, "jmdict-compounds.json.gz")
FULL_ASSET_URL = (
    "https://github.com/scriptin/jmdict-simplified/releases/"
    "download/{tag}/jmdict-eng-{tag}.json.zip"
)
_compounds = None
_cjk_re = re.compile(r"^[぀-ヿー一-鿿]{3,8}$")


def _iter_words(text):
    """Stream entries from a jmdict-simplified JSON string without loading
    the whole structure (full dict is ~500MB; this keeps RAM ~1GB)."""
    dec = json.JSONDecoder()
    i = text.index('"words"')
    i = text.index("[", i) + 1
    while True:
        while text[i] in " ,\n\t\r":
            i += 1
        if text[i] == "]":
            return
        obj, i = dec.raw_decode(text, i)
        yield obj


def build_compounds(verbose=True):
    """Compound lookup from the FULL JMdict (eng): kanji surfaces 3-8 chars.
    The eng-common subset misses everyday compounds like 生徒会; this gives
    the merger a real existence check without bloating the main lookup."""
    if verbose:
        print("Downloading full JMdict for the compound lookup (one-time)...")
    tag = _latest_tag()
    raw = _fetch(FULL_ASSET_URL.format(tag=urllib.parse.quote(tag)))
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read(z.namelist()[0]).decode("utf-8")
    del raw
    lookup = {}
    for e in _iter_words(text):
        kanji = [k["text"] for k in e.get("kanji", []) if _cjk_re.match(k["text"])]
        if not kanji:
            continue
        kana = [k["text"] for k in e.get("kana", [])]
        glosses = [
            g["text"]
            for s in e.get("sense", [])[:3]
            for g in s.get("gloss", [])[:3]
            if g.get("lang", "eng") == "eng"
        ]
        if not glosses:
            continue
        for surface in kanji:
            lookup.setdefault(surface, []).append({"g": glosses, "r": kana})
    with gzip.open(COMPOUNDS_PATH, "wt", encoding="utf-8") as f:
        json.dump(lookup, f, ensure_ascii=False)
    if verbose:
        print(f"Compound lookup built: {len(lookup)} surfaces")
    return lookup


def load_compounds(auto_build=True):
    global _compounds
    if _compounds is not None:
        return _compounds
    if os.path.exists(COMPOUNDS_PATH):
        with gzip.open(COMPOUNDS_PATH, "rt", encoding="utf-8") as f:
            _compounds = json.load(f)
        return _compounds
    if not auto_build:
        return None
    _compounds = build_compounds()
    return _compounds


def load_merged(auto_build=True, fixture_path=None):
    """Common lookup overlaid on the compound lookup (common wins).

    fixture_path: if given, load ONLY this JSON file instead of the
    common+compounds network/build path - a committed, hand-authored fixture
    for offline/deterministic testing (e.g.
    tests/fixtures/dict_promise_secret.json). Bypasses load()/load_compounds()
    (and therefore any network access) entirely; the file must already be in
    the shape this function returns: {surface: [{"g": [...], "r": [...]}]}.
    """
    if fixture_path:
        with open(fixture_path, encoding="utf-8") as f:
            return json.load(f)
    common = load(auto_build) or {}
    comp = load_compounds(auto_build)
    if not comp:
        return common
    merged = dict(comp)
    merged.update(common)
    return merged


def entry(lemma):
    lk = load(auto_build=False)
    return lk.get(lemma) if lk else None


def _lemmatize(tok):
    try:
        return simplemma.lemmatize(tok, lang="en")
    except Exception:
        return tok


def readings(ent):
    """All kana readings across a surface's entries."""
    out = []
    for e in ent:
        out.extend(r for r in e["r"] if r not in out)
    return out


# A gloss containing any of these is a DESCRIPTION, not a synonym: a negation
# ('not beautiful' -> the antonym) or a definitional relativiser/placeholder
# ('place WHERE you do SOMETHING'). Its individual tokens must NOT become
# matchable synonyms. Clean glosses (no marker) are trusted, incl. their head.
GLOSS_MARKERS = frozenset(
    (
        "not",
        "no",
        "never",
        "without",
        "cannot",
        "nor",
        "neither",
        "n't",
        "where",
        "who",
        "whom",
        "whose",
        "which",
        "when",
        "why",
        "whoever",
        "wherever",
        "something",
        "someone",
        "somebody",
        "such",
    )
)


def _gloss_tokens(ent_key, ent):
    """Matchable synonym tokens for a word. A gloss with a GLOSS_MARKER (a
    negation or a definitional relativiser) is a DESCRIPTION, not a synonym, so
    it is skipped wholesale - this is what stops 悪い 'not beautiful' from leaking
    'beautiful'/'not' and 前 'place where you do something' from leaking 'place'.
    Clean glosses contribute all their content words (so 'military operation' ->
    operation, 'female high school student' -> high/school/student all match)."""
    toks = _gloss_token_cache.get(ent_key)
    if toks is None:
        toks = set()
        for e in ent:
            for g in e.get("g", []):
                g2 = re.sub(r"\([^)]*\)", " ", g.lower())  # drop "(quality)" etc.
                raw = _word_re.findall(g2)
                if any(t in GLOSS_MARKERS for t in raw):
                    continue  # a definition - trust nothing
                for t in raw:
                    if t in STOP_TOKENS:
                        continue
                    toks.add(t)
                    toks.add(_lemmatize(t))
        _gloss_token_cache[ent_key] = toks
    return toks


_DERIV = ("able", "ably", "ful", "ness", "ment", "ly", "ion", "tion", "sion", "ation")


def _tok_matches(t, toks):
    """Does one English token canonically match a gloss token (incl. bounded
    inflection/derivation/abbreviation bridges)? Stopwords never match - this is
    what excludes 'not'/'a'/'the' from the injected span, so a negation or
    article around the word stays in English instead of being deleted."""
    if t in STOP_TOKENS:
        return False
    lem = _lemmatize(t)
    if t in toks or lem in toks:
        return True
    if t.endswith("ly") and (t[:-2] in toks or t[:-2] + "e" in toks):
        return True  # beautifully -> beautiful
    if len(lem) >= 4 and any(gt.startswith(lem) and gt[len(lem) :] in _DERIV for gt in toks):
        return True  # enjoyed/enjoy -> enjoyable
    if len(t) >= 5 and any(gt.startswith(t) and len(gt) - len(t) >= 4 for gt in toks):
        return True  # recon -> reconnaissance
    if len(lem) >= 5:
        if lem.endswith("d") and lem[:-1] + "se" in toks:
            return True  # defend -> defense
        if lem.endswith("de") and lem[:-2] + "sion" in toks:
            return True  # decide -> decision
    return False


def gloss_span(lemma, ent, en_word):
    """Char span (start, end) of the minimal part of en_word that canonically
    translates lemma - the TIGHT core to inject over, so a negation / article /
    rest-of-clause around it stays in English. None if nothing matches.

    A single-word synonym matches as a token (+ bridges); a multi-word gloss
    matches only as a contiguous phrase. That is what stops 悪い from matching
    'beautiful' (leaked from its 'not beautiful' gloss) and stops the 'not' in
    'not bad' from being swallowed - the lemma injects over just 'bad'."""
    toks = _gloss_tokens(lemma, ent)
    # span the first..last gloss-matching token: drops leading/trailing material
    # (a negation, article, or the rest of a clause) that isn't part of the word.
    hits = [
        (m.start(), m.end())
        for m in _word_re.finditer(en_word.lower())
        if _tok_matches(m.group(), toks)
    ]
    return (hits[0][0], hits[-1][1]) if hits else None


def gloss_match(lemma, ent, en_word):
    """Does en_word canonically translate lemma (some token matches a gloss)?"""
    return gloss_span(lemma, ent, en_word) is not None


def reading_entries(ent, reading):
    """Entries whose kana include `reading` (falls back to all). Used to
    restrict the canonical set to one reading - kills the 実 み/じつ
    cross-reading homograph hazard in tiered routing."""
    sub = [e for e in ent if reading and reading in e["r"]]
    return sub or ent


def _entry_en_score(e, en_word):
    """How well one JMdict entry's glosses match en_word, for sense-first reading
    selection. 3 = a gloss equals en_word exactly; 2 =
    head-gloss token overlap; 1 = later-gloss token overlap; 0 = no match.
    Parentheticals are stripped; tokens are lemmatized so 'horns'->'horn'.
    NOTE: deliberately does NOT use _gloss_tokens (which caches by lemma, unioning
    all entries) - each entry must be scored in isolation."""
    ew = re.sub(r"\s*\([^)]*\)", "", en_word.lower()).strip()
    ew_toks = set()
    for t in _word_re.findall(ew):
        ew_toks.add(t)
        ew_toks.add(_lemmatize(t))
    if not ew_toks:
        return 0
    best = 0
    for i, g in enumerate(e["g"]):
        gn = re.sub(r"\s*\([^)]*\)", "", g.lower()).strip()
        if gn == ew:
            return 3
        gtoks = set()
        for t in _word_re.findall(gn):
            gtoks.add(t)
            gtoks.add(_lemmatize(t))
        if ew_toks & gtoks:
            best = max(best, 2 if i == 0 else 1)
    return best


def entry_for_en(lemma, ent, en_word):
    """The JMdict entry (one reading-group) whose gloss the injected English word
    maps to - the sense this occurrence actually means. Returns the entry dict,
    or None when no entry STRICTLY wins (a tie, or no gloss match at all) so the
    caller keeps its existing reading. This is what lets 角 read つの over 'horn'
    but かど over 'corner': a per-injection disambiguation anchored on the
    English already committed to replacing."""
    if not ent or not en_word:
        return None
    scored = sorted(((_entry_en_score(e, en_word), -i) for i, e in enumerate(ent)), reverse=True)
    if scored[0][0] == 0:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None  # ambiguous tie -> don't override
    return ent[-scored[0][1]]


def canonical_gloss(ent, reading=None, max_glosses=2, prefer=None):
    """Short display gloss (parentheticals stripped). Prefers the entry
    whose readings include `reading` (人+ひと -> "person", not "-ian").

    prefer: confirmed English matches from this episode - the gloss list is
    rotated so the sense actually used comes first (すごい matched "amazing"
    -> store "amazing; great", not first-sense "terrible")."""
    best = ent[0]
    if reading:
        for e in ent:
            if reading in e["r"]:
                best = e
                break
    glosses = list(best["g"])
    if prefer:
        ptoks = set()
        for p in prefer:
            for t in _word_re.findall(p.lower()):
                ptoks.add(t)
                ptoks.add(_lemmatize(t))
        for i, g in enumerate(glosses):
            gtoks = {x for t in _word_re.findall(g.lower()) for x in (t, _lemmatize(t))}
            if gtoks & ptoks:
                glosses = glosses[i:] + glosses[:i]
                break
    out = []
    for g in glosses[:max_glosses]:
        out.append(re.sub(r"\s*\([^)]*\)", "", g).strip())
    return "; ".join(x for x in out if x)
