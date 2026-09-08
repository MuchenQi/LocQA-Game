# locqa

Localization QA for game scripts, EN → JA. Reads XLIFF, loc sheets, and Ren'Py
scripts; runs 26 checks; reports to console, JSON, JUnit, CSV, or an HTML proof
sheet. No network access, no API keys, no model calls.

```
locqa check script.xlf -c project.yml
locqa check dialogue.rpy -t dialogue_ja.rpy -c project.yml -r html -o report.html
locqa graph dialogue.rpy --dot | dot -Tsvg > graph.svg
locqa pseudo strings.csv -o pseudo.csv
```

---

## Why this exists

Most localization QA tooling treats a game script as a list of strings. That
assumption is wrong in two ways, and both of them cost money.

**A game script is a graph, not a list.** Dialogue branches, merges, and gets
re-entered. A translator working down a spreadsheet sees row 412 next to row
411; a player sees it after whichever of four branches they took. Bugs that
live in the difference are invisible to string-level review.

**Japanese encodes things English does not.** Politeness level, first-person
pronoun, and sentence-final particles are obligatory in Japanese and absent
from the English source. There is no source-side signal to diff against, so
these drift silently, and the English-speaking reviewer cannot see the drift
because English has no surface form that carries it.

locqa is built in two tiers. Tier 1 is the table-stakes string checking that
makes it usable in a pipeline. Tier 2 is the part that needs the graph.

---

## Tier 1 — string-level

| Check | What it catches |
|---|---|
| `placeholder.parity` | `{ITEM}` dropped, invented, or duplicated in the target |
| `placeholder.corrupt` | Full-width `｛｝％＄` from a Japanese IME — substitution silently stops firing |
| `tag.parity` | Rich-text markup dropped, unbalanced, or crossed |
| `length.budget` | Overflow in half-width units, with placeholders expanded to worst case |
| `ja.charset` | 機種依存文字 outside the build's font atlas (`①`, `㈱`, `～`) |
| `ja.kinsoku` | 禁則処理 violations — `。` stranded at a line head, `「` at a line end |
| `ja.punctuation` | Half-width `.` `,` `!` in Japanese prose, ASCII `...`, odd 三点リーダー runs |
| `ja.wave_dash` | U+301C vs U+FF5E mixed in one file — identical on screen, different in Shift-JIS |
| `ja.mojibake` | Replacement chars and UTF-8-read-as-Latin-1 signatures |
| `ja.untranslated` | Empty, source-copied, or no kana/kanji present |
| `ja.spacing`, `ja.number_width` | Full-width space artifacts, mixed digit widths |
| `term.glossary`, `term.forbidden`, `term.dnt` | Approved renderings, banned renderings, trademarks that must survive |
| `consistency.source` | One source string rendered several ways |
| `consistency.target` | Several source strings collapsed into one target |

## Tier 2 — graph-level

These require branch structure and produce nothing on a flat file.

| Check | What it catches |
|---|---|
| `voice.register` | A speaker drifting between です・ます and だ・である across the script |
| `voice.first_person` | 俺 → 僕 mid-script. Same character, different person, to a Japanese reader |
| `voice.final_particle` | A persona-marking 語尾 that appears exactly once across a large speaker set |
| `graph.choice_parallelism` | Sibling menu options that don't share a grammatical shape or register |
| `graph.anaphora` | A line opening with そう / はい / でも at a node reached from several parents |
| `graph.merge_register` | Paths in different registers converging on one node |
| `graph.unreachable` | Translated content with no path from an entry point — wordcount you paid for and nobody sees |
| `graph.dangling` | A branch pointing at a label that doesn't exist |

### Reachability weighting

Findings carry a `reach` score derived from BFS depth from the entry node.
A bug at depth 1 is on the mainline; a bug at depth 12 sits behind a pile of
flag checks. Severity is scaled by reach for sort order only — never for
correctness, and never below a configurable floor, because "hard to reach" is
a triage hint and not permission to ship a broken string.

---

## Design notes

**One canonical model, N parsers.** Every format lowers into `Script` /
`Node` / `Segment`. A check written against the model works on all three
formats unchanged; adding a format costs one parser rather than 26 check
rewrites. Checks that need branch structure declare `requires_graph` and are
skipped rather than producing noise on flat input.

**Width is measured in half-width units.** Japanese UI budgets are quoted in
全角 counts, but real game strings mix Japanese and Latin, so half-width units
are the only honest common denominator. East Asian Ambiguous characters count
as wide, because the game will render them in a Japanese font.

**Placeholders are expanded before measuring.** A box that fits `{ITEM}` but
not a fourteen-character item name is a bug that surfaces in one save file out
of ten. Length and kinsoku checks wrap the same expanded string, so the line
break lands where it will actually land.

**Overlapping findings are collapsed.** A string that overflows its width will
trivially exceed its line budget too; reporting both is one bug counted twice,
and doubled counts are how a QA report loses a reviewer's trust.

