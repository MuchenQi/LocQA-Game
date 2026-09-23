"""Placeholder and markup integrity.

The checks here are format-agnostic and language-agnostic: a dropped `{ITEM}`
is a bug in every locale. They run first because everything downstream depends
on knowing which parts of a string are prose and which are machinery — the
Japanese orthography checks measure prose only, and the length checks measure
placeholders at their substituted width.

Tokenizing is done once, in `tokens()`, so that placeholders and tags can never
disagree about where one ends and the other begins.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, NamedTuple

from ..model import Finding, Script, Severity
from .base import Check, Config, register

# Markup tag names, as opposed to substitution placeholders. The bracket
# families are shared between the two — Ren'Py writes text tags as `{b}` and
# interpolation as `[name]`, HTML-ish formats do the reverse — so the name
# decides, not the delimiter.
#
# These are only consulted for a *lowercase* bare token, because markup is
# conventionally lowercase and placeholders are conventionally UPPER_SNAKE.
# Matching case-insensitively would read the placeholder `{A}` as an anchor
# tag and `{B}` as bold, which is not a hypothetical: single-letter
# placeholders are ordinary in argument-ordered strings.
TAG_NAMES = frozenset("""
    a b i u s p k w nw br nobr sub sup em strong del ins small big
    color colour size font face align center left right
    alpha outlinecolor cps fast done space vspace img image
    ruby rb rt rp plain strike underline bold italic
