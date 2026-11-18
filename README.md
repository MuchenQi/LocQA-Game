# locqa

Localization QA for game scripts, EN → JA. Reads XLIFF, loc sheets, and Ren'Py
scripts; runs 26 checks; reports to console, JSON, JUnit, CSV, or an HTML proof
sheet. No network access, no API keys, no model calls.

There is also a browser front end with accounts and saved runs — see
[Web front end](#web-front-end).

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

## Tier 2 — graph- and voice-level

The `graph.*` checks require branch structure and are skipped on a flat file.
The `voice.*` checks need speaker attribution instead, which Ren'Py carries
natively, XLIFF through a `speaker` note, and a loc sheet through a speaker
column.

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
| `graph.solo_choice` | A menu with one option — usually a branch lost in conversion |

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

**One bug, one finding.** A string that overflows its width will trivially
exceed its line budget too; a polite menu option among plain ones is trivially
also the wrong grammatical shape. Reporting both is one bug counted twice, and
doubled counts are how a QA report loses a reviewer's trust. The runner drops
exact repeats; deciding that two different findings are the same bug needs
knowledge only the check has, so each check that can overlap with itself
reports the more specific finding and suppresses the other.

**Layout is measured on the rendered string.** Markup is removed and
placeholders are expanded before anything is measured or wrapped, so
`<b>決定</b>` costs four units, not eleven.

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
  cli.py            check · graph · pseudo · checks, and the exit codes
locqa_web/          browser front end (optional extra; see "Web front end")
  app.py            FastAPI routes: auth, uploads, configs, run pages, JSON API
  jobs.py           the job queue: claim, lease, fence, run in a child process
  analysis.py       the child process: bytes + config in, report out
  models.py         users, sessions, saved configs, runs (which are also the queue)
  worker.py         standalone worker process
tests/              92 core tests + 37 web tests, stdlib unittest
samples/            Ren'Py script + JA translation, loc sheet, XLIFF 2.0, config
eval/               labeled set, annotation guidelines, ingest + classifier + threshold harnesses
```

## Running

```bash
pip install -e .
python -m unittest discover -s tests -v
locqa checks              # list every check with its default severity
```

## Web front end

`locqa_web` puts the checker behind a browser, for the translators and
producers who will never install a Python CLI. Upload a locale file, pick a
config, and the result page frames the same HTML proof sheet the CLI writes.
Accounts keep a history of past runs and a set of saved configs.

```bash
pip install -e ".[web]"
uvicorn locqa_web.app:app --reload      # http://localhost:8000
```

That is the whole local setup: SQLite in the working directory, and the
worker running as a thread inside the web process. For two processes:

```bash
LOCQA_INLINE_WORKER=0 uvicorn locqa_web.app:app
python -m locqa_web.worker              # as many as you like
```

### Not running the checker in the request

The request handler never runs locqa. `POST /runs` stores the upload, inserts
a `queued` row, and redirects; a worker runs the checks and writes the result
back; the run page polls `/api/runs/{id}` until it has one. A slow file costs
a worker, not a request thread, and the user can close the tab.

The queue is the `runs` table itself rather than Redis or a broker — one
fewer service to keep alive on a free tier, and no way for a job and its
result to disagree. What makes that safe (`locqa_web/jobs.py`):

- **Claiming is compare-and-set.** The UPDATE that marks a row `running`
  repeats the "still claimable" condition, so of two racing workers exactly
  one gets `rowcount == 1`. On Postgres the candidate is also selected
  `FOR UPDATE SKIP LOCKED`, so a second worker moves on instead of waiting.
- **A claim is a lease.** If a worker dies mid-run, `locked_until` lapses and
  another worker retries the run, up to `LOCQA_MAX_ATTEMPTS`; after that it
  fails with a message saying so.
- **`attempts` is a fencing token.** A worker that lost its lease and wakes
  up later cannot overwrite the newer attempt's result.
- **The checks run in a child process with a hard timeout**
  (`LOCQA_JOB_TIMEOUT`, default 60 s). A hostile or pathological file is
  killed; the worker never blocks on it.
- **Only lost workers are retried.** An unparseable file, an invalid config,
  or a timeout would fail identically the second time, so they fail once with
  a message the user can act on.

### Other decisions

- **Uploads and reports live in the database**, not on disk. Free tiers give
  a container an ephemeral filesystem, and the worker may be another machine.
- **Each run snapshots its config.** Editing a saved config never changes
  what an old report says it was checked against.
- **The proof sheet is served sandboxed** (`Content-Security-Policy: sandbox`),
  so it runs in an opaque origin with no access to the app's session.
- **Passwords are scrypt** (stdlib `hashlib`); session tokens are random and
  stored only as hashes; forms carry a per-session CSRF token on top of
  `SameSite=Lax` cookies; other users' runs are 404, not 403.
- **XLIFF with a DOCTYPE is refused.** XLIFF never needs one, and refusing it
  closes off entity-expansion tricks from uploaded files.

### Deploying

`Dockerfile` builds one image for both processes, so any platform that runs a
container works. `render.yaml` is a Render blueprint for one web service plus
Postgres, with the worker as a thread inside the web service. Set:

| variable | default | |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./locqa_web.db` | `postgres://…` is rewritten for psycopg 3 |
| `LOCQA_SECURE_COOKIES` | `0` | set `1` anywhere served over HTTPS |
| `LOCQA_INLINE_WORKER` | `1` | `0` when running `locqa-worker` separately |
| `LOCQA_JOB_TIMEOUT` | `60` | seconds per run |
| `LOCQA_MAX_UPLOAD_BYTES` | `5242880` | per file |

### Web: known limits

- Tables are created with `create_all`; there are no migrations yet, so a
  schema change on a live database needs Alembic first.
- The Postgres paths (`SKIP LOCKED`, psycopg) are exercised by SQL compilation
  only; the test suite runs on SQLite.
- No rate limiting on sign-in, no email verification, no password reset.
- The request-size guard reads `Content-Length`; a chunked upload with none
  is still capped per file, but only after it has been spooled.
- The worker polls once a second when idle. Postgres `LISTEN/NOTIFY` would
  make pickup instant.

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
reports that it cannot yet select a value. The set has two register-drift
positives, but one is a menu choice, which `voice.register` never judges, so
only one is in scope, and 40 of 45 combinations tie (`eval/FINDINGS.md` 006).
`MIN_LINES` and `MIN_RATIO` remain unvalidated intuition until the set grows.

```bash
python eval/ingest.py FILE --append  # any parsable script → label-ready rows
python eval/evaluate.py     # confusion matrix + error list + anchoring check
python eval/sweep.py        # precision/recall across thresholds
```

`ingest.py` writes to `eval/labels.csv` by default and refuses to replace an
existing file: pass `--append` to add rows, or `--force` to overwrite.

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
- The 一人称 detector requires a pronoun to be followed by a particle,
  suffix, or punctuation, and a hiragana pronoun to not follow hiragana, so
  that そのうち and 私立 are not read as pronouns. The cost is recall:
  `でもぼくは` is missed. It still attributes pronouns inside quotations to
  the speaker (`eval/FINDINGS.md` 005).
- `graph.merge_register` compares the last line of each incoming path
  regardless of who spoke it, so two speakers who are each consistent can
  still read as a conflict. It is a MINOR check for that reason.
- A learned classifier over a tagged corpus is the obvious next step. The rules
  exist to be the baseline it has to beat, and `eval/` is how that comparison
  gets made.
- `graph.anaphora` uses an opener list, not semantics. It flags the position
  where a context bug is possible; a human confirms.
- The Ren'Py parser recovers control flow, not full semantics: conditional
  bodies contribute their edges, but `if` guards on choices are ignored, so
  reachability is an upper bound. The exception is a computed target —
  `jump expression` or `call expression` — which cannot be followed, so a
  label reached only that way is reported unreachable.
- Only EN → JA target rules are implemented. The tier-1 engine and the graph
  tier are language-neutral; `checks/japanese.py` and `checks/voice.py` are
  the locale-specific surface.
