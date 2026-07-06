# DESIGN — Issue #10: Exercise the dictionary-veto path in the e2e smoke via a committed offline fixture

> Input: `.claude/sdd/_synthesized/DEFINE_ISSUE_10_DICT_VETO_E2E_FIXTURE.md`. Every non-obvious claim
> below (gloss-token matching, the `injectable_counts()`/selection interaction, the fallback-recovery
> mechanic, byte-identical determinism) was **executed against the real `bll/jmdict.py`, `bll/jp.py`,
> `bll/cli.py` in this checkout** via a monkeypatched `jmdict.load_merged` and, for the final pass, the
> actual drafted seam + fixture run through the real `uv run pytest`, `uv run ruff check`,
> `uv run ruff format --check`, and `uv run mypy bll/` — not hand-traced. Exact output is quoted where
> it matters.

## Problem recap

`tests/test_process_e2e_smoke.py` runs with `--no-dict`, so `cmd_process`'s "Dictionary conjunction"
veto block (`bll/cli.py`) never executes in CI — `jmdict.load_merged()`'s real path cold-downloads a
GitHub release asset, which is unacceptable in an offline, deterministic suite. Issue #10 closes this
gap with a committed, hand-authored, license-clean dictionary fixture plus an explicit, discoverable
offline seam (no env-var magic) that lets `load_merged()` consume it instead of downloading.

---

## Seam design

**New CLI flag on the `process` subparser: `--dict-json PATH`.** Mirrors the existing `--gloss-json`
convention exactly (load a JSON file instead of calling the real backend), so it's immediately
recognizable next to `--no-dict` in `--help` output.

**New parameter on `jmdict.load_merged`**, not a new sibling function — the fixture *is* the
already-merged view, authored directly in the shape the function already returns, so extending the
existing contract is more honest than inventing a parallel one:

```python
# bll/jmdict.py
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
```

**Decision: the flag REPLACES `load_merged()`'s network path entirely when set** (does not still allow
compounds to merge on top). Rationale: the fixture is hand-authored *as* the merged view; a partial
override ("fixture for common, still network for compounds" or vice versa) would need a second flag,
a merge-order decision, and would still touch the network — contradicting "zero network." The DEFINE's
own guidance ("no need to gzip or mimic the real on-disk cache format... a hand-authored test fixture")
already assumes one flat file is the merged shape, not two.

**Call site in `cmd_process`** (`bll/cli.py`, replacing the single line that calls `load_merged()`):

```python
# JMdict canonical-pair dictionary (conjunction filter; --no-dict to
# skip). Merged view: common subset + full-dict compounds. --dict-json
# loads a committed offline fixture instead (deterministic testing).
jm = None
if not args.no_dict:
    jm = jmdict.load_merged(fixture_path=args.dict_json)  # auto-downloads unless --dict-json
```

**New argparse block** (`bll/cli.py`, immediately after the existing `--no-dict` argument, ~line 1266):

```python
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
```

(Help text deliberately avoids embedding literal `"g"`/`"r"` double quotes — `ruff format` wants to
flip the enclosing quote style to single quotes to dodge escaping, which is uglier; describing the
shape in prose sidesteps the churn entirely. Verified: `ruff format --check` is clean with this
wording, and flags a reformat if the quoted-JSON version is used instead.)

**Precedence, not validation**: if both `--no-dict` and `--dict-json` are passed, `--no-dict` wins
silently (`args.dict_json` is never read, since the guard is `if not args.no_dict:`). No argparse
`mutually_exclusive_group` or explicit error — this combination is harmless (dict_json is simply
inert) and adding validation for it is unnecessary ceremony for a flag whose own docstring says
"minimal, not a general feature." Not unit-tested either, for the same reason (DEFINE's scope
guardrail: no full veto-logic unit coverage).

**`cmd_render`'s own `jmdict.load_merged()` call (`--rebake-db` path, ~line 996) is untouched** — it
keeps calling with no `fixture_path` (defaults to `None`), 100% unaffected. Out of scope per the
DEFINE/issue, which only discusses `cmd_process`.

