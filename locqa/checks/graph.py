"""Graph-tier checks.

These exist only because the script is a directed graph rather than a list of
strings. Each one catches a bug class that is invisible to a translator working
through a spreadsheet in row order, because the evidence is spread across rows
that are far apart in the file and adjacent in play.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from ..model import Finding, Node, Script, Segment, Severity
from .base import Check, Config, register
from .japanese import _strip_markup
from .voice import classify_register, sentences

# Openers that presuppose a specific preceding line. If a node carrying one of
# these has several predecessors, the writer verified at most one of them.
ANAPHORIC_OPENERS = [
    "はい", "いいえ", "ええ", "うん", "ううん", "そう", "それ", "これ", "あれ",
    "だから", "でも", "しかし", "じゃあ", "では", "つまり", "その通り", "確かに", "もちろん",
]

# Ending shapes for choice-parallelism. Player-facing options in a menu should
# be grammatically uniform; mixed shapes read as a UI defect in Japanese.
ENDING_SHAPES = [
    ("ますform", re.compile(r"(ます|ません|ましょう|ください)[。！？…]*$")),
    ("てform", re.compile(r"[てで][。！？…]*$")),
    # ない / なかった are plain verb forms, not nouns — they must be tested
    # before the noun-final catch-all or every negative reads as 体言止め.
    ("plain verb", re.compile(r"(?:ない|なかった|[るくすつぬぶむぐう])[。！？…]*$")),
    ("question", re.compile(r"[？?][」』）]*$")),
    ("noun-final", re.compile(r"[\u4E00-\u9FFF\u30A0-\u30FF\u3040-\u309F][」』）]*$")),
]


@register
class DanglingEdge(Check):
    id = "graph.dangling"
    tier = 2
    title = "Dangling branch target"
    description = "A branch points at a node that does not exist."
    default_severity = Severity.BLOCKER
    requires_graph = True

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for node in script.nodes.values():
            for succ in node.successors:
                if succ not in script.nodes:
                    yield Finding(
                        check_id=self.id,
                        severity=config.severity_for(self.id, self.default_severity),
                        message=f"Node “{node.id}” branches to “{succ}”, which is not defined.",
                        node_id=node.id,
                        suggestion="A label was renamed on one side of the branch only.",
                    )


@register
class UnreachableNode(Check):
    id = "graph.unreachable"
    tier = 2
    title = "Unreachable content"
    description = "Nodes with no path from an entry point — translated text a player can never see."
    default_severity = Severity.MINOR
    requires_graph = True

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for nid in script.unreachable_nodes():
            segs = script.segments_of(nid)
            if not segs:
                continue
            yield Finding(
                check_id=self.id,
                severity=config.severity_for(self.id, self.default_severity),
                message=f"Node “{nid}” holds {len(segs)} translated segment(s) but has no path "
                "from any entry point.",
                node_id=nid,
                excerpt=segs[0].target or segs[0].source,
                suggestion="Either the branch was cut and the strings should be removed from the "
                "wordcount, or an edge is missing.",
            )


@register
class ChoiceParallelism(Check):
    id = "graph.choice_parallelism"
    tier = 2
    title = "Choice parallelism"
    description = "Sibling menu options that do not share a grammatical shape or register."
    default_severity = Severity.MINOR
    requires_graph = True

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for node in script.nodes.values():
            choices = [s for s in script.segments_of(node.id) if s.is_choice and s.is_translated]
            if len(choices) < 2:
                continue

            # Register first: a polite option among plain ones is also, almost
            # always, a ます-shaped option among plain verbs. That is one bug,
            # and the register finding is the one that names it, so an option
            # it covers gets no shape finding.
            reported: set[str] = set()
            registers = {s.id: classify_register(s.target) for s in choices}
            informative = {k: v for k, v in registers.items() if v in ("POLITE", "PLAIN")}
            if len(set(informative.values())) > 1:
                tally: dict[str, int] = defaultdict(int)
                for v in informative.values():
                    tally[v] += 1
                dominant = max(tally, key=tally.get)
                # Report the odd one out, not both sides of the mismatch.
                for sid, reg in informative.items():
                    if reg == dominant:
                        continue
                    reported.add(sid)
                    yield self.finding(
                        config,
                        f"Option is in {_short(reg)} while its siblings are in {_short(dominant)}. "
                        "The player character's voice should not change between options in one menu.",
                        segment=script.segments[sid],
                        excerpt=script.segments[sid].target,
                        severity=Severity.MAJOR,
                        meta={"register": reg, "dominant": dominant},
                    )

            shapes = {s.id: _ending_shape(s.target) for s in choices}
            distinct = {v for v in shapes.values() if v}
            if len(distinct) > 1:
                counts: dict[str, int] = defaultdict(int)
                for v in shapes.values():
                    if v:
                        counts[v] += 1
                majority = max(counts, key=counts.get)
                for seg in choices:
                    if seg.id in reported:
                        continue
                    if shapes[seg.id] and shapes[seg.id] != majority:
                        yield self.finding(
                            config,
                            f"Option ends in {shapes[seg.id]} while its siblings end in {majority}. "
                            "Menu options should be grammatically parallel.",
                            segment=seg,
                            excerpt=seg.target,
                            meta={"shape": shapes[seg.id], "majority": majority},
                        )


@register
class BranchAnaphora(Check):
    id = "graph.anaphora"
    tier = 2
    title = "Anaphora across a merge point"
    description = "A line opening with そう/はい/でも at a node reachable from several parents."
    default_severity = Severity.MAJOR
    requires_graph = True

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        preds = script.predecessors()
        for node in script.nodes.values():
            parents = preds.get(node.id, [])
            if len(parents) < 2:
                continue
            # Only the node's first line follows a parent. Every later line
            # follows the line before it in this same node, which is one
            # writer's sequential text and not a merge at all.
            seg = next((s for s in script.segments_of(node.id) if not s.is_choice), None)
            if seg is not None and seg.is_translated:
                opener = _anaphoric_opener(seg.target)
                if not opener:
                    continue
                parent_lines = []
                for pid in parents[:3]:
                    last = _last_line(script, pid)
                    if last is not None:
                        parent_lines.append(f"{pid}: {(last.target or last.source)[:28]}")
                    else:
                        parent_lines.append(pid)
                yield self.finding(
                    config,
                    f"Opens with 「{opener}」, which refers back to the previous line — but this "
                    f"node is reached from {len(parents)} different places.",
                    segment=seg,
                    excerpt=seg.target,
                    suggestion="Verify the line reads correctly after each parent: "
                    + "; ".join(parent_lines),
                    meta={"opener": opener, "parents": parents},
                )


@register
class MergeRegisterConflict(Check):
    id = "graph.merge_register"
    tier = 2
    title = "Register conflict at a merge"
    description = "Paths in different politeness registers converge on one node."
    default_severity = Severity.MINOR
    requires_graph = True

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        preds = script.predecessors()
        for node in script.nodes.values():
            parents = preds.get(node.id, [])
            if len(parents) < 2:
                continue
            registers: dict[str, str] = {}
            for pid in parents:
                # The register a player carries into the merge is that of the
                # last line they heard on the way in, so read each parent
                # backwards. Reading forwards took a parent's opening line,
                # which can be a whole scene away from the merge. Choices are
                # skipped for the reason given in _last_line.
                for ps in reversed(script.segments_of(pid)):
                    if ps.is_choice:
                        continue
                    reg = classify_register(ps.target)
                    if reg in ("POLITE", "PLAIN"):
                        registers[pid] = reg
                        break
            if len(set(registers.values())) > 1:
                segs = script.segments_of(node.id)
                if not segs:
                    continue
                summary = ", ".join(f"{p} ({_short(r)})" for p, r in registers.items())
                yield self.finding(
                    config,
                    f"Incoming paths use different registers — {summary} — and converge here.",
                    segment=segs[0],
                    excerpt=segs[0].target,
                    suggestion="Japanese carries the register forward; English does not, so this "
                    "reads as continuous in the source and jarring in the target.",
                    meta={"parents": registers},
                )


@register
class SoloChoice(Check):
    id = "graph.solo_choice"
    tier = 2
    title = "Single-option menu"
    description = "A menu node with one option, usually a branch lost in conversion."
    default_severity = Severity.INFO
    requires_graph = True

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for node in script.nodes.values():
            if node.kind != "menu":
                continue
            choices = [s for s in script.segments_of(node.id) if s.is_choice]
            if len(choices) == 1:
                yield self.finding(
                    config,
                    "Menu presents a single option.",
                    segment=choices[0],
                    excerpt=choices[0].target or choices[0].source,
                )


def _ending_shape(text: str) -> str | None:
    text = _strip_markup(text).strip()
    if not text:
        return None
    for name, pattern in ENDING_SHAPES:
        if pattern.search(text):
            return name
    return None


def _last_line(script: Script, node_id: str) -> Segment | None:
    """The last spoken line in a node — what a player heard just before leaving it.

    Choices are skipped: a menu node's segments are every option, and the last
    one listed is not the one the player picked.
    """
    for seg in reversed(script.segments_of(node_id)):
        if not seg.is_choice and (seg.target or seg.source):
            return seg
    return None


def _anaphoric_opener(text: str) -> str | None:
    stripped = _strip_markup(text).lstrip("「『（ 　")
    for opener in ANAPHORIC_OPENERS:
        if stripped.startswith(opener):
            return opener
    return None


def _short(register: str) -> str:
    return {"POLITE": "です・ます", "PLAIN": "だ・である"}.get(register, register)
