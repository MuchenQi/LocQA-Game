# Corpus sources

## What is committed, and what is not

**Committed:** `labels.csv` — line ids, speaker, and the human labels, plus the
source and target text of each labeled line.

**Not committed:** whole scripts. `.gitignore` excludes `corpus/` and
`eval/raw/`. Put fetched material there.

The distinction matters because a commercial game script is someone else's
copyrighted text, and a QA tool's test fixture is not a licence to redistribute
it. A few dozen short lines quoted for the purpose of evaluating a linter is a
defensible excerpt; the whole chapter is republication.

If a source's terms do not permit even the excerpt, **do not add it to
`labels.csv`.** Label it locally, keep the rows out of the commit, and record
the exclusion below so the committed numbers can be understood as a subset.

## Adding a source

1. Fetch into `corpus/`, which is ignored.
2. `python eval/ingest.py corpus/<file> --sample N --note "<source, licence>"`
   — the `--note` lands in the `notes` column of every row it writes, so
   provenance travels with the label rather than living only here.
3. Label per `GUIDELINES.md`.
4. Add a row to the table below.
5. Commit `labels.csv`. Never `corpus/`.

## Current sources

| source | licence | lines | notes |
|---|---|---|---|
| self-authored seed | project licence (MIT) | 35 | written for this repo; see caveat |

**Caveat on the seed set.** All 35 lines were written by the same person who
wrote the rules in `locqa/checks/voice.py`. They are legally unencumbered and
methodologically the weakest evidence in the project — the author's blind
spots are reproduced in both the rules and the data. They are committed
because they are better than nothing and because they did catch a real bug
(`FINDINGS.md` 001), not because they are a valid sample.

Replacing them with real translated text is the single highest-value piece of
work outstanding in this repository.

## Where real material can come from

Roughly in order of how easily the terms can be established:

- **Your own shipped titles.** If you have EN→JA loc for a game you own or
  worked on, that is the ideal corpus: real translator output, real drift, and
  you already hold the rights.
- **Open-source and freeware visual novels** with permissive licences. Ren'Py
  games on itch.io frequently ship their `.rpy` files, and a good number carry
  a licence that permits excerpting. Check each one; "source available" is not
  a licence.
- **Open localization corpora** — Mozilla, KDE, GNOME, LibreOffice. Real
  EN→JA translation under clear terms. The register distribution is wrong for
  this purpose (UI strings are overwhelmingly 体言止め and です・ます, with
  almost no 常体 dialogue and no character voice at all), so it is useful for
  the tier-1 orthography checks and close to useless for the voice checks.
- **Fan translation patches.** Usually unlicensed derivative works. Avoid.

## What not to do

**Do not generate the lines.** Synthetic Japanese produced by the same author
— or the same model — that wrote the rules will match those rules, because
both come from one set of intuitions about what Japanese dialogue looks like.
The accuracy figure goes up, the information content goes to zero, and the
result is indistinguishable from a real improvement in the report.

The eval set is only worth maintaining if it retains the capacity to surprise
whoever wrote the checker. Every shortcut that raises the number by removing
that capacity makes the directory actively misleading.
