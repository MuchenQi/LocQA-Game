#!/usr/bin/env python3
"""Turn any file locqa can parse into label-ready rows.

Extracts speaker, source, and target into the labels.csv schema with the gold
columns left blank. Optionally attaches the classifier's own guess — in a
clearly separate column that is never the gold column.

    python eval/ingest.py corpus/ch1.rpy -t corpus/ch1_ja.rpy --out rows.csv
    python eval/ingest.py corpus/ch1.xlf --predict --blind 0.3 --out rows.csv
    python eval/ingest.py corpus/ui.csv --sample 80 --seed 7 --append

ON PRE-FILLED PREDICTIONS

Showing an annotator the classifier's guess makes labeling roughly three times
faster and biases them toward agreeing with it. That bias would inflate every
number in the report, and it would inflate them invisibly — a corrupted gold set
looks exactly like a good classifier.

Two safeguards, both on by default when --predict is used:

  1. Predictions land in `register_pred` / `pronoun_pred`. The gold columns are
     always written empty. Nothing copies one to the other.
  2. --blind reserves a fraction of rows with no predictions at all, marked
     blind=1. Label those cold. `evaluate.py` then reports accuracy on the blind
     and assisted slices separately: if the assisted slice agrees with the
     classifier substantially more, you were anchored, and the assisted labels
     need a second pass.

Without --predict, nothing is pre-filled and neither safeguard is needed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import random
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from locqa import load  # noqa: E402
from locqa.checks.voice import _first_person, classify_register  # noqa: E402

FIELDS = [
    "line_id", "speaker", "source_en", "target_ja",
    "register_gold", "pronoun_gold", "is_drift", "drift_type", "notes",
]
PRED_FIELDS = ["register_pred", "pronoun_pred", "blind"]

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LABELS = os.path.join(HERE, "labels.csv")


def make_id(source_path: str, segment_id: str) -> str:
    """Stable across re-ingests, so re-running never duplicates a row."""
    stem = re.sub(r"[^a-z0-9]+", "", os.path.splitext(os.path.basename(source_path))[0].lower())[:12]
    clean = re.sub(r"[^A-Za-z0-9_.#:-]+", "_", segment_id)
    if len(clean) > 28:
        clean = clean[:20] + "_" + hashlib.sha1(segment_id.encode()).hexdigest()[:6]
    return f"{stem}__{clean}"


def existing_ids(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return {r["line_id"] for r in csv.DictReader(fh) if r.get("line_id")}


def existing_targets(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return {(r.get("target_ja") or "").strip() for r in csv.DictReader(fh)}


def collect(args) -> list[dict]:
    script = load(args.path, translations=args.translations, sheet=args.sheet)

    rows = []
    for seg in script:
        target = (seg.target or "").strip()
        if not target or seg.do_not_translate:
            continue
        if args.min_chars and len(target) < args.min_chars:
            continue
        rows.append({
            "line_id": make_id(args.path, seg.id),
            "speaker": seg.speaker or "",
            "source_en": seg.source,
            "target_ja": target,
            "register_gold": "",
            "pronoun_gold": "",
            "is_drift": "",
            "drift_type": "",
            "notes": "; ".join(filter(None, [
                args.note or os.path.basename(args.path),
                # voice.register skips choices and unattributed lines, so the
                # annotator needs to know which rows those are before spending
                # time on is_drift for them.
                "choice" if seg.is_choice else "",
                "narration" if not seg.speaker and not seg.is_choice else "",
            ])),
        })
    return rows


def dedupe(rows: list[dict], seen_ids: set[str], seen_targets: set[str]) -> list[dict]:
    out, local = [], set()
    for r in rows:
        key = r["target_ja"]
        if r["line_id"] in seen_ids or key in seen_targets or key in local:
            continue
        local.add(key)
        out.append(r)
    return out


def sample(rows: list[dict], n: int, seed: int, per_speaker: int | None) -> list[dict]:
    rng = random.Random(seed)

    if per_speaker:
        by_speaker = defaultdict(list)
        for r in rows:
            by_speaker[r["speaker"]].append(r)
        picked = []
        for speaker, group in by_speaker.items():
            rng.shuffle(group)
            picked.extend(group[:per_speaker])
        rows = picked

    if n and n < len(rows):
        rows = rng.sample(rows, n)
    return rows


def attach_predictions(rows: list[dict], blind_fraction: float, seed: int) -> None:
    rng = random.Random(seed + 1)
    order = list(range(len(rows)))
    rng.shuffle(order)
    blind = set(order[: int(round(len(rows) * blind_fraction))])

    for i, r in enumerate(rows):
        if i in blind:
            # No prediction at all. These rows are the control: they measure
            # whether the assisted rows were anchored.
            r["register_pred"] = ""
            r["pronoun_pred"] = ""
            r["blind"] = "1"
        else:
            r["register_pred"] = classify_register(r["target_ja"])
            r["pronoun_pred"] = _first_person(r["target_ja"]) or ""
            r["blind"] = "0"


def write(rows: list[dict], path: str, append: bool, fields: list[str]) -> None:
    mode = "a" if append and os.path.exists(path) else "w"
    with open(path, mode, encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n",
                                extrasaction="ignore")
        if mode == "w":
            writer.writeheader()
        for r in rows:
            writer.writerow(r)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("path", help="Any file locqa parses: .rpy, .xlf, .csv, .xlsx")
    ap.add_argument("-t", "--translation", dest="translations", action="append", default=[],
                    help="Ren'Py translation file. Repeatable.")
    ap.add_argument("--sheet", help="Worksheet name for .xlsx input.")
    ap.add_argument("--out", default=DEFAULT_LABELS,
                    help="Output CSV. Defaults to eval/labels.csv")
    ap.add_argument("--append", action="store_true",
                    help="Append to --out, skipping rows already present.")
    ap.add_argument("--sample", type=int, default=0, help="Random subset of this size.")
    ap.add_argument("--per-speaker", type=int, default=0,
                    help="Cap rows per speaker before sampling.")
    ap.add_argument("--seed", type=int, default=0, help="Sampling seed, for reproducibility.")
    ap.add_argument("--min-chars", type=int, default=2,
                    help="Skip targets shorter than this.")
    ap.add_argument("--note", help="Value for the notes column — put licensing here.")
    ap.add_argument("--predict", action="store_true",
                    help="Attach classifier guesses in register_pred/pronoun_pred.")
    ap.add_argument("--blind", type=float, default=0.30, metavar="FRAC",
                    help="With --predict, fraction of rows left unassisted. Default 0.30.")
    args = ap.parse_args()

    if not 0.0 <= args.blind <= 1.0:
        raise SystemExit("--blind must be between 0 and 1")
    if args.predict and args.blind < 0.15:
        print(f"  warning: --blind {args.blind:.2f} leaves few control rows; "
              "the anchoring check will be weak.\n")

    rows = collect(args)
    if not rows:
        raise SystemExit(f"No translated segments found in {args.path}")

    before = len(rows)
    if args.append:
        rows = dedupe(rows, existing_ids(args.out), existing_targets(args.out))
    rows = sample(rows, args.sample, args.seed, args.per_speaker or None)

    fields = list(FIELDS)
    if args.predict:
        attach_predictions(rows, args.blind, args.seed)
        fields += PRED_FIELDS

    write(rows, args.out, args.append, fields)

    by_speaker = defaultdict(int)
    for r in rows:
        by_speaker[r["speaker"] or "(narration)"] += 1

    print(f"\n  {len(rows)} rows → {args.out}"
          f"{' (appended)' if args.append else ''}")
    if before != len(rows):
        print(f"  {before - len(rows)} skipped as duplicate or not sampled")
    print()
    for speaker, n in sorted(by_speaker.items(), key=lambda kv: -kv[1]):
        print(f"    {n:>4}  {speaker}")

    if args.predict:
        n_blind = sum(1 for r in rows if r["blind"] == "1")
        print(f"\n  {n_blind} rows blind, {len(rows) - n_blind} assisted.")
        print("  Fill register_gold and pronoun_gold yourself — never copy from")
        print("  the _pred columns. Label the blind rows in a separate sitting.")
    print("\n  Read eval/GUIDELINES.md before labeling.\n")


if __name__ == "__main__":
    main()
