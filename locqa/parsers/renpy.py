"""Ren'Py scripts, and the translation files that pair with them.

This is the parser that earns the graph tier. A `.rpy` script is a program,
not a list of strings: labels are nodes, `jump` and `menu` are edges, and the
order a translator reads the file in has nothing to do with the order a player
hears it in.

What is recovered is control flow, not semantics. Conditional bodies
contribute their edges but their guards are not evaluated, so reachability is
an upper bound — a node counted reachable might sit behind a flag that is
never set. That direction is the safe one: the check that uses it reports
unreachable content, and an upper bound on reachability means it under-reports
rather than accusing live content of being dead.

Translations are joined on the English text in Ren'Py's own `# original`
comment rather than on its generated label hash, because the hash changes when
the source line is edited and the comment does not.
"""

from __future__ import annotations

import os
import re

from ..model import Node, Script, Segment

# Statement keywords that can never be a say-statement's character tag.
KEYWORDS = frozenset("""
    label menu jump call return scene show hide play stop queue voice
    with pause window python init define default image transform screen
    style layeredimage if elif else while for pass nvl camera showif
    translate old new strings text vbox hbox add use
""".split())

_LABEL = re.compile(r"^label\s+(?P<name>[\w.]+)\s*(?:\(.*\))?\s*:\s*$")
_MENU = re.compile(r"^menu\s*(?:[\w.]+\s*)?:\s*$")
_JUMP = re.compile(r"^jump\s+(?P<label>[\w.]+)\s*$")
_CALL = re.compile(r"^call\s+(?P<label>[\w.]+)")
_RETURN = re.compile(r"^return\b")
_IF = re.compile(r"^(?P<kind>if|elif|else)\b.*:\s*$")
_CHARACTER = re.compile(
    r"^(?:define|default)\s+(?P<tag>\w+)\s*=\s*Character\s*\((?P<args>.*)\)\s*$"
)
_TRANSLATE_BLOCK = re.compile(r"^translate\s+\w+\s+(?P<label>[\w.]+)\s*:\s*$")
_HASH_SUFFIX = re.compile(r"_[0-9a-f]{4,}$")

# A say statement, with an optional character tag and optional trailing
# modifiers (`with vpunch`, `nointeract`). The trailing colon is what
# distinguishes a menu choice from a line of narration.
_SAY = re.compile(
    r"""^(?:(?P<who>\w+)\s+)?
         "(?P<text>(?:[^"\\]|\\.)*)"
         \s*(?P<rest>.*?)\s*(?P<colon>:)?$""",
    re.VERBOSE,
)


class Stmt:
    """One statement and the block indented beneath it."""

    __slots__ = ("text", "line", "children")

    def __init__(self, text: str, line: int):
        self.text = text
        self.line = line
        self.children: list[Stmt] = []

    def __repr__(self) -> str:                  # pragma: no cover - debugging aid
        return f"<Stmt {self.line}: {self.text[:40]!r}>"


