"""Canonical representation of a localizable game script.

Every parser lowers its native format into this model, and every check reads
only this model. That separation is the point: a check written for XLIFF works
unchanged on a Ren'Py script, and adding a format costs one parser rather than
N check rewrites.
"""

from __future__ import annotations

import unicodedata
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterable, Iterator


class Severity(IntEnum):
    """Ordered so that `max()` and sorting do the obvious thing."""

    INFO = 0
    MINOR = 1
    MAJOR = 2
    BLOCKER = 3

    @property
    def label(self) -> str:
        return self.name.title()


@dataclass
class Segment:
    """One translatable unit: a source string and its target.

    `node_id` ties the segment to a position in the dialogue graph. Flat
    formats (a CSV of UI strings) leave it None, and the graph-tier checks
    simply find nothing to do.
    """

    id: str
    source: str
    target: str = ""
    node_id: str | None = None
    speaker: str | None = None
    max_width: int | None = None          # UI budget, half-width units per line
    max_lines: int | None = None
    is_choice: bool = False               # rendered as a player-selectable option
    do_not_translate: bool = False
    notes: str = ""
    location: str = ""                    # file:line, for jumping back to source
    meta: dict = field(default_factory=dict)

    @property
    def is_translated(self) -> bool:
        return bool(self.target.strip())


@dataclass
class Node:
    """A node in the dialogue graph.

    `successors` are node ids. A `menu` node's successors are the branches its
    choices lead to; the choices themselves are Segments with `is_choice=True`.
    """

    id: str
    kind: str = "line"                    # line | menu | choice | label | end
    successors: list[str] = field(default_factory=list)
    segment_ids: list[str] = field(default_factory=list)
    label: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class Script:
    name: str
    source_lang: str = "en"
    target_lang: str = "ja"
    segments: dict[str, Segment] = field(default_factory=dict)
    nodes: dict[str, Node] = field(default_factory=dict)
    entry_nodes: list[str] = field(default_factory=list)
    source_format: str = ""

    # -- construction ----------------------------------------------------

    def add_segment(self, seg: Segment) -> Segment:
        if seg.id in self.segments:
            raise ValueError(f"duplicate segment id: {seg.id}")
        self.segments[seg.id] = seg
        if seg.node_id:
            node = self.nodes.setdefault(seg.node_id, Node(id=seg.node_id))
            node.segment_ids.append(seg.id)
        return seg

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing:
            existing.kind = node.kind or existing.kind
            existing.successors.extend(
                s for s in node.successors if s not in existing.successors
            )
            existing.label = node.label or existing.label
            existing.meta.update(node.meta)
            return existing
        self.nodes[node.id] = node
        return node

    # -- access ----------------------------------------------------------

    def __iter__(self) -> Iterator[Segment]:
        return iter(self.segments.values())

    def __len__(self) -> int:
        return len(self.segments)

    @property
    def has_graph(self) -> bool:
        return any(n.successors for n in self.nodes.values())

    def segments_of(self, node_id: str) -> list[Segment]:
        node = self.nodes.get(node_id)
        if not node:
            return []
        return [self.segments[s] for s in node.segment_ids if s in self.segments]

    def node_of(self, seg: Segment) -> Node | None:
        return self.nodes.get(seg.node_id) if seg.node_id else None

    def predecessors(self) -> dict[str, list[str]]:
        preds: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for node in self.nodes.values():
            for succ in node.successors:
                preds.setdefault(succ, []).append(node.id)
        return preds

    # -- reachability ----------------------------------------------------

    def depths(self) -> dict[str, int]:
        """BFS depth from the entry node(s). Unreachable nodes are absent.

        Depth is the cheap proxy for "how likely is a player to see this".
        A bug at depth 1 is on the mainline; a bug at depth 12 sits behind a
        pile of flag checks. Severity gets weighted accordingly, so a triage
        pass fixes what ships in front of players first.
        """
        roots = self.entry_nodes or self._infer_roots()
        depth: dict[str, int] = {}
        queue = deque((r, 0) for r in roots if r in self.nodes)
        for r, d in list(queue):
            depth[r] = d
        while queue:
            nid, d = queue.popleft()
            for succ in self.nodes[nid].successors:
                if succ in self.nodes and succ not in depth:
                    depth[succ] = d + 1
                    queue.append((succ, d + 1))
        return depth

    def _infer_roots(self) -> list[str]:
        preds = self.predecessors()
        roots = [nid for nid in self.nodes if not preds.get(nid)]
        return roots or (list(self.nodes)[:1] if self.nodes else [])

    def unreachable_nodes(self) -> list[str]:
        if not self.has_graph:
            return []
        seen = self.depths()
        return [nid for nid in self.nodes if nid not in seen]


@dataclass
class Finding:
    """One QA hit. `reach` scales severity for triage, never for correctness."""

    check_id: str
    severity: Severity
    message: str
    segment_id: str | None = None
    node_id: str | None = None
    excerpt: str = ""
    suggestion: str = ""
    span: tuple[int, int] | None = None
    reach: float = 1.0
    meta: dict = field(default_factory=dict)

    @property
    def priority(self) -> float:
        return float(self.severity) * self.reach

    def as_dict(self) -> dict:
        return {
            "check": self.check_id,
            "severity": self.severity.label,
            "message": self.message,
            "segment": self.segment_id,
            "node": self.node_id,
            "excerpt": self.excerpt,
            "suggestion": self.suggestion,
            "span": list(self.span) if self.span else None,
            "reach": round(self.reach, 3),
            "priority": round(self.priority, 3),
        }


# -- width -------------------------------------------------------------------

_WIDE = frozenset("WF")


def display_width(text: str, ambiguous_wide: bool = True) -> int:
    """Width in half-width units.

    Japanese UI budgets are quoted in 全角 (full-width) counts, but mixed
    EN/JA strings are the norm in game text, so half-width units are the only
    honest common denominator. `ambiguous_wide` reflects that East Asian
    Ambiguous characters (§, ±, Greek, box drawing) render double-width in a
    Japanese font, which is what the game will actually use.
    """
    total = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        eaw = unicodedata.east_asian_width(ch)
        if eaw in _WIDE or (ambiguous_wide and eaw == "A"):
            total += 2
        else:
            total += 1
    return total


def normalize_breaks(text: str) -> str:
    r"""Turn the two-character escape `\n` into a real newline.

    Loc sheets and script formats store a line break as a backslash followed
    by an `n`. Left alone those are two rendered characters, so a string gets
    charged two units it will not occupy *and* loses the break the engine will
    actually make — measured as neither the text nor the layout that ships.
    Everything that counts or wraps goes through here first.
    """
    return text.replace("\\n", "\n")


def wrap_by_width(text: str, max_width: int, ambiguous_wide: bool = True) -> list[str]:
    """Greedy character wrap, the way a game textbox actually breaks lines.

    No word-boundary logic: Japanese engines wrap per character, which is
    exactly why kinsoku shori exists as a separate correction pass.
    """
    text = normalize_breaks(text)
    if max_width <= 0:
        return [text]
    lines: list[str] = []
    current = ""
    width = 0
    for ch in text:
        if ch == "\n":
            lines.append(current)
            current, width = "", 0
            continue
        w = display_width(ch, ambiguous_wide)
        if width + w > max_width and current:
            lines.append(current)
            current, width = ch, w
        else:
            current += ch
            width += w
    lines.append(current)
    return lines


def iter_lines(text: str) -> Iterable[str]:
    return normalize_breaks(text).split("\n")