""".split())

# One pass, longest-first, so `%1$s` can never be read as `%1` plus stray text
# and `<<player>>` can never be read as `<` plus `<player>`.
_TOKEN = re.compile(
    r"""
      (?P<pct_literal>%%)
    | (?P<pct_named>%\(\w+\)[-+#0]*\d*(?:\.\d+)?[hlL]?[diouxXeEfgGcrs])
    | (?P<printf>%(?:\d+\$)?[-+#0]*\d*(?:\.\d+)?[hlL]?[diouxXeEfgGcrs])
    | (?P<dangle><<[^<>]+>>)
    | (?P<dollar>\$\{[^{}]*\})
    | (?P<brace>\{[^{}]*\})
    | (?P<bracket>\[[^\[\]]*\])
    | (?P<angle><[^<>]*>)
    """,
    re.VERBOSE,
)

# Delimiter pairs, for stripping a bracket token down to its inner text.
_DELIMS = {
    "dangle": (2, 2),
    "dollar": (2, 1),
    "brace": (1, 1),
    "bracket": (1, 1),
    "angle": (1, 1),
}

# Full-width forms a Japanese IME produces when the input mode is not switched
# back to half-width. They are visually near-identical to the ASCII originals.
FULLWIDTH_TO_ASCII = {
    "｛": "{", "｝": "}", "［": "[", "］": "]",
    "＜": "<", "＞": ">", "％": "%", "＄": "$",
    "（": "(", "）": ")",
}
_FULLWIDTH_RE = re.compile("[" + "".join(FULLWIDTH_TO_ASCII) + "]")


class Token(NamedTuple):
    """One placeholder or markup tag, located in the string."""

    text: str                       # the literal token, e.g. "{ITEM}" or "</b>"
    kind: str                       # "placeholder" | "tag"
    name: str                       # "ITEM" | "b"
    role: str                       # placeholder: ""; tag: "open" | "close" | "self"
    start: int
    end: int


def _bare_name(inner: str) -> str:
    """The tag name inside a bracket token, without slashes or attributes."""
    return inner.lstrip("/").rstrip("/").strip().split("=", 1)[0].strip()


def _closing_names(text: str) -> set[str]:
    """Names that appear as an explicit closing tag — `</b>`, `{/i}`, `[/color]`."""
    found = set()
    for m in _TOKEN.finditer(text):
        group = m.lastgroup or ""
        if group not in ("brace", "bracket", "angle"):
            continue
        head, tail = _DELIMS[group]
        inner = m.group()[head:-tail].strip()
        if inner.startswith("/"):
            found.add(_bare_name(inner).lower())
    return found


def tokens(text: str, also: str = "") -> list[Token]:
    """Every placeholder and markup tag in `text`, in order of appearance.

    Classification is per string, not per token, because a bare `{b}` is only
    knowable as markup in the company of its closer. Scanning for explicit
    closers first means `<B>bold</B>` resolves both halves the same way, where
    a token-at-a-time rule would call the opener a placeholder and the closer
    a tag, then report the mismatch it had just invented.

    `also` extends that evidence with a sibling string without tokenizing it.
    Callers pass a segment's other side: markup vocabulary is a property of
    the segment, so a target that drops `</B>` must not thereby reclassify its
    own `<B>` as a placeholder and produce two findings for one dropped tag.
    """
    closed = _closing_names(text) | (_closing_names(also) if also else set())

    out: list[Token] = []
    for m in _TOKEN.finditer(text):
        group = m.lastgroup or ""
        token = m.group()

        if group == "pct_literal":
            continue                        # `%%` is an escaped literal, not a slot
        if group in ("printf", "pct_named"):
            out.append(Token(token, "placeholder", token, "", m.start(), m.end()))
            continue

        head, tail = _DELIMS[group]
        inner = token[head:-tail].strip()
        if not inner:
            continue                        # `{}` / `<>` — not a token, just punctuation

        if group in ("dangle", "dollar"):
            out.append(Token(token, "placeholder", inner, "", m.start(), m.end()))
            continue

        closing = inner.startswith("/")
        name = _bare_name(inner)
        is_tag = (
            closing
            or "=" in inner
            or name.lower() in closed
            or (name.islower() and name in TAG_NAMES)
        )
        if is_tag:
            role = "close" if closing else ("self" if inner.endswith("/") else "open")
            out.append(Token(token, "tag", name, role, m.start(), m.end()))
        else:
            out.append(Token(token, "placeholder", inner, "", m.start(), m.end()))
    return out


def extract_placeholders(text: str, also: str = "") -> list[str]:
    """Runtime substitution slots, in order: `{ITEM}`, `%1$s`, `[BTN_A]`, `<<name>>`."""
    return [t.text for t in tokens(text, also) if t.kind == "placeholder"]


def extract_tags(text: str, also: str = "") -> list[str]:
    """Markup tags, in order: `<b>`, `</b>`, `[color=red]`, `{i}`."""
    return [t.text for t in tokens(text, also) if t.kind == "tag"]


def expand_placeholders(
    text: str, widths: dict[str, int] | None = None, default: int = 8
) -> str:
    """Replace each placeholder with filler at its worst-case substituted width.

    A textbox that fits `{ITEM}` but not a fourteen-character item name is a
    bug that surfaces in one save file out of ten. Measuring the literal token
    measures a string no player will ever see.
    """
    widths = widths or {}
    out: list[str] = []
    cursor = 0
    for t in tokens(text):
        if t.kind != "placeholder":
            continue
        width = widths.get(t.text, widths.get(t.name, default))
        out.append(text[cursor:t.start])
        out.append("X" * max(0, width))     # ASCII: one half-width unit per char
        cursor = t.end
    out.append(text[cursor:])
    return "".join(out)


def recovered_by_halfwidth(source: str, target: str) -> Counter:
    """Placeholders the target would regain if its full-width delimiters were fixed.

    Shared by `placeholder.corrupt`, which reports them, and
    `placeholder.parity`, which stays quiet about them. A corrupted `％d` is
    also, trivially, a missing `%d`; the check that can name the cause owns
    the finding.
    """
    if not _FULLWIDTH_RE.search(target):
        return Counter()
    repaired = _FULLWIDTH_RE.sub(lambda m: FULLWIDTH_TO_ASCII[m.group()], target)
    gained = Counter(extract_placeholders(repaired, source)) - Counter(
        extract_placeholders(target, source)
    )
    in_source = Counter(extract_placeholders(source, target))
    return Counter({p: n for p, n in gained.items() if in_source[p]})


# -- checks ------------------------------------------------------------------


@register
class PlaceholderParity(Check):
    id = "placeholder.parity"
    tier = 1
    title = "Placeholder parity"
    description = "Placeholders dropped, invented, or repeated a different number of times."
    default_severity = Severity.BLOCKER

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for seg in script:
            if not seg.is_translated or seg.do_not_translate:
                continue
            source = Counter(extract_placeholders(seg.source, seg.target))
            target = Counter(extract_placeholders(seg.target, seg.source))
            if source == target:
                continue

            # placeholder.corrupt reports these with the cause attached.
            target = target + recovered_by_halfwidth(seg.source, seg.target)
            if source == target:
                continue

            missing = sorted((source - target).elements())
            invented = sorted((target - source).elements())

            # A token that is present on both sides but counted differently is
            # a repeat, not a drop; separating them keeps the message true.
            repeated = sorted(
                p for p in set(missing) & set(invented) if source[p] != target[p]
            )
            missing = [p for p in missing if p not in set(repeated)]
            invented = [p for p in invented if p not in set(repeated)]

            if missing:
                yield self.finding(
                    config,
                    f"Placeholder {_join(missing)} is in the source but not the target. "
                    "The value will never be substituted.",
                    segment=seg,
                    excerpt=seg.target,
                    suggestion=f"Reinsert {_join(missing)} where the value belongs in Japanese "
                    "word order.",
                    meta={"missing": missing},
                )
            if invented:
                yield self.finding(
                    config,
                    f"Placeholder {_join(invented)} appears in the target but not the source. "
                    "It will render literally.",
                    segment=seg,
                    excerpt=seg.target,
                    meta={"invented": invented},
                )
            for token in repeated:
                yield self.finding(
                    config,
                    f"Placeholder {token} appears {source[token]}× in the source and "
                    f"{target[token]}× in the target.",
                    segment=seg,
                    excerpt=seg.target,
                    severity=Severity.MAJOR,
                    meta={"placeholder": token, "source": source[token], "target": target[token]},
                )


@register
class PlaceholderCorrupt(Check):
    id = "placeholder.corrupt"
    tier = 1
    title = "Full-width placeholder delimiters"
    description = "｛｝％＄ typed with the IME in Japanese mode — substitution silently stops firing."
    default_severity = Severity.BLOCKER

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for seg in script:
            if not seg.is_translated or seg.do_not_translate:
                continue
            # A placeholder that appears only after the full-width delimiters
            # are normalized, and that the source actually has, is one the IME
            # ate. Anything else is a full-width character doing its
            # legitimate job — ％ after a number is correct Japanese, and
            # flagging it is how a check loses a reviewer's trust.
            recovered = sorted(recovered_by_halfwidth(seg.source, seg.target).elements())
            if not recovered:
                continue

            bad = sorted({ch for ch in seg.target if ch in FULLWIDTH_TO_ASCII})
            shown = " ".join(f"{c} (U+{ord(c):04X})" for c in bad)
            yield self.finding(
                config,
                f"Placeholder {_join(recovered)} is written with full-width delimiters "
                f"— {shown}. It looks correct on screen and never substitutes.",
                segment=seg,
                excerpt=seg.target,
                suggestion=f"Retype the delimiters in half-width: {_join(recovered)}.",
                meta={"recovered": recovered, "characters": bad},
            )


@register
class TagParity(Check):
    id = "tag.parity"
    tier = 1
    title = "Markup parity and nesting"
    description = "Rich-text tags dropped, invented, left unclosed, or crossed."
    default_severity = Severity.MAJOR

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for seg in script:
            if not seg.is_translated or seg.do_not_translate:
                continue

            source_tags = [t for t in tokens(seg.source, seg.target) if t.kind == "tag"]
            target_tags = [t for t in tokens(seg.target, seg.source) if t.kind == "tag"]
            if not source_tags and not target_tags:
                continue

            structural: set[str] = set()
            for finding, name in self._structure(seg, config, target_tags):
                structural.add(name)
                yield finding

            # A tag left unclosed is also, trivially, a tag whose closer is
            # missing. Reporting both is one bug counted twice, so the
            # structural finding wins and the parity pass stays quiet.
            source_names = Counter(t.name.lower() for t in source_tags)
            target_names = Counter(t.name.lower() for t in target_tags)
            for name in sorted((source_names - target_names)):
                if name in structural:
                    continue
                yield self.finding(
                    config,
                    f"Tag <{name}> is in the source but missing from the target.",
                    segment=seg,
                    excerpt=seg.target,
                    meta={"tag": name},
                )
            for name in sorted((target_names - source_names)):
                if name in structural:
                    continue
                yield self.finding(
                    config,
                    f"Tag <{name}> appears in the target but not the source.",
                    segment=seg,
                    excerpt=seg.target,
                    meta={"tag": name},
                )

    def _structure(self, seg, config: Config, target_tags: list[Token]):
        """Walk the target's tags as a stack, yielding (finding, tag name)."""
        stack: list[Token] = []
        for tag in target_tags:
            if tag.role == "self":
                continue
            if tag.role == "open":
                stack.append(tag)
                continue

            name = tag.name.lower()
            if stack and stack[-1].name.lower() == name:
                stack.pop()
                continue

            match = next(
                (i for i in range(len(stack) - 1, -1, -1) if stack[i].name.lower() == name),
                None,
            )
            if match is None:
                yield (
                    self.finding(
                        config,
                        f"Closing {tag.text} in the target was never opened.",
                        segment=seg,
                        excerpt=seg.target,
                        span=(tag.start, tag.end),
                        meta={"tag": name},
                    ),
                    name,
                )
                continue

            inner = stack[-1]
            # Remove only the crossed tag, leaving the inner one open, so a
            # single misordering produces a single finding instead of a
            # cascade of spurious "never closed" reports after it.
            stack.pop(match)
            yield (
                self.finding(
                    config,
                    f"Closing {tag.text} crosses {inner.text}, which is still open. "
                    "Overlapping markup renders unpredictably.",
                    segment=seg,
                    excerpt=seg.target,
                    span=(tag.start, tag.end),
                    meta={"tag": name, "crossed": inner.name.lower()},
                ),
                name,
            )

        for tag in stack:
            yield (
                self.finding(
                    config,
                    f"Tag {tag.text} is opened in the target but never closed.",
                    segment=seg,
                    excerpt=seg.target,
                    span=(tag.start, tag.end),
                    severity=Severity.MAJOR,
                    meta={"tag": tag.name.lower()},
                ),
                tag.name.lower(),
            )


def _join(items: list[str]) -> str:
    return "、".join(items) if len(items) > 1 else items[0]