def _strip_comment(line: str) -> str:
    """Remove a trailing `#` comment, respecting quotes.

    `Character("K", color="#fff")` must survive intact, so this cannot be a
    `split("#")`.
    """
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and i + 1 < len(line):
                out.append(line[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _unescape(text: str) -> str:
    r"""Undo Ren'Py string escaping, leaving `\n` as the literal the sheet holds."""
    return text.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")


def _blocks(path: str) -> list[Stmt]:
    """Parse the file into a tree of statements by indentation."""
    root: list[Stmt] = []
    stack: list[tuple[int, list[Stmt]]] = [(-1, root)]

    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            text = _strip_comment(raw.rstrip("\n"))
            if not text.strip():
                continue
            indent = len(text) - len(text.lstrip())
            while indent <= stack[-1][0]:
                stack.pop()
            stmt = Stmt(text.strip(), lineno)
            stack[-1][1].append(stmt)
            stack.append((indent, stmt.children))
    return root


def _first_argument(args: str) -> str | None:
    """The character's display name from a `Character(...)` argument list."""
    args = args.strip()
    if args.startswith("_("):
        args = args[2:]
    match = re.match(r"""^\s*(?P<q>["'])(?P<name>(?:[^"'\\]|\\.)*)(?P=q)""", args)
    if match:
        return _unescape(match.group("name"))
    return None                                 # Character(None) — narration


def _characters(blocks: list[Stmt]) -> dict[str, str]:
    """Map say-statement tags to display names, e.g. `k` → `Kestrel`."""
    found: dict[str, str] = {}

    def visit(stmts: list[Stmt]) -> None:
        for stmt in stmts:
            match = _CHARACTER.match(stmt.text)
            if match:
                name = _first_argument(match.group("args"))
                if name:
                    found[match.group("tag")] = name
            visit(stmt.children)

    visit(blocks)
    return found


def _say(text: str) -> tuple[str | None, str, bool] | None:
    """Parse a say statement or menu choice: (tag, text, is_choice)."""
    match = _SAY.match(text)
    if not match:
        return None
    who = match.group("who")
    if who and who in KEYWORDS:
        return None
    rest = match.group("rest") or ""
    # `"text" if flag:` is a conditional menu choice; the guard is not
    # evaluated, which is what makes reachability an upper bound.
    if rest and not match.group("colon") and not re.match(r"^(with|nointeract|id\s)", rest):
        return None
    return who, _unescape(match.group("text")), bool(match.group("colon"))


def load_translations(paths: list[str]) -> dict[tuple[str | None, str], str]:
    """Join key → target text, read from Ren'Py `translate` blocks.

    Keyed by (label, source text) and also by (None, source text), so a
    translation still resolves when the source line has moved to another label.
    """
    mapping: dict[tuple[str | None, str], str] = {}
    for path in paths:
        label: str | None = None
        pending: str | None = None
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue

                block = _TRANSLATE_BLOCK.match(line)
                if block:
                    label = _HASH_SUFFIX.sub("", block.group("label"))
                    pending = None
                    continue

                if line.startswith("#"):
                    # `# k "English source"` — the comment Ren'Py writes above
                    # each line so a translator can see what they are translating.
                    parsed = _say(line[1:].strip())
                    if parsed:
                        pending = parsed[1]
                    continue

                # `old "…"` / `new "…"`, the string-translation form.
                if line.startswith("old "):
                    parsed = _say(line[4:].strip())
                    if parsed:
                        pending = parsed[1]
                    continue
                if line.startswith("new "):
                    parsed = _say(line[4:].strip())
                    if parsed and pending is not None:
                        mapping[(label, pending)] = parsed[1]
                        mapping.setdefault((None, pending), parsed[1])
                        pending = None
                    continue

                parsed = _say(line)
                if parsed and pending is not None:
                    mapping[(label, pending)] = parsed[1]
                    mapping.setdefault((None, pending), parsed[1])
                    pending = None
    return mapping


class _Builder:
    """Walks the statement tree, building nodes, edges, and segments."""

    def __init__(self, script: Script, characters: dict[str, str],
                 translations: dict, filename: str):
        self.script = script
        self.characters = characters
        self.translations = translations
        self.filename = filename
        self.menus: dict[str, int] = {}
        self.counts: dict[str, int] = {}

    def edge(self, src: str, dst: str) -> None:
        node = self.script.nodes.setdefault(src, Node(id=src))
        if dst not in node.successors:
            node.successors.append(dst)

    def segment(self, node_id: str, label: str, who: str | None,
                text: str, is_choice: bool, line: int) -> None:
        self.counts[node_id] = self.counts.get(node_id, 0) + 1
        target = self.translations.get((label, text)) or self.translations.get((None, text), "")
        self.script.add_segment(
            Segment(
                id=f"{node_id}#{self.counts[node_id]}",
                source=text,
                target=target,
                node_id=node_id,
                speaker=self.characters.get(who) if who else None,
                is_choice=is_choice,
                notes="",
                location=f"{self.filename}:{line}",
            )
        )

    def walk(self, stmts: list[Stmt], label: str, node_id: str) -> bool:
        """Process a block. Returns True if every path through it terminates."""
        self.script.nodes.setdefault(node_id, Node(id=node_id))
        terminates = False
        index = 0

        while index < len(stmts):
            stmt = stmts[index]
            index += 1
            text = stmt.text

            if _MENU.match(text):
                terminates = self.menu(stmt, label, node_id)
                continue

            jump = _JUMP.match(text)
            if jump:
                self.edge(node_id, jump.group("label"))
                terminates = True
                continue

            call = _CALL.match(text)
            if call:
                # `call` returns to the next statement, so it adds an edge but
                # does not end the block.
                self.edge(node_id, call.group("label"))
                continue

            if _RETURN.match(text):
                terminates = True
                continue

            branch = _IF.match(text)
            if branch and branch.group("kind") == "if":
                group = [stmt]
                while index < len(stmts):
                    following = _IF.match(stmts[index].text)
                    if not following or following.group("kind") == "if":
                        break
                    group.append(stmts[index])
                    index += 1
                has_else = any(_IF.match(s.text).group("kind") == "else" for s in group)
                all_terminate = all(self.walk(s.children, label, node_id) for s in group)
                # Without an else, the guard can be false and control falls
                # through, so the block only terminates if every branch does
                # *and* one of them always runs.
                terminates = all_terminate and has_else
                continue
            if branch:
                continue                        # stray elif/else; already consumed

            parsed = _say(text)
            if parsed:
                who, content, _ = parsed
                self.segment(node_id, label, who, content, False, stmt.line)
                terminates = False
            # Anything else — scene, show, $, with — carries no text and no edge.

        return terminates

    def menu(self, stmt: Stmt, label: str, node_id: str) -> bool:
        count = self.menus.get(label, 0) + 1
        self.menus[label] = count
        menu_id = f"{label}:menu{count}"
        self.script.add_node(Node(id=menu_id, kind="menu"))
        self.edge(node_id, menu_id)

        choices = 0
        all_terminate = True
        for child in stmt.children:
            parsed = _say(child.text)
            if not parsed or not parsed[2]:
                continue                        # the optional prompt line, or a `$` setup
            choices += 1
            who, content, _ = parsed
            self.segment(menu_id, label, who, content, True, child.line)

            # A choice body holding dialogue is its own node; one that only
            # jumps is an edge and nothing more.
            body_has_text = any(
                _say(s.text) and not _say(s.text)[2] for s in child.children
            )
            if body_has_text:
                body_id = f"{menu_id}:c{choices}"
                self.script.add_node(Node(id=body_id))
                self.edge(menu_id, body_id)
                all_terminate &= self.walk(child.children, label, body_id)
            else:
                all_terminate &= self.walk(child.children, label, menu_id)

        return bool(choices) and all_terminate


def parse(path: str, translations: list[str] | None = None, **_) -> Script:
    """Read a Ren'Py script, joining one or more translation files onto it."""
    blocks = _blocks(path)
    filename = os.path.basename(path)
    script = Script(
        name=os.path.splitext(filename)[0],
        source_format="renpy",
    )
    builder = _Builder(
        script,
        _characters(blocks),
        load_translations(list(translations or [])),
        filename,
    )

    labels = [(s, _LABEL.match(s.text)) for s in blocks]
    labels = [(s, m.group("name")) for s, m in labels if m]

    for position, (stmt, name) in enumerate(labels):
        script.add_node(Node(id=name, kind="label", label=name))
        terminates = builder.walk(stmt.children, name, name)
        # Ren'Py runs off the end of a label into whatever is defined next.
        # Inventing that edge after a `return` would make dead content look
        # reachable and silence graph.unreachable.
        if not terminates and position + 1 < len(labels):
            builder.edge(name, labels[position + 1][1])

    script.entry_nodes = ["start"] if "start" in script.nodes else script._infer_roots()
    return script