**Why no changes are needed anywhere else** (`injectable_counts()`, `jp.analyze()`, the veto block
itself): all three already take `jm` as a plain dict argument — they don't care where it came from.
The seam is entirely: one new parameter on one function, one new flag, one call-site edit. Verified by
running the full pipeline through a monkeypatched `load_merged` before writing any of the above (see
"Fixture design" and "Test design" for the exact verified numbers).

---

## Fixture design

**File: `tests/fixtures/dict_promise_secret.json`** (mirrors the `promise_secret.{ja,en}.srt` naming —
"the dictionary fixture for the same promise_secret pair"). Exact content:

```json
{
  "約束": [
    {"g": ["promise", "vow"], "r": ["やくそく"]}
  ],
  "秘密": [
    {"g": ["secret"], "r": ["ひみつ"]}
  ]
}
```

Hand-authored, license-clean: these are plain, common English words describing the concept (the same
way the existing `promise_secret.{ja,en}.srt` pair is "fully-fabricated... not sourced from any real
show"), not a copy of JMdict's actual entries, phrasing, or multi-sense structure. Reuses the exact two
lemmas the existing `promise_secret` fixture pair already produces (約束/やくそく "promise",
秘密/ひみつ "secret") — no third subtitle pair invented, per the DEFINE's steer.

### Which lemma is vetoed, and why this shape (not a "wrong dictionary")

Both lemmas' fixture entries are **genuinely canonical** (real synonyms for their actual meaning), for
a structural reason discovered by executing `injectable_counts()` against the real code, not by
choice of taste:

`--dict-json` makes `jm` non-`None`, which flips `cmd_process`'s `count_field` from `"count"` to
`"inj"` (`count_field="count" if args.no_dict else "inj"`) — new-word selection is now scored by
`injectable_counts()`, which requires a lemma's own gloss tokens to literally overlap its own EN
cues' text just to clear `min_count=2` and reach `jp.select_new`'s candidate pool at all. A lemma whose
fixture entry is *entirely* non-canonical (no gloss token present anywhere in its EN cues) scores
`inj=0` and never reaches the veto block as a selected word in the first place — that would test the
selection gate, not the conjunction check. Verified directly:

```
=== jp.analyze with jm=FIXTURE (compound-merge check) ===
約束 -> count=4, cues={0, 1, 2, 5}      (identical to the --no-dict path)
秘密 -> count=2, cues={3, 4}            (identical to the --no-dict path)

=== injectable_counts with FIXTURE ===
約束 inj = 4  raw count = 4
秘密 inj = 2  raw count = 2

=== select_new sort order (count_field='inj') ===
select_new order: ['約束', '秘密']       (same order as the --no-dict test's ids 1-4/5-6)
```

(The compound-merge branch in `jp.analyze()` — which activates whenever `jm is not None` — also does
not fire for either lemma: every JA line has a particle or auxiliary immediately after 約束/秘密, never
another noun, so the adjacent-noun-merge attempt always fails its POS gate before it even reaches a
`surface in jm` lookup. Verified above: cues/counts are byte-for-byte identical to the `--no-dict`
run's documented facts.)

So **injectable_counts() being active changes nothing observable** in this fixture — `inj` numerically
equals the old raw `count` for both lemmas, confirming the DEFINE's open question. The veto instead
comes from a **per-occurrence mismatch between the fixture's canonical gloss and the aligner's claimed
match**, which is the actual real-world scenario `gloss_span` exists to catch (per `jmdict.py`'s own
module docstring: "rejects contextual equivalences the LLM may propose... that are not canonical
translations").

**The vetoed occurrence: 約束's first occurrence (id=1, cue 0) claims `en_word="special"` instead of
the correct `"promise"`** in the test's `--gloss-json` payload (a hand-edited one-word change from the
existing `GLOSS_JSON` constant — see Test design). "special" is literally present in cue 0's EN text
("Today we have a special promise.") but is **not** a canonical gloss of 約束. Verified it fails every
token-match path in `gloss_span`/`_tok_matches`, not just superficially:

```
_gloss_tokens("約束", ent) == {'vow', 'promise'}
gloss_span("約束", ent, "special") == None                      # vetoed
_tok_matches("special", {'vow', 'promise'}) == False             # no bridge rescues it either:
  - not a direct/lemma token match ("special" lemma is itself)
  - doesn't end in "ly"
  - len>=4 derivational-suffix bridge (able/ably/ful/ness/ment/ly/ion/tion/sion/ation): no gloss
    token is a prefix of "special" with one of those suffixes
  - len>=5 recon-prefix bridge: no gloss token starts with "special"+4-more-chars
  - defend/decide bridges: don't apply (different word shape entirely)
```

The other three 約束 occurrences (ids 2-4, claiming the correct `"promise"`) and both 秘密 occurrences
(ids 5-6, claiming the correct `"secret"`) pass canonically (`gloss_span` returns the full-string span,
unchanged) — proving the conjunction check discriminates in **both** directions on the very same
fixture, as the DEFINE encouraged.

### Important, verified nuance: the veto's effect is only observable via stdout, not via the plan

Because `canonical_gloss()` unconditionally offers a lemma's own first gloss as a fallback candidate
(independent of which occurrences survived the veto), and because "promise" *is* literally present in
cue 0's EN text, the vetoed occurrence still injects correctly — via the fallback, not via the rejected
"special". Verified end-to-end by running the real pipeline (monkeypatched `load_merged`, real
`cli.main()`, real `--gloss-json`):

```
Dictionary veto: 約束 -/-> special
...
=== injections ===
0 約束 'promise'      <- id=1 (cue 0): vetoed "special" never appears; fallback "promise" does
1 約束 'promise'
2 約束 'promise'
5 約束 'promise'
3 秘密 'secret'
4 秘密 'secret'
=== plain layer, cue 0 ===
Today we have a special 約束.     <- "special" (context) untouched, "promise" swapped for 約束
```

This is correct, intentional system behavior (one mis-aligned occurrence must not sink an otherwise
well-attested word) — not a bug in the fixture. It means AC2 ("at least one veto decision... asserted")
is proven via the printed `Dictionary veto: 約束 -/-> special` line, which is one of the two assertion
routes the issue itself names. The plan/rendered-output assertions instead prove the *pass-through*
side (秘密, and 約束's other three occurrences) and prove the veto didn't break anything downstream.

---

## Test design

**Extend `tests/test_process_e2e_smoke.py`** (not a sibling file). Rationale: the file's own top
docstring already flags this exact gap as a tracked follow-up ("the dictionary-veto path itself is
therefore NOT exercised here... tracked as a separate, larger effort" — that effort is issue #10), and
the new tests reuse `block_network`, `run_process`, `JA_SUB`/`EN_SUB` directly. A sibling file would
duplicate all three for no isolation benefit (the new tests don't need different fixtures/imports, just
one more flag and one more constant).

**Update the stale docstring claim** (top of file, the `--no-dict` bullet) to stop asserting the gap is
open — it's closed by the two tests below:

```python
  --no-dict     skips jmdict.load_merged() for the first two tests below
                (--gloss-json is enough on its own for those). The
                dictionary-conjunction veto path itself IS exercised further
                down (test_process_e2e_dict_veto_fixture and its
                byte-identical sibling, issue #10) via --dict-json, a
                committed offline JMdict fixture - no network needed there
                either.
```

**New module-level constant** (alongside `FIXTURES`/`JA_SUB`/`EN_SUB`):

```python
DICT_FIXTURE = os.path.join(FIXTURES, "dict_promise_secret.json")
```

**New `GLOSS_JSON_DICT_VETO` constant** — structurally identical to the existing `GLOSS_JSON`, with
exactly one value changed (id=1's `en_word`):

```python
GLOSS_JSON_DICT_VETO = {
    "words": [
        {
            "lemma": "約束",
            "gloss": "promise",
            "reading": "やくそく",
            "skip": False,
            "matches": [
                {"id": 1, "en_word": "special"},  # deliberately non-canonical -> vetoed
                {"id": 2, "en_word": "promise"},
                {"id": 3, "en_word": "promise"},
                {"id": 4, "en_word": "promise"},
            ],
        },
        {
            "lemma": "秘密",
            "gloss": "secret",
            "reading": "ひみつ",
            "skip": False,
            "matches": [{"id": 5, "en_word": "secret"}, {"id": 6, "en_word": "secret"}],
        },
    ]
}
```

(id-to-cue mapping verified, not assumed: `select_new`'s sort order under `count_field="inj"` puts
約束 first — same as the existing `--no-dict` test's documented order — so ids 1-4 go to 約束's cues
`sorted({0,1,2,5}) = [0,1,2,5]` and ids 5-6 to 秘密's `sorted({3,4}) = [3,4]`. id=1 is therefore
unambiguously cue 0.)

**Extend the shared `run_process` helper** with one new optional parameter (existing 3-positional-arg
call sites are unaffected — fully backward compatible):

```python
def run_process(tmp_path, run_name, gloss_json_path, dict_json_path=None):
    """One clean `bll process` run: fresh --db, fresh output dir (matches the
    existing tests' fresh-tmp_path/--db pattern). Returns the four rendered
    layer paths and the plan sidecar path.

    dict_json_path: if given, passes --dict-json (the offline JMdict fixture
    seam - dictionary-conjunction veto path active) instead of --no-dict.
    """
    out_dir = tmp_path / run_name
    out_dir.mkdir()
    out_path = out_dir / "out.srt"
    dict_args = ["--dict-json", str(dict_json_path)] if dict_json_path else ["--no-dict"]
    rc = cli.main(
        [
            "--db",
            str(out_dir / "vocab.db"),
            "process",
            JA_SUB,
            EN_SUB,
            "-o",
            str(out_path),
            *dict_args,
            "--gloss-json",
            str(gloss_json_path),
        ]
    )
    assert rc == 0
    layers = {
        "adaptive": out_dir / "out.srt",
        "plain": out_dir / "out.plain.srt",
        "kana": out_dir / "out.kana.srt",
        "answers": out_dir / "out.answers.srt",
    }
    plan = out_dir / "out.plan.json"
    return layers, plan
```

**Two new test functions** (append to the file):

```python
def test_process_e2e_dict_veto_fixture(tmp_path, monkeypatch, capsys):
    """Exercises the dictionary-conjunction veto path end to end (issue #10):
    --dict-json loads a committed, hand-authored JMdict fixture (no network),
    so jm is not None and cmd_process's "Dictionary conjunction" block runs.

    id=1 (約束 vs "special") is vetoed: gloss_span returns None because
    "special" matches no canonical gloss of 約束 in the fixture. The other
    three 約束 occurrences (vs "promise") and both 秘密 occurrences (vs
    "secret") pass canonically.

    Note: the veto's effect on id=1 is only observable via the printed
    "Dictionary veto:" line, NOT via the final plan - canonical_gloss()
    always offers 約束's own gloss ("promise") as a fallback candidate, and
    "promise" IS literally present in cue 0's EN text, so the occurrence
    still injects correctly with the RIGHT word. That is by design (one
    mis-aligned occurrence doesn't sink an otherwise well-attested word) and
    is exactly why AC2 is asserted on stdout here.
    """
    block_network(monkeypatch)
    gloss_json_path = tmp_path / "gloss.json"
    gloss_json_path.write_text(json.dumps(GLOSS_JSON_DICT_VETO), encoding="utf-8")

    layers, plan = run_process(tmp_path, "run1", gloss_json_path, dict_json_path=DICT_FIXTURE)

    captured = capsys.readouterr()
    assert "Dictionary veto: 約束 -/-> special" in captured.out

    # 秘密 passes through canonically (untouched span, same as the --no-dict
    # smoke test): the conjunction check discriminates in both directions.
    kana_text = layers["kana"].read_text(encoding="utf-8")
    assert "秘密 (ひみつ)" in kana_text
    assert "約束 (やくそく)" in kana_text

    # The vetoed occurrence still injects correctly via 約束's own canonical
    # gloss ("promise") as a fallback candidate - the veto blocked the WRONG
    # claim, it did not sink the word.
    plain_text = layers["plain"].read_text(encoding="utf-8")
    assert "special 約束" in plain_text  # cue 0: "special" (context) kept, "promise" swapped
    assert "promise" not in plain_text.lower()
    assert "secret" not in plain_text.lower()

    plan_data = json.loads(plan.read_text(encoding="utf-8"))
    injections = plan_data["injections"]
    assert len(injections) == 6  # the veto didn't drop the occurrence
    assert any(
        inj["lemma"] == "約束" and inj["cue"] == 0 and inj["en_word"] == "promise"
        for inj in injections
    )


def test_process_e2e_dict_veto_two_clean_runs_byte_identical(tmp_path, monkeypatch):
    block_network(monkeypatch)
    gloss_json_path = tmp_path / "gloss.json"
    gloss_json_path.write_text(json.dumps(GLOSS_JSON_DICT_VETO), encoding="utf-8")

    layers1, plan1 = run_process(tmp_path, "run1", gloss_json_path, dict_json_path=DICT_FIXTURE)
    layers2, plan2 = run_process(tmp_path, "run2", gloss_json_path, dict_json_path=DICT_FIXTURE)

    for name in layers1:
        assert layers1[name].read_bytes() == layers2[name].read_bytes(), (
            f"{name} layer differs between two clean runs"
        )
    assert plan1.read_bytes() == plan2.read_bytes(), (
        "plan.json sidecar differs between two clean runs"
    )
```

Both reuse `block_network` (zero network, same guard as the existing two tests). The second mirrors
`test_process_e2e_two_clean_runs_byte_identical` exactly, substituting the dict-active helper call —
proving AC3 (determinism) for this path specifically, not just inheriting it from the `--no-dict` tests.

**Acceptance-criteria traceability:**

| AC | Proof | Where |
|----|-------|-------|
| AC1 (no network/Ollama/Claude, automated) | `block_network` guard active; `--gloss-json` bypasses the aligner backend entirely (no Ollama reachability check, no Claude CLI spawn — same mechanism the existing tests already rely on) | both new tests |
| AC2 (>=1 veto decision asserted) | `"Dictionary veto: 約束 -/-> special" in captured.out` (vetoed) + kana/plain assertions for 秘密 and 約束's surviving occurrences (passed-through) | `test_process_e2e_dict_veto_fixture` |
| AC3 (byte-identical across 2 clean runs) | full per-layer + plan.json byte comparison | `test_process_e2e_dict_veto_two_clean_runs_byte_identical` |

---

## Typing/lint compliance

- `bll/jmdict.py` is under `[[tool.mypy.overrides]] ignore_errors = true` — `load_merged`'s signature
  is left unannotated, matching every other function in this file (none of `load`, `load_compounds`,
  `build`, `entry`, etc. carry type hints); adding hints to only the one parameter I touched while its
  neighbors stay bare would be inconsistent, not more correct. No new helper function is introduced in
  this file (the change is 6 lines inside the existing function), so the DEFINE's "new helper functions
  should be typed normally" instruction doesn't add an obligation here.
- `bll/cli.py`: `cmd_process` stays `@typing.no_type_check`; the one-line call-site edit inside it needs
  no annotation. The new argparse block is plain `pp.add_argument(...)` calls, consistent with every
  neighboring flag. No new helper function is introduced in this file either.
- **Verified, not assumed**: copied the planned edits into scratch copies of `bll/cli.py`,
  `bll/jmdict.py`, and `tests/test_process_e2e_smoke.py` and ran the real toolchain from this repo's
  `pyproject.toml`:
  - `uv run ruff check` → `All checks passed!`
  - `uv run ruff format --check` → clean (one round-trip fix needed and applied: the help text was
    rewritten to avoid embedded double quotes so ruff's quote-style preference doesn't churn it — see
    Seam design).
  - `uv run mypy bll/` → `Success: no issues found in 9 source files`
  - `uv run pytest tests/` → **25 passed** (23 existing + 2 new; matches the DEFINE's "suite floor is
    23... whatever this adds raises that count").

---

## Verify command

Standalone, outside pytest, zero network, zero spend (no Ollama/Claude call — `--gloss-json` bypasses
the aligner backend the same way the existing e2e smoke test does):

```bash
cat > /tmp/bll-dict-veto-demo.gloss.json <<'EOF'
{
  "words": [
    {"lemma": "約束", "gloss": "promise", "reading": "やくそく", "skip": false,
     "matches": [{"id": 1, "en_word": "special"}, {"id": 2, "en_word": "promise"},
                 {"id": 3, "en_word": "promise"}, {"id": 4, "en_word": "promise"}]},
    {"lemma": "秘密", "gloss": "secret", "reading": "ひみつ", "skip": false,
     "matches": [{"id": 5, "en_word": "secret"}, {"id": 6, "en_word": "secret"}]}
  ]
}
EOF
uv run bll --db /tmp/bll-dict-veto-demo.db process \
  tests/fixtures/promise_secret.ja.srt \
  tests/fixtures/promise_secret.en.srt \
  -o /tmp/bll-dict-veto-demo-out.srt \
  --dict-json tests/fixtures/dict_promise_secret.json \
  --gloss-json /tmp/bll-dict-veto-demo.gloss.json
```

**Expected** (verified against the drafted seam in this session): exit code `0`; stdout contains the
line `Dictionary veto: 約束 -/-> special`; `/tmp/bll-dict-veto-demo-out.plain.srt` shows all six
subtitle cues correctly injected (約束 in cues 1/2/3/6, 秘密 in cues 4/5 — 1-indexed `.srt` cue
numbers), including cue 1 reading `Today we have a special 約束.` (context word "special" untouched,
only "promise" replaced).

---

## Out of scope (explicit, so build doesn't scope-creep)

- **Full veto-logic unit coverage** (e.g. exhaustively testing `gloss_span`/`_tok_matches`'s bridge
  rules in isolation) — belongs with the selection/veto module's own tests per the DEFINE; this issue
  adds one e2e proof point, not a unit suite.
- **CI download caching** for the real JMdict network path — unrelated to this fixture; noted only
  because the existing test file's docstring already flagged it as a tempting follow-up. Not built
  here.
- **Argparse validation for `--dict-json` + `--no-dict` together** — precedence (no-dict wins) is
  simple enough by inspection that a dedicated test/guard would be ceremony, not safety.
- **`_gloss_token_cache` global-state fragility**: `jmdict._gloss_tokens` caches by lemma string alone
  in a process-global dict, so two tests in the same pytest session asserting *different* glosses for
  the *same* lemma string could see stale cached tokens depending on run order. Checked: nothing else
  in this suite uses "約束"/"秘密" as a jmdict key (only `test_heteronym_reading.py` touches real
  JMdict data, using unrelated lemmas 角/方/今日), so this fixture introduces no collision today. Not
  fixed here — it's a pre-existing property of the production caching design, out of this issue's
  blast radius.
- **`cmd_render`'s `--rebake-db` path**: untouched; it keeps calling `load_merged()` with no
  `fixture_path`.

---

## File manifest

| File | Action | Purpose |
|------|--------|---------|
| `bll/jmdict.py` | Modify | `load_merged` gains `fixture_path=None`; short-circuits to a direct JSON read when set |
| `bll/cli.py` | Modify | new `--dict-json` flag on the `process` subparser; `cmd_process`'s `load_merged()` call threads it through |
| `tests/fixtures/dict_promise_secret.json` | Create | the committed, hand-authored dictionary fixture (2 entries) |
| `tests/test_process_e2e_smoke.py` | Modify | stale docstring line updated; `DICT_FIXTURE` + `GLOSS_JSON_DICT_VETO` constants added; `run_process` gains `dict_json_path=None`; two new test functions appended |

No dependency or CI workflow changes — `--dict-json` is pure argparse + stdlib `json`/`open`, already
available.

Referenced throughout as issue #10.
