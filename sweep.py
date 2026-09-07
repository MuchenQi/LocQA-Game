#!/usr/bin/env python3
"""Check-level evaluation: threshold sweep for voice.register.

evaluate.py asks whether a line is classified correctly. This asks the
downstream question — given that classifier, does the check fire on the lines a
human marked as drift, and stay quiet on the ones they didn't?

The two thresholds under test:

  MIN_LINES  how many register-bearing lines a speaker needs before the check
             will judge them at all
  MIN_RATIO  how dominant the majority register must be before the minority
             counts as an error rather than as a genuinely mixed speaker

Both are currently set by intuition. This script is what replaces that.

Usage:
    python eval/sweep.py [--labels eval/labels.csv] [--plot out.png]
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from locqa import Config, Script, Segment  # noqa: E402
from locqa.checks.voice import RegisterConsistency  # noqa: E402

CURRENT = (RegisterConsistency.MIN_LINES, RegisterConsistency.MIN_RATIO)


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [r for r in csv.DictReader(fh) if (r.get("target_ja") or "").strip()]


def build_script(rows: list[dict]) -> tuple[Script, set[str]]:
    """One segment per labeled line; gold positives are register-type drift."""
    script = Script(name="eval")
    gold: set[str] = set()
    for r in rows:
        speaker = (r.get("speaker") or "").strip()
        if not speaker:
            continue
        script.add_segment(
            Segment(
                id=r["line_id"],
                source=r.get("source_en", ""),
                target=r["target_ja"],
                speaker=speaker,
            )
        )
        if (r.get("is_drift") or "").strip() == "1" and (r.get("drift_type") or "").strip() == "register":
            gold.add(r["line_id"])
    return script, gold


def evaluate_at(script: Script, gold: set[str], min_lines: int, min_ratio: float) -> dict:
    check = RegisterConsistency()
    check.MIN_LINES = min_lines
    check.MIN_RATIO = min_ratio
    # Only the majority-vote finding is under test here; the within-line MIXED
    # finding uses no threshold and would distort the counts.
    fired = {
        f.segment_id
        for f in check.run(script, Config())
        if f.meta.get("expected")
    }

    tp = len(fired & gold)
    fp = len(fired - gold)
    fn = len(gold - fired)
    precision = tp / (tp + fp) if tp + fp else (1.0 if not fn else 0.0)
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "min_lines": min_lines, "min_ratio": min_ratio,
        "tp": tp, "fp": fp, "fn": fn,
        "precision": precision, "recall": recall, "f1": f1,
        "fired": fired,
    }


def sweep(script: Script, gold: set[str], line_range, ratio_range) -> list[dict]:
    return [
        evaluate_at(script, gold, ml, mr)
        for ml in line_range
        for mr in ratio_range
    ]


def print_table(results: list[dict], line_range, ratio_range) -> None:
    print("\nF1 BY THRESHOLD")
    print("===============\n")
    print("            " + "".join(f"{r:>8.2f}" for r in ratio_range) + "   ← MIN_RATIO")
    print("            " + "-" * (8 * len(ratio_range)))
    lookup = {(r["min_lines"], r["min_ratio"]): r for r in results}
    for ml in line_range:
        cells = []
        for mr in ratio_range:
            res = lookup[(ml, mr)]
            mark = "*" if (ml, round(mr, 2)) == (CURRENT[0], round(CURRENT[1], 2)) else " "
            cells.append(f"{res['f1']:>7.3f}{mark}")
        print(f"  lines {ml:>2}  " + "".join(cells))
    print("\n  * = current setting in locqa/checks/voice.py\n")


def print_detail(results: list[dict]) -> None:
    print("\nOPERATING POINTS")
    print("================\n")
    print(f"  {'lines':>6}{'ratio':>8}{'TP':>5}{'FP':>5}{'FN':>5}{'prec':>9}{'rec':>8}{'F1':>8}")
    print("  " + "-" * 52)
    seen = set()
    for r in sorted(results, key=lambda x: (-x["f1"], x["min_lines"], x["min_ratio"])):
        key = (r["tp"], r["fp"], r["fn"])
        if key in seen and len(seen) > 6:
            continue
        seen.add(key)
        star = " *" if (r["min_lines"], round(r["min_ratio"], 2)) == (CURRENT[0], round(CURRENT[1], 2)) else ""
        print(f"  {r['min_lines']:>6}{r['min_ratio']:>8.2f}{r['tp']:>5}{r['fp']:>5}{r['fn']:>5}"
              f"{r['precision']:>9.3f}{r['recall']:>8.3f}{r['f1']:>8.3f}{star}")
    print()


def print_errors(script: Script, results: list[dict], gold: set[str]) -> None:
    current = next(
        (r for r in results
         if (r["min_lines"], round(r["min_ratio"], 2)) == (CURRENT[0], round(CURRENT[1], 2))),
        None,
    )
    if not current:
        return
    print("\nAT THE CURRENT SETTING")
    print("======================\n")
    print(f"  MIN_LINES = {CURRENT[0]}, MIN_RATIO = {CURRENT[1]}")
    print(f"  precision {current['precision']:.3f}  recall {current['recall']:.3f}  "
          f"F1 {current['f1']:.3f}\n")

    for label, ids in (("false positives", current["fired"] - gold),
                       ("missed", gold - current["fired"])):
        if not ids:
            continue
        print(f"  {label}:")
        for sid in sorted(ids):
            seg = script.segments[sid]
            print(f"    [{sid}] {seg.speaker}: {seg.target}")
        print()
    if not (current["fired"] - gold) and not (gold - current["fired"]):
        print("  No errors at this operating point on this set.\n")


def maybe_plot(results: list[dict], path: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print(f"  matplotlib not available; skipped {path}")
        return

    fig, ax = plt.subplots(figsize=(6, 5))
    by_lines = defaultdict(list)
    for r in results:
        by_lines[r["min_lines"]].append(r)
    for ml, group in sorted(by_lines.items()):
        group.sort(key=lambda x: x["recall"])
        ax.plot([g["recall"] for g in group], [g["precision"] for g in group],
                marker="o", markersize=3.5, linewidth=1.2, label=f"MIN_LINES={ml}")
    current = next(
        (r for r in results
         if (r["min_lines"], round(r["min_ratio"], 2)) == (CURRENT[0], round(CURRENT[1], 2))),
        None,
    )
    if current:
        ax.scatter([current["recall"]], [current["precision"]], s=110, zorder=5,
                   facecolors="none", edgecolors="#BE3A2B", linewidths=2,
                   label="current setting")
    ax.set_xlabel("recall")
    ax.set_ylabel("precision")
    ax.set_title("voice.register — threshold sweep")
    ax.set_xlim(-0.02, 1.05)
    ax.set_ylim(-0.02, 1.05)
    ax.grid(alpha=0.25, linewidth=0.6)
    ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    print(f"  wrote {path}")


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labels", default=os.path.join(here, "labels.csv"))
    ap.add_argument("--plot", nargs="?", const=os.path.join(here, "sweep.png"))
    args = ap.parse_args()

    rows = load(args.labels)
    script, gold = build_script(rows)

    by_speaker = defaultdict(int)
    for seg in script:
        by_speaker[seg.speaker] += 1

    print("\nSWEEP INPUT")
    print("===========\n")
    print(f"  {len(script)} lines, {len(by_speaker)} speakers, "
          f"{len(gold)} gold register-drift lines\n")
    for sp, n in sorted(by_speaker.items(), key=lambda kv: -kv[1]):
        print(f"    {n:>4}  {sp}")

    if not gold:
        print("\n  No lines labeled is_drift=1 with drift_type=register.")
        print("  The sweep needs positives to score against.\n")
        return

    line_range = [3, 4, 5, 6, 8]
    ratio_range = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]
    results = sweep(script, gold, line_range, ratio_range)

    print_table(results, line_range, ratio_range)
    print_detail(results)
    print_errors(script, results, gold)

    best = max(results, key=lambda r: (r["f1"], -r["min_ratio"]))
    print("BEST ON THIS SET")
    print("================\n")
    print(f"  MIN_LINES = {best['min_lines']}, MIN_RATIO = {best['min_ratio']:.2f}  "
          f"→ F1 {best['f1']:.3f}\n")

    if len(script) < 300:
        print(f"  NOTE: {len(script)} lines and {len(gold)} positives. A sweep this")
        print("  small selects noise — several settings will tie, and the winner")
        print("  is not evidence. Grow the set before changing any threshold.\n")

    if args.plot:
        maybe_plot(results, args.plot)


if __name__ == "__main__":
    main()
