"""Japanese tokenization, lemmatization and candidate-word extraction."""

import re

import fugashi
import pykakasi
from wordfreq import zipf_frequency

_tagger = None
_kks = None


def get_tagger():
    global _tagger
    if _tagger is None:
        _tagger = fugashi.Tagger()
    return _tagger


def to_romaji(hira: str) -> str:
    global _kks
    if _kks is None:
        _kks = pykakasi.kakasi()
    return "".join(item["hepburn"] for item in _kks.convert(hira))


def kata_to_hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


# Content-word POS (UniDic pos1). Pronouns (代名詞), particles (助詞),
# determiners (連体詞), interjections (感動詞), affixes (接頭辞/接尾辞) are
# separate pos1 values in UniDic and never get this far.
CONTENT_POS = {"名詞", "動詞", "形容詞", "副詞"}
# pos2 exclusions:
#   固有名詞/数詞/助数詞 - proper nouns, numerals, counters
#   非自立可能 - "can be non-independent": UniDic's lexicographer-curated tag
#     for verbs/adjectives that serve as grammatical auxiliaries (する, いる,
#     なる, 見る, 行く, 来る, しまう, ない, いい...). This is the light-verb/
#     copula/motion-verb class, detected mechanically instead of by blacklist.
#     Concrete verbs (食べる, 戦う, 降る) are 動詞/一般 and pass.
EXCLUDE_POS2 = {"固有名詞", "数詞", "助数詞", "非自立可能"}

# Temporary compensation, not architecture: the residue of the
# compositionality criterion that no available mechanical signal detects yet -
# abstract relational nouns (thing/way/sake/reason-words) and frame-heavy
# adverbs.
# Checked against BOTH the surface lemma and UniDic's normalized lemma
# (f.lemma), so spelling variants collapse (ヤツ/やつ -> 奴, こと -> 事).
# Auto-detecting this class is an open problem; keep this list small and
# justified per-entry by the criterion, not by annoyance.
STOPWORDS = {
    # abstract relational nouns (normalized kanji + common kana spellings)
    "事",
    "こと",
    "物",
    "もの",
    "奴",
    "所",
    "ところ",
    "訳",
    "わけ",
    "筈",
    "はず",
    "為",
    "ため",
    "様",
    "よう",
    "方",
    "ほう",
    "内",
    "うち",
    "時",
    "とき",
    "気",
    "感じ",
    "まま",
    "くらい",
    "ぐらい",
    # frame-heavy demonstrative/interrogative adverbs (pos1=副詞, so they
    # pass CONTENT_POS but their meaning is purely deictic)
    "どう",
    "そう",
    "こう",
    "ああ",
}

_ascii_re = re.compile(r"^[\x00-\x7F　、。！？…・「」『』（）]+$")

POS_MAP = {"noun": "名詞", "adj": "形容詞", "verb": "動詞", "adv": "副詞"}


def clean_sdh(line):
    """Strip SDH artifacts from a JA subtitle line: speaker labels in
    parens, inline furigana, sound descriptions in parens, song-lyric
    markers, and invisible bidi/zero-width marks."""
    line = re.sub("[​-‏⁠﻿]", "", line)
    line = re.sub(r"（[^）]*）|\([^)]*\)", " ", line)
    line = re.sub(r"♪[^♪]*♪", " ", line)
    line = re.sub(r"[♪♬～〜]+", " ", line)
    return re.sub(r"\s+", " ", line).strip()


def count_tokens(lines):
    """Total JA morpheme tokens across cleaned subtitle lines - the unit of
    the content/forgetting clock. Vocabulary-independent: counts all
    language that flowed past, not just injected words."""
    tagger = get_tagger()
    return sum(len(list(tagger(line))) for line in lines if line)


