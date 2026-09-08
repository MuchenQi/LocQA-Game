#!/usr/bin/env python3
"""Classifier-level evaluation.

Answers one question: given a line in isolation, does `classify_register` label
it the way a human annotator does? The check-level question — does
`voice.register` fire on the right lines — is separate and lives in sweep.py.

Usage:
    python eval/evaluate.py [--labels eval/labels.csv] [--errors]
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from locqa.checks.voice import _first_person, classify_register  # noqa: E402

LABELS = ["POLITE", "PLAIN", "NEUTRAL", "MIXED"]


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("target_ja") or "").strip()]
    if not rows:
        raise SystemExit(f"No labeled rows in {path}")
    return rows


# -- metrics -----------------------------------------------------------------


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def confusion(pairs: list[tuple[str, str]], labels: list[str]) -> None:
    """Rows are gold, columns are predicted."""
    matrix: dict[str, Counter] = {g: Counter() for g in labels}
    for gold, pred in pairs:
        matrix.setdefault(gold, Counter())[pred] += 1

    width = max(len(l) for l in labels) + 2
    header = " " * (width + 6) + "".join(f"{l:>{width}}" for l in labels)
    print(header)
    print(" " * (width + 6) + "-" * (width * len(labels)))
    for gold in labels:
        row = "".join(f"{matrix[gold][p]:>{width}}" for p in labels)
        total = sum(matrix[gold].values())
        print(f"  gold {gold:<{width}}{row}   ({total})")
    print()


def report(pairs: list[tuple[str, str]], labels: list[str], title: str) -> float:
    print(f"\n{title}")
    print("=" * len(title))
    n = len(pairs)
    correct = sum(1 for g, p in pairs if g == p)
    print(f"\n  n = {n}    accuracy = {correct / n:.3f}\n")

    confusion(pairs, labels)

    print(f"  {'label':<10}{'prec':>8}{'rec':>8}{'F1':>8}{'support':>10}")
    print("  " + "-" * 44)
    macro = []
    for label in labels:
        tp = sum(1 for g, p in pairs if g == label and p == label)
        fp = sum(1 for g, p in pairs if g != label and p == label)
        fn = sum(1 for g, p in pairs if g == label and p != label)
        support = sum(1 for g, _ in pairs if g == label)
        if not support and not fp:
            continue
        p, r, f = prf(tp, fp, fn)
        macro.append(f)
        print(f"  {label:<10}{p:>8.3f}{r:>8.3f}{f:>8.3f}{support:>10}")
    macro_f1 = sum(macro) / len(macro) if macro else 0.0
    print("  " + "-" * 44)
    print(f"  {'macro F1':<10}{macro_f1:>24.3f}\n")
    return macro_f1


# -- evaluations -------------------------------------------------------------


def eval_register(rows: list[dict], show_errors: bool) -> float:
    pairs = []
    errors = []
    for r in rows:
        gold = (r.get("register_gold") or "").strip().upper()
        if gold not in LABELS:
            continue
        pred = classify_register(r["target_ja"])
        pairs.append((gold, pred))
        if gold != pred:
            errors.append((r, gold, pred))

    if not pairs:
        print("No register labels found.")
        return 0.0

    macro = report(pairs, LABELS, "REGISTER CLASSIFIER — classify_register()")

    if errors:
        # The error list is the point of this harness, not the score. Each row
        # is a case where surface form and human judgment disagree, which is
        # where the rules need to change and where the interesting linguistics
        # lives.
        print(f"  {len(errors)} misclassified:\n")
        for r, gold, pred in errors if show_errors else errors[:12]:
            print(f"    [{r['line_id']}] gold {gold:<8} pred {pred:<8} {r['target_ja']}")
            if r.get("notes"):
                print(f"              {r['notes']}")
        if not show_errors and len(errors) > 12:
            print(f"    … {len(errors) - 12} more (--errors to see all)")
        print()

    directional = Counter()
    for _, gold, pred in errors:
        directional[f"{gold}→{pred}"] += 1
    if directional:
        print("  error directions:")
        for k, v in directional.most_common():
            print(f"    {v:>3}  {k}")
        print()
    return macro


def eval_pronoun(rows: list[dict], show_errors: bool) -> float:
    """Detection of the speaker's own 一人称.

    Quoted pronouns are the known failure: 「『俺が行く』と仰っていました」 is a
    polite speaker quoting a blunt one, and a substring match attributes 俺 to
    the wrong character.
    """
    pairs = []
    errors = []
    for r in rows:
        gold = (r.get("pronoun_gold") or "").strip()
        pred = _first_person(r["target_ja"]) or ""
        pairs.append((gold or "NONE", pred or "NONE"))
        if gold != pred:
            errors.append((r, gold or "(none)", pred or "(none)"))

    labels = sorted({g for g, _ in pairs} | {p for _, p in pairs})
    macro = report(pairs, labels, "FIRST-PERSON DETECTOR — _first_person()")

    if errors:
        print(f"  {len(errors)} incorrect:\n")
        for r, gold, pred in errors:
            print(f"    [{r['line_id']}] gold {gold:<6} pred {pred:<6} {r['target_ja']}")
            if r.get("notes"):
                print(f"              {r['notes']}")
        print()
    return macro


def coverage(rows: list[dict]) -> None:
    print("\nDATASET")
    print("=======\n")
    by_speaker = defaultdict(int)
    drift = 0
    labeled_drift = 0
    for r in rows:
        by_speaker[r.get("speaker") or "(none)"] += 1
        d = (r.get("is_drift") or "").strip()
        if d:
            labeled_drift += 1
            drift += int(d == "1")

    print(f"  {len(rows)} lines, {len(by_speaker)} speakers")
    print(f"  {labeled_drift} lines labeled for drift, of which {drift} are drift\n")
    for speaker, n in sorted(by_speaker.items(), key=lambda kv: -kv[1]):
        flag = "" if n >= 30 else "   ← thin; 30+ needed for the drift sweep"
        print(f"    {n:>4}  {speaker}{flag}")

    reg = Counter((r.get("register_gold") or "").strip().upper() for r in rows)
    print("\n  register_gold distribution:")
    for label in LABELS:
        print(f"    {reg.get(label, 0):>4}  {label}")
    print()


def eval_anchoring(rows: list[dict]) -> None:
    """Compare classifier agreement on blind vs assisted rows.

    ingest.py --predict shows the annotator the classifier's guess on most rows
    and hides it on a blind slice. If the assisted rows agree with the
    classifier substantially more than the blind rows do, the annotator was
    anchored and the assisted labels are not independent evidence.

    The gap is the measurement. A few points is sampling noise; ten or more
    means the assisted labels need a second pass with predictions hidden.
    """
    if not any("blind" in r for r in rows):
        return

    slices: dict[str, list[tuple[str, str]]] = {"blind": [], "assisted": []}
    for r in rows:
        gold = (r.get("register_gold") or "").strip().upper()
        if gold not in LABELS:
            continue
        flag = (r.get("blind") or "").strip()
        if flag not in ("0", "1"):
            continue
        bucket = "blind" if flag == "1" else "assisted"
        slices[bucket].append((gold, classify_register(r["target_ja"])))

    if not slices["blind"] or not slices["assisted"]:
        return

    print("\nANCHORING CHECK")
    print("===============\n")
    rates = {}
    for name, pairs in slices.items():
        agree = sum(1 for g, p in pairs if g == p) / len(pairs)
        rates[name] = agree
        print(f"  {name:<10} n = {len(pairs):>4}   agreement with classifier = {agree:.3f}")

    gap = rates["assisted"] - rates["blind"]
    print(f"\n  gap = {gap:+.3f}")
    if gap >= 0.10:
        print("\n  The assisted rows agree with the classifier far more than the blind")
        print("  rows do. Treat the assisted labels as anchored: relabel them with the")
        print("  _pred columns hidden before reporting any number from this set.")
    elif gap >= 0.05:
        print("\n  Mild gap. Watch it as the set grows; if it widens, relabel.")
    else:
        print("\n  No meaningful anchoring detected.")
    print()


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labels", default=os.path.join(here, "labels.csv"))
    ap.add_argument("--errors", action="store_true", help="Show every error, not the first 12.")
    args = ap.parse_args()

    rows = load(args.labels)
    coverage(rows)
    reg_f1 = eval_register(rows, args.errors)
    pro_f1 = eval_pronoun(rows, args.errors)
    eval_anchoring(rows)

    print("SUMMARY")
    print("=======\n")
    print(f"  register macro F1     {reg_f1:.3f}")
    print(f"  first-person macro F1 {pro_f1:.3f}\n")

    if len(rows) < 300:
        print(f"  NOTE: {len(rows)} lines. These numbers are indicative, not")
        print("  publishable. See eval/GUIDELINES.md for the target set.\n")


if __name__ == "__main__":
    main()
