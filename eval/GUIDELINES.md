# Annotation guidelines

Read this before labeling. It takes five minutes and it is the difference
between a gold set and a pile of opinions.

You are labeling what a Japanese reader would perceive, not what a rule should
output. If you find yourself thinking "the classifier would probably say
PLAIN here", stop — that is the failure mode this whole directory exists to
detect.

## The schema

`labels.csv`, one row per line of dialogue:

| column | who fills it | values |
|---|---|---|
| `line_id` | `ingest.py` | stable hash; never edit |
| `speaker` | `ingest.py` | character name, or `Narrator` |
| `is_choice` | `ingest.py` | `1` for a menu option, else `0`; never edit |
| `source_en` | `ingest.py` | English source |
| `target_ja` | `ingest.py` | Japanese target — **the thing you are judging** |
| `register_gold` | you | `POLITE` · `PLAIN` · `NEUTRAL` · `MIXED` |
| `pronoun_gold` | you | the 一人称 used, or empty |
| `is_drift` | you | `1` · `0` |
| `drift_type` | you | `register` · `pronoun` · `particle` — only when `is_drift=1` |
| `notes` | you | anything; put licensing here on ingest |

Leave a cell empty rather than guessing. An empty `register_gold` is skipped
by the harness; a wrong one is counted forever.

## register_gold

Judge the **sentence-final form**, per sentence. If a line has several
sentences, judge the line as a whole by what the endings do.

**POLITE** — です・ます and its family: `ます` `ません` `でした` `ましょう`
`ください` `ございます`, plus 美化語 where the polite auxiliary is present.

> 行きます。 · お許しください。 · 居られません。 · そうですね。

**PLAIN** — 常体. The dictionary form, `だ`/`である`, plain past `〜た`,
negatives in `〜ない`, i-adjective predicates.

> 遅いぞ。 · 雨が降り続いていた。 · ただの扉だ。 · 何も言わない。

**NEUTRAL** — carries *no register information*. This is the most important
label and the one most often got wrong, because it feels like a dodge. It is
not. Game dialogue is full of forms that a Japanese reader cannot assign a
politeness level to at all:

> 体言止め (noun-final): セーブポイント · 門を開く者
> interjections: はい · ええ · ほう
> fragments: 光の中へ · あの社には…

If you would have to know the character to decide, it is NEUTRAL.

**MIXED** — one line that genuinely does both, e.g. a polite opening and a
plain close. Rare. Do not use it for "I'm unsure between two labels" — use
NEUTRAL for that, or leave it empty.

### The hard cases, and how they are resolved here

These are settled decisions. Follow them even if you would argue otherwise,
because consistency across the set matters more than any single call.

| form | label | why |
|---|---|---|
| `動くな。` | `NEUTRAL` | bare imperative; register-ambiguous by itself |
| `お待ちを。` | `POLITE` | 美化語 with elided ください; a reader hears the politeness |
| `行こう。` | `PLAIN` | volitional, plain |
| `行きましょう。` | `POLITE` | volitional, polite |
| `そう。` | `NEUTRAL` | interjection, not 常体 |
| `だろ。` / `でしょ。` | `PLAIN` / `POLITE` | clipped but the auxiliary is audible |
| quoted speech | judge the **frame**, not the quote | see below |

The classifier currently gets the first two of these *wrong* and that is
deliberate — see `FINDINGS.md` 002. Label them correctly anyway. The gold set
records what is true, not what the code does.

## pronoun_gold

Fill in only the 一人称 the **speaker uses for themselves**: `俺` `僕` `私`
`わたくし` `あたし` `うち` `わし` `拙者`.

Leave it empty when the line contains no first-person pronoun, and — this is
the trap — when the pronoun appears **inside a quotation of someone else**:

> 「俺が行く」と、そう仰っていました。

Sister Aldis is quoting Kestrel. Her 一人称 is not 俺. `pronoun_gold` is
empty. The detector currently gets this wrong (`FINDINGS.md` 005), which is
exactly why the row belongs in the set.

## is_drift and drift_type

`is_drift` is a judgement about the line **in the context of that speaker
across the whole script**, not about the line alone. A polite line is not
drift; a polite line from a character who is plain everywhere else is.

Set `is_drift=1` and pick one `drift_type`:

- `register` — the politeness level changed with no narrative reason
- `pronoun` — the 一人称 changed
- `particle` — a persona-marking 語尾 appeared or vanished

If the shift is clearly motivated — a character dropping formality at a
turning point, a servant addressing a master differently — that is **not
drift**. Set `is_drift=0` and say why in `notes`. Those rows are the valuable
ones: they are what stops the check being tuned into a machine that flags
every deliberate choice.

Only `drift_type=register` rows count as positives for `sweep.py`, and only
when `is_choice=0`: `voice.register` never judges menu options, so the sweep
lists choice positives separately instead of scoring them. Label them anyway.
They are real drift, and `graph.choice_parallelism` is the check that owns them.

## Not being anchored

`ingest.py --predict` pre-fills the classifier's guess into `register_pred` /
`pronoun_pred`. It makes labeling roughly three times faster and it will bias
you toward agreeing, which would inflate every number in the report
invisibly — a corrupted gold set looks exactly like a good classifier.

Two rules:

1. **Never copy a `_pred` value into a `_gold` column.** Form your own
   judgement first, then look if you want a sanity check.
2. **Rows with `blind=1` have no prediction. Label those cold.** Do not go
   looking for what the classifier said about them.

`evaluate.py` then compares agreement on the blind and assisted slices and
prints the gap. A few points is sampling noise. **Ten or more means you were
anchored**, and the assisted rows need relabeling with the `_pred` columns
hidden or deleted.

## Who should label

Ideally not the person who wrote the rules in `locqa/checks/voice.py`. If that
is unavoidable, at minimum label from `--blind` rows only, and record in
`notes` that author and annotator were the same person, so the number can be
discounted later by someone who did not.

## Target

~350 lines, **~30 per speaker minimum**. Per-speaker volume matters more than
the total, because every voice check reasons about one speaker at a time: a
set of 350 lines spread over 40 characters evaluates nothing.

Aim for 10–15% drift positives. A set with two positives cannot distinguish a
good threshold from a lucky one, which is the state the sweep is in today.
