# eval

The voice checks are rule-based. Nothing about them is learned, so there is no
held-out accuracy number that falls out of training, and the unit tests cannot
supply one: they were written by the same person who wrote the rules, against
the same intuitions, so they agree with the rules by construction.

This directory exists to disagree with the rules.

## The two harnesses

They measure different things, and both are needed.

| | Question | Unit |
|---|---|---|
| `evaluate.py` | Does `classify_register` label this line correctly? | one line |
| `sweep.py` | Given that classifier, does `voice.register` fire on the lines a human called drift? | one speaker's worth of lines |

A classifier can be accurate while the check built on it is useless, because
the check adds two thresholds (`MIN_LINES`, `MIN_RATIO`) that the classifier
knows nothing about. `sweep.py` is what tests those.

```bash
python eval/ingest.py FILE      # any parsable script → label-ready rows
python eval/evaluate.py         # confusion matrix, error list, anchoring check
python eval/evaluate.py --errors
python eval/sweep.py            # precision/recall across both thresholds
python eval/sweep.py --plot sweep.png
```

## Current state

**35 lines, all self-authored. Do not publish these numbers.**

| | |
|---|---|
| lines | 35 |
| speakers | 4 (13 / 12 / 5 / 5) |
| register-drift positives | 2 |
| pronoun-drift positives | 1 |
| register accuracy | 0.943 |
| register macro F1 | 0.893 |
| first-person accuracy | 0.971 |

Three things are wrong with this set, and they are worth stating plainly
because the numbers above look respectable and are not yet evidence.

1. **It is a tenth of the target.** ~350 lines, ~30 per speaker, is the point
   at which per-speaker checks can be evaluated at all. `evaluate.py` prints
   `← thin; 30+ needed for the drift sweep` against every speaker at present.
2. **Two positives cannot select a threshold.** `sweep.py` reports several
   settings tied at F1 1.000. A sweep this small selects noise, so `MIN_LINES`
   and `MIN_RATIO` remain unvalidated intuition. They have not been changed on
   the strength of it, and should not be.
3. **The author wrote both the rules and the lines.** That is the exact bias
   the harness is supposed to catch, so the current set is the weakest possible
   evidence for the rules being right — while still being strong enough to have
   caught a real bug (see `FINDINGS.md` 001).

## Growing the set

See `GUIDELINES.md` for the annotation protocol and `SOURCES.md` for what may
be committed. In short: real translated game text, ingested with `ingest.py`,
labeled by someone who did not write the rules, with `--predict --blind`
enabled so the anchoring check has something to measure.

Do not generate the lines. Synthetic Japanese written by whoever — or
whatever — also wrote the rules reproduces their blind spots faithfully, which
raises the score and lowers the information to zero. The set is only worth
having if it can surprise you.