**The register classifier refuses to guess.** 体言止め, interjections, and
fragments are extremely common in game dialogue and carry no register
information. Classifying them as either polite or plain is the main source of
noise in a naive implementation, so they return `NEUTRAL` and are excluded from
the majority vote. A speaker whose lines are genuinely mixed is left alone
rather than flagged eighty times.

**Checks report the outlier, not both sides.** When three menu options are in
plain form and one is in です・ます, the finding is on the one.

---

## Configuration

```yaml
charset: shift_jis          # font atlas repertoire; use cp932 for vendor extensions
default_placeholder_width: 8
placeholder_widths:
  "{ITEM}": 14              # longest value in the DB
glossary:
  "Save Point": ["セーブポイント"]
forbidden:
  "保存地点": "Save Point is a named system; use the katakana form."
do_not_translate: ["Aetherbound"]
severity_overrides:
  consistency.target: INFO
reach_weighting: true
min_reach: 0.45
```

Loc sheets need no column configuration — headers are matched against a
synonym table, so `key` / `StringID` / `identifier` and `japanese` / `JP` /
`target_text` all resolve.

---

## The HTML report

Findings that are fundamentally spatial — a string overrunning its box, a
line break stranding a `。` at a line head — render on a 原稿用紙 character
grid with the budget drawn as a rule and overflowing cells marked in
correction red. The reviewer sees the overflow in the same terms the engine
will render it, rather than reading a number and imagining it.

---

## CI

Exit codes are the contract: `0` clean, `1` findings at or above `--fail-on`,
`2` the tool itself failed. That third code matters — a crashed linter must
not read as a passing build.

```yaml
- run: locqa check loc/ja.xlf -c loc/project.yml --fail-on blocker
- run: locqa check loc/ja.xlf -c loc/project.yml -r junit -o results.xml --fail-on none
  if: always()
```

---

## Layout

```
locqa/
  model.py          Script / Node / Segment, width and wrapping, reachability
  parsers/          xliff.py · sheet.py · renpy.py
  checks/
    base.py         Check protocol, registry, config, runner, reach weighting
    integrity.py    placeholders, markup
    japanese.py     orthography, encoding, typesetting
    text.py         length, terminology, consistency
    voice.py        register and character-voice drift
    graph.py        branch-structural checks
  report.py         console · JSON · JUnit · CSV
  htmlreport.py     proof sheet with the character grid
  pseudo.py         pseudolocalization
tests/              71 tests, stdlib unittest
samples/            Ren'Py script + JA translation, loc sheet, XLIFF 2.0, config
eval/               labeled set, annotation guidelines, ingest + classifier + threshold harnesses
```

## Running

```bash
pip install -e .
python -m unittest discover -s tests -v
locqa checks              # list every check with its default severity
```

## Evaluation

The voice checks are rule-based, so `eval/` measures them rather than asserting
they work. Two harnesses: `evaluate.py` scores `classify_register` against
hand-labeled lines, `sweep.py` scores the `voice.register` check itself across
its thresholds.

It has already paid for itself. On its first run it found that plain past `〜た`
— the default ending of Japanese narration — was not detected at all. Fixing it
moved classifier accuracy from 0.857 to 0.943. That gap had survived 68 passing
tests, because the tests were written by the person who wrote the rules and
shared their blind spot.

Current seed set is 35 self-authored lines, which is a tenth of the target and
not a publishable number. `eval/README.md` says so, and the threshold sweep
reports that it cannot yet select a value — with 2 positives, 18 of 45
combinations tie. `MIN_LINES` and `MIN_RATIO` remain unvalidated intuition until
the set grows.

```bash
python eval/ingest.py FILE  # any parsable script → label-ready rows
python eval/evaluate.py     # confusion matrix + error list + anchoring check
python eval/sweep.py        # precision/recall across thresholds
```

`ingest.py --predict` can pre-fill the classifier's guess to speed up labeling,
but it writes into `register_pred`, never `register_gold`, and holds back a
blind slice. `evaluate.py` then compares agreement on the blind and assisted
rows: if the assisted rows agree with the classifier much more, the annotator
was anchored and those labels aren't independent evidence.

See `eval/GUIDELINES.md` for the annotation protocol and `eval/FINDINGS.md` for
the log of what each run caught.

## Known limits

- The register and 語尾 classifiers are rule-based, tuned for precision over
  recall. Two known errors stand deliberately: `動くな。` (bare imperative,
  register-ambiguous) classifies as plain, and `お待ちを。` (美化語 with elided
  ください) classifies as neutral. Both are single-example failures, and patching
  them now would be over-fitting — see `eval/FINDINGS.md` 002.
- A learned classifier over a tagged corpus is the obvious next step. The rules
  exist to be the baseline it has to beat, and `eval/` is how that comparison
  gets made.
- `graph.anaphora` uses an opener list, not semantics. It flags the position
  where a context bug is possible; a human confirms.
- The Ren'Py parser recovers control flow, not full semantics: conditional
  bodies contribute their edges, but `if` guards on choices are ignored, so
  reachability is an upper bound.
- Only EN → JA target rules are implemented. The tier-1 engine and the graph
  tier are language-neutral; `checks/japanese.py` and `checks/voice.py` are
  the locale-specific surface.