def analyze(lines, jm=None, keep=None):
    """Tokenize JA subtitle lines.

    lines: list of plaintext strings (one per cue, cue index = position).
    jm: optional JMdict lookup dict; enables compound merging.
    keep: set of lemmas exempt from the compound-fragment filter - pass the
        already-learned words so they inject everywhere they legitimately
        appear. The filter exists to avoid selecting junk fragments as new
        words; for a learned word max recall is preferred and the JMdict veto
        is the real backstop. Fixes elided-particle adjacency (試合テレビで =
        試合を テレビで - "match" dropped because テレビ sat next to it).
    """
    keep = keep or set()
    tagger = get_tagger()
    words = {}
    for idx, line in enumerate(lines):
        toks = list(tagger(line))
        i = -1
        while i + 1 < len(toks):
            i += 1
            tok = toks[i]
            f = tok.feature
            # Compound merging - greedy longest adjacent-noun surface that is
            # a real JMdict word (生徒会, 生徒会長). The compound is a
            # vocabulary item in its own right; its components are consumed and
            # never counted (no accidental component picks). Non-JMdict
            # adjacencies (proper-noun compounds) fall through to the
            # compound-internal exclusion below, as before.
            if jm is not None and f.pos1 == "名詞" and f.pos2 not in ("固有名詞", "数詞", "助数詞"):
                comp_len = 0
                for L in (3, 2):
                    if i + L > len(toks):
                        continue
                    rest = toks[i + 1 : i + L]
                    if not all(
                        t.feature.pos1 in ("名詞", "接尾辞")
                        and t.feature.pos2 not in ("固有名詞", "数詞")
                        for t in rest
                    ):
                        continue
                    surface = "".join(t.surface for t in toks[i : i + L])
                    if surface in jm and surface not in STOPWORDS:
                        comp_len = L
                        break
                if comp_len:
                    reading = kata_to_hira(
                        "".join(
                            t.feature.lForm or t.feature.pron or "" for t in toks[i : i + comp_len]
                        )
                    )
                    w = words.get(surface)
                    if w is None:
                        words[surface] = w = {
                            "lemma": surface,
                            "reading": reading,
                            "romaji": to_romaji(reading) if reading else "",
                            "pos": "名詞",
                            "count": 0,
                            "cues": set(),
                            # presence in JMdict-common waives the zipf
                            # floor (wordfreq rarely covers compounds)
                            "zipf": max(zipf_frequency(surface, "ja"), 3.0),
                            "compound": True,
                        }
                    w["count"] += 1
                    w["cues"].add(idx)
                    i += comp_len - 1  # consume components
                    continue
            if f.pos1 not in CONTENT_POS:
                continue
            # Compound-internal occurrence (生徒|会): MeCab tokens are
            # contiguous, so a noun glued to another noun (or a prefix before
            # it) is a fragment of a larger word at this occurrence, excluded
            # so the model never sees fragment contexts.
            #   A trailing suffix (接尾辞) is not a fragment boundary -
            #   honorific/plural/productive suffixes (戦車さん, 戦車たち, 攻撃力)
            #   leave the head noun's meaning and its English word intact, so
            #   dropping them silently lost real injections (a head noun plus
            #   honorific-plural never became its English word). The JMdict
            #   veto still blocks any non-canonical match, and real
            #   suffix-compounds (生徒会長) are caught by the merge pass above.
            #   So only the second element of a suffix-compound is dropped, via
            #   the `prev` check.
            if f.pos1 == "名詞" and (f.orthBase or tok.surface) not in keep:
                prev = toks[i - 1].feature.pos1 if i > 0 else None
                nxt = toks[i + 1].feature.pos1 if i + 1 < len(toks) else None
                if prev in ("名詞", "接頭辞") or nxt == "名詞":
                    continue
            if f.pos2 in EXCLUDE_POS2:
                continue
            lemma = f.orthBase or tok.surface
            if not lemma or _ascii_re.match(lemma):
                continue
            norm = f.lemma or lemma  # UniDic-normalized (ヤツ -> 奴)
            if lemma in STOPWORDS or norm in STOPWORDS:
                continue
            # single-kana lemmas are noise
            if len(lemma) == 1 and ("ぁ" <= lemma <= "ん" or "ァ" <= lemma <= "ヶ"):
                continue
            w = words.get(lemma)
            if w is None:
                reading = kata_to_hira(f.lForm or f.pronBase or "")
                words[lemma] = w = {
                    "lemma": lemma,
                    "reading": reading,
                    "romaji": to_romaji(reading) if reading else "",
                    "pos": f.pos1,
                    "count": 0,
                    "cues": set(),
                    "zipf": zipf_frequency(lemma, "ja"),
                }
            w["count"] += 1
            w["cues"].add(idx)
    return words


