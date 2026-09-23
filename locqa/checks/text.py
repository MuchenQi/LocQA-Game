"""Length budgets, terminology, and cross-string consistency.

These are language-neutral in mechanism and locale-specific only in their
configuration. What makes them worth more than a spreadsheet formula is that
they measure the string the engine will render — placeholders substituted at
their worst case, width counted in the units the box is actually quoted in —
rather than the string as it sits in the sheet.
"""

from __future__ import annotations

import re
from collections import defaultdict
from functools import lru_cache
from typing import Iterable

from ..model import (
    Finding, Script, Segment, Severity,
    display_width, iter_lines, normalize_breaks, wrap_by_width,
)
from .base import Check, Config, register
from .integrity import expand_placeholders
from .japanese import _strip_markup

_WHITESPACE = re.compile(r"\s+")


@lru_cache(maxsize=512)
def _term_pattern(term: str) -> re.Pattern:
    """Match a term, with word boundaries only where the script has them.

    `\\b` around a Latin term stops `Save` matching inside `Saved`. Japanese
    is unsegmented, so the same guard there would match nothing at all.
    """
    escaped = re.escape(term)
    if re.match(r"^\w", term, re.ASCII) and re.search(r"\w$", term, re.ASCII):
        return re.compile(rf"\b{escaped}\b", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def _checkable(seg: Segment) -> bool:
    return seg.is_translated and not seg.do_not_translate


def _normalize_source(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


@register
class LengthBudget(Check):
    id = "length.budget"
    tier = 1
    title = "Length budget"
    description = "Target exceeds its width or line budget once placeholders are substituted."
    default_severity = Severity.MAJOR

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for seg in script:
            if not _checkable(seg):
                continue

            expanded = expand_placeholders(
                seg.target, config.placeholder_widths, config.default_placeholder_width
            )
            # Normalized here as well as inside wrap_by_width, because the
            # total width below measures `expanded` directly.
            expanded = normalize_breaks(expanded)
            width = seg.max_width or config.default_max_width

            if width:
                # A width budget with no stated line count means one line.
                # That is what a button label is, and treating an unstated
                # count as "unlimited" would pass every overflow in the sheet.
                budget_lines = seg.max_lines or 1
                lines = wrap_by_width(expanded, width, config.ambiguous_wide)
                if len(lines) <= budget_lines:
                    continue

                total = display_width(expanded, config.ambiguous_wide)
                if budget_lines == 1:
                    # Framed as width, because that is the budget the UI team
                    # quoted and the number the reviewer has to act on.
                    yield self.finding(
                        config,
                        f"Target is {total} half-width units against a budget of {width}.",
                        segment=seg,
                        excerpt=seg.target,
                        suggestion=f"Shorten by {total - width} half-width units, "
                        f"or raise the budget.",
                        meta={"width": total, "budget": width, "expanded": expanded},
                    )
                else:
                    # Width overflow implies the line budget is blown, so only
                    # the line count is reported — one bug, one finding.
                    yield self.finding(
                        config,
                        f"Target wraps to {len(lines)} lines at {width} half-width units; "
                        f"the box holds {budget_lines}.",
                        segment=seg,
                        excerpt=seg.target,
                        suggestion="Shorten the string or raise the line budget.",
                        meta={"lines": len(lines), "budget": budget_lines, "width": width},
                    )
                continue

            if seg.max_lines:
                # No width budget: the only thing measurable is explicit breaks.
                hard = [l for l in iter_lines(expanded)]
                if len(hard) > seg.max_lines:
                    yield self.finding(
                        config,
                        f"Target has {len(hard)} hard line breaks; the box holds {seg.max_lines}.",
                        segment=seg,
                        excerpt=seg.target,
                        meta={"lines": len(hard), "budget": seg.max_lines},
                    )


@register
class GlossaryTerm(Check):
    id = "term.glossary"
    tier = 1
    title = "Glossary adherence"
    description = "A source term with an approved rendering that the target does not use."
    default_severity = Severity.MAJOR

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        if not config.glossary:
            return
        for seg in script:
            if not _checkable(seg):
                continue
            target = _strip_markup(seg.target)
            for term, approved in config.glossary.items():
                if not approved or not _term_pattern(term).search(seg.source):
                    continue
                if any(_term_pattern(a).search(target) for a in approved):
                    continue
                shown = "、".join(f"「{a}」" for a in approved)
                yield self.finding(
                    config,
                    f"Source uses the glossary term “{term}”, but the target does not use "
                    f"its approved rendering {shown}.",
                    segment=seg,
                    excerpt=seg.target,
                    suggestion=f"Render “{term}” as {shown}.",
                    meta={"term": term, "approved": list(approved)},
                )


@register
class ForbiddenTerm(Check):
    id = "term.forbidden"
    tier = 1
    title = "Forbidden rendering"
    description = "A banned rendering appears in the target."
    default_severity = Severity.MAJOR

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        if not config.forbidden:
            return
        for seg in script:
            if not _checkable(seg):
                continue
            target = _strip_markup(seg.target)
            for term, reason in config.forbidden.items():
                match = _term_pattern(term).search(target)
                if not match:
                    continue
                yield self.finding(
                    config,
                    f"Target contains the forbidden rendering 「{term}」."
                    + (f" {reason}" if reason else ""),
                    segment=seg,
                    excerpt=seg.target,
                    span=(match.start(), match.end()),
                    suggestion=reason or "",
                    meta={"term": term, "reason": reason},
                )


@register
class DoNotTranslate(Check):
    id = "term.dnt"
    tier = 1
    title = "Do-not-translate term"
    description = "A trademark or proper noun that must survive into the target verbatim."
    default_severity = Severity.MAJOR

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        if not config.do_not_translate:
            return
        for seg in script:
            if not _checkable(seg):
                continue
            for term in config.do_not_translate:
                if term not in seg.source:
                    continue
                # Case-sensitive on the way out: a trademark that survives with
                # the wrong casing has not really survived.
                if term in seg.target:
                    continue
                yield self.finding(
                    config,
                    f"“{term}” must not be translated, but does not appear in the target.",
                    segment=seg,
                    excerpt=seg.target,
                    suggestion=f"Keep “{term}” in Latin script, exactly as written.",
                    meta={"term": term},
                )


@register
class SourceConsistency(Check):
    id = "consistency.source"
    tier = 1
    title = "Inconsistent translation"
    description = "One source string rendered several different ways."
    default_severity = Severity.MINOR

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        groups: dict[str, list[Segment]] = defaultdict(list)
        for seg in script:
            if _checkable(seg) and seg.source.strip():
                groups[_normalize_source(seg.source)].append(seg)

        for source, segs in groups.items():
            renderings: dict[str, list[Segment]] = defaultdict(list)
            for seg in segs:
                renderings[seg.target.strip()].append(seg)
            if len(renderings) < 2:
                continue

            ranked = sorted(renderings.items(), key=lambda kv: -len(kv[1]))
            top = len(ranked[0][1])
            # With a clear majority, the minority renderings are the outliers
            # and the majority is presumed correct. With a tie there is no
            # outlier to point at, so every rendering is reported and the
            # reviewer picks.
            tied = sum(1 for _, v in ranked if len(v) == top) > 1
            others = "、".join(f"「{t}」" for t, _ in ranked if t)

            for target, hits in ranked:
                if not tied and len(hits) == top:
                    continue
                for seg in hits:
                    yield self.finding(
                        config,
                        f"“{source}” is rendered {len(renderings)} different ways across the "
                        f"file: {others}.",
                        segment=seg,
                        excerpt=seg.target,
                        suggestion="Pick one rendering and apply it everywhere, or add the "
                        "distinction to the glossary if the contexts genuinely differ.",
                        meta={"source": source, "renderings": list(renderings)},
                    )


@register
class TargetConsistency(Check):
    id = "consistency.target"
    tier = 1
    title = "Collapsed distinction"
    description = "Several different source strings rendered as one target."
    default_severity = Severity.MINOR

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        groups: dict[str, set[str]] = defaultdict(set)
        holders: dict[str, list[Segment]] = defaultdict(list)
        for seg in script:
            if _checkable(seg) and seg.source.strip():
                key = seg.target.strip()
                groups[key].add(_normalize_source(seg.source))
                holders[key].append(seg)

        for target, sources in groups.items():
            if len(sources) < 2:
                continue
            shown = "、".join(f"“{s}”" for s in sorted(sources))
            for seg in holders[target]:
                yield self.finding(
                    config,
                    f"「{target}」 renders {len(sources)} different source strings: {shown}. "
                    "A distinction the source draws is not drawn in the target.",
                    segment=seg,
                    excerpt=seg.target,
                    suggestion="Confirm the strings really are interchangeable in context.",
                    meta={"target": target, "sources": sorted(sources)},
                )
