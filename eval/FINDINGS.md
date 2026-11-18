# Findings log

What each evaluation run actually caught. Numbered so the code and the README
can point at an entry rather than restating it.

The point of keeping this is that a rule-based checker has no training curve
to look at. This log is the only record of whether the rules are getting
better or just getting longer.

---

## 001 — Plain past `〜た` was not detected at all

**Status:** fixed · **Found by:** `evaluate.py`, first run

`PLAIN_ENDINGS` matched the lexicalized forms `だった`, `なかった`, `かった`,
but had no general case for plain past `〜た`. So every ordinary past-tense
line came back `NEUTRAL`:

```
雨が降り続いていた。   gold PLAIN   pred NEUTRAL
息を殺したような音で応えた。  gold PLAIN   pred NEUTRAL
光は…折りたたまれていった。  gold PLAIN   pred NEUTRAL
```

`〜た` is the default ending of Japanese narration. The classifier was blind
to the single most common form in the corpus it exists to read.

Fixed by adding `(?<!まし)(?<!でし)た` to the alternation, with the lookbehinds
handing polite past back to `POLITE_ENDINGS`.

| | before | after |
|---|---|---|
| register accuracy | 0.857 | **0.943** |
| register macro F1 | 0.798 | **0.893** |
| errors | 5 | 2 |

**Why the tests missed it.** 68 unit tests passed throughout. They were
written by the person who wrote the rules, so they tested the forms the rules
already handled — `です`/`ます` and `だ`/`である` — and never the form nobody
had thought about. A test suite cannot find a blind spot it shares.

This entry is the argument for the whole directory.

---

## 002 — Two classifier errors left standing deliberately

**Status:** open by choice · **Found by:** `evaluate.py`

After 001, two errors remain:

```
[seed014]  動くな。    gold NEUTRAL  pred PLAIN
[seed024]  お待ちを。   gold POLITE   pred NEUTRAL
```

`動くな。` is a bare imperative and carries no reliable register on its own.
`お待ちを。` is 美化語 with an elided `ください` — a reader hears the
politeness, the regex does not.

**Not fixed, on purpose.** Each is a single example. A pattern added to catch
one line in a 35-line set is fitted to that line, and the honest way to find
out whether these generalize is to grow the set until there are ten of each.
Patching them now would raise the score and teach nothing.

Revisit when the set passes ~350 lines. If either form still appears once,
leave it alone again.

---

## 003 — A tier-1 classifier gap was manufacturing a tier-2 finding

**Status:** fixed by 001 · **Found by:** re-running the samples after 001

Before 001, `graph.merge_register` reported a register conflict at
`chapter_two` in the Ren'Py sample. Its two incoming paths:

```
gate_open:menu1   門をくぐる                      → PLAIN
watch_light       光は…折りたたまれていった。      → NEUTRAL (wrong; 001)
```

The check takes the first segment on each parent path whose register is
POLITE or PLAIN. Because the narration line was misread as NEUTRAL, the check
skipped past it to the next line in `watch_light` — Aldis's
`いつまでもここには居られません。` (POLITE) — and reported a plain path
converging with a polite one.

Both parents are plain. There was no conflict. The finding was an artifact.

*Since revised:* the check now reads each parent's **last** line, the one the
player hears just before the merge, and skips menu choices. The scenario above
describes the code as it was.

**What this shows.** A gap in a tier-1 classifier surfaced as a false positive
in a tier-2 check two modules away, on a different node, with a plausible
message. No amount of reading `graph.py` would have found it. The composition
of checks needs measuring, not just the checks.

---

## 004 — The threshold sweep cannot yet select a value

**Status:** open, blocked on data · **Found by:** `sweep.py`

`MIN_LINES = 4` and `MIN_RATIO = 0.8` in `RegisterConsistency` were set by
intuition. The sweep exists to replace that and currently cannot:

```
MIN_LINES = 3, MIN_RATIO = 0.55  → F1 1.000
```

with several settings tied at the top. With **2 register-drift positives** in
the set, most of the 45 combinations are indistinguishable, and the winner is
noise. (After 006 there is only one positive in scope, and 40 of 45 tie.)

**Neither threshold has been changed on the strength of this**, and neither
should be until the set has 30+ positives. Reporting a tuned value from two
examples would be worse than admitting the value is a guess, because it would
look like it had been measured.

---

## 005 — First-person detector attributes quoted pronouns to the speaker

**Status:** open · **Found by:** `evaluate.py`

```
[seed035]  「俺が行く」と、そう仰っていました。   gold (none)  pred 俺
           speaker: Sister Aldis
```

Aldis is quoting Kestrel. `_first_person` does a substring scan and has no
notion of quotation, so it attributes `俺` to her — and `voice.first_person`
would then report her as drifting from `わたくし`.

Fixing this needs the detector to skip content inside `「…」` and `『…』`,
which is a real change with its own failure modes (nested quotes, a quote
running across two segments). Held until the set has more quoted-speech rows
than this one, so the fix can be evaluated rather than asserted.

The first-person detector is at 0.971 accuracy and this is its only error.

---

## 006 — The sweep was scoring a positive the check cannot produce

**Status:** fixed · **Found by:** reading `sweep.py` against `voice.py`

`voice.register` skips menu choices (`_speaker_groups` filters `is_choice`).
`sweep.py` built every labeled row as an ordinary line, because `labels.csv`
had no way to say which rows were choices. So seed026:

```
[seed026]  Player  門とは何かを尋ねます   PLANTED choice register mismatch
```

was one of the sweep's two positives, and at the current setting the sweep
"caught" it. The production check never would. Half of the sweep's recall
came from a check configuration that does not ship.

Fixed by adding an `is_choice` column (filled by `ingest.py`, set on
seed025–029) and having the sweep build segments with it. Choice positives are
now listed as out of scope rather than scored. That leaves **one** in-scope
positive, and 40 of 45 threshold settings tie at F1 1.000, up from 18.

**What this shows.** A harness is only evidence about the check it actually
runs. The sweep and the check had drifted apart on one boolean, and the
number still looked plausible. It looked better, in fact.