def select_new(
    words,
    exclude,
    n,
    min_count=2,
    min_zipf=3.0,
    allowed_pos=None,
    future=None,
    future_traj=None,
    load_threshold=10,
    consolidation=12,
    durable_zipf=4.5,
    count_field="count",
):
    """Pick the n most useful new words.

    Usefulness = frequent in this episode (seen again immediately)
    x common in the language overall (worth knowing forever).

    allowed_pos: set of UniDic pos1 values for NEW words. Default policy
    (empirically): nouns and adjectives only; JA verbs/adverbs sit awkwardly
    in English frames.

    Runway: when `future` (rest-of-season occurrence counts) is known,
    skip "doomed" words - too few total occurrences to ever consolidate
    (count + future < consolidation) AND not general enough to recur in
    other shows (zipf < durable_zipf). A high-zipf word is kept even with
    little season runway because other shows will reinforce it for free
    (the general-vs-domain split); a domain word with no runway is dropped.
    """

    # Score on the injectable count (occurrences that will actually inject)
    # when available - `count_field` selects "inj" vs raw "count".
    def cnt(w):
        return w.get(count_field, w["count"])

    # flat rest-of-season totals (for the runway/doomed check), derived from
    # the per-episode trajectory when available.
    fut = {w: sum(v) for w, v in future_traj.items()} if future_traj is not None else future or {}
    have_lookahead = future_traj is not None or future is not None

    def time_to_consolidate(w):
        """Episodes (counting this one) until the word's cumulative INJECTABLE
        exposures reach load_threshold and it frees its learning slot. 999 if
        it will never consolidate within the known season. The denominator of
        value-density."""
        cum = cnt(w)
        if cum >= load_threshold:
            return 1
        for k, c in enumerate(future_traj.get(w["lemma"], []), start=2):
            cum += c
            if cum >= load_threshold:
                return k
        return 999  # won't consolidate within the known season

    def doomed(w):
        """No-orphan rule: only introduce a word that will either
        (a) consolidate before the season ends, OR (b) carry over to other
        shows (general enough, zipf >= durable_zipf, so the next show keeps
        reinforcing it). Skip a word that does neither - a domain-specific
        straggler introduced too late would be half-learned then orphaned on
        a show switch. With injection-aware trajectories, "won't consolidate
        in-season" = time_to_consolidate == 999."""
        if not have_lookahead:
            return False  # no lookahead -> can't tell, keep
        if w["zipf"] >= durable_zipf:
            return False  # general -> carries over, never orphaned
        if future_traj is not None:
            return time_to_consolidate(w) >= 999  # domain + won't finish
        return cnt(w) + fut.get(w["lemma"], 0) < consolidation  # flat fallback

    cands = [
        w
        for lemma, w in words.items()
        if lemma not in exclude
        and cnt(w) >= min_count
        and w["zipf"] >= min_zipf
        and (allowed_pos is None or w["pos"] in allowed_pos)
        and not doomed(w)
    ]
    if future_traj is not None:
        # Value-density: the scarce resource is learning-SLOT TIME.
        # Rank by value per slot-episode = (injectable x zipf) /
        # time-to-consolidate. Front-loads fast consolidators, defers slow
        # words to their dense stretch, and ignores phantom-frequency words
        # whose English rarely appears - they have low injectable count.
        cands.sort(key=lambda w: (cnt(w) * w["zipf"]) / time_to_consolidate(w), reverse=True)
    else:
        # No season lookahead: episode frequency primary, flat runway tiebreak.
        cands.sort(
            key=lambda w: (cnt(w) * w["zipf"], cnt(w) + fut.get(w["lemma"], 0)), reverse=True
        )
    return cands[:n]
