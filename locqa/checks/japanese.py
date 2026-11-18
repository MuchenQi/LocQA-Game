"""Japanese target-language checks.

Everything here encodes a convention that a Japanese QA lead would mark in red
on a proof, and that a generic string linter cannot see: punctuation width,
kinsoku shori, platform-dependent characters, and the wave-dash bug.
"""

from __future__ import annotations

import re
from typing import Iterable

from ..model import Finding, Script, Severity, display_width, iter_lines, wrap_by_width
from .base import Check, Config, register
from .integrity import extract_placeholders, extract_tags, layout_text

KANA = r"\u3040-\u309F\u30A0-\u30FF"
KANJI = r"\u4E00-\u9FFF\u3400-\u4DBF"
JAPANESE = re.compile(f"[{KANA}{KANJI}]")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")

# Characters forbidden at the start of a line (行頭禁則).
KINSOKU_NOT_FIRST = set("、。，．・：；？！ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮーヽヾ゛゜”）〕］｝〉》」』】〙〗〟’｠»%‰℃")
# Characters forbidden at the end of a line (行末禁則).
KINSOKU_NOT_LAST = set("“（〔［｛〈《「『【〘〖〝‘｟«＄£¥＃")

FULLWIDTH_PUNCT = {".": "。", ",": "、", "!": "！", "?": "？", ":": "：", ";": "；"}


def _strip_markup(text: str) -> str:
    """Remove placeholders and tags so orthography checks see only prose."""
    for token in extract_placeholders(text) + extract_tags(text):
        text = text.replace(token, "\x00" * len(token))
    return text.replace("\x00", "")


def _has_japanese(text: str) -> bool:
    return bool(JAPANESE.search(text))


@register
class Untranslated(Check):
    id = "ja.untranslated"
    tier = 1
    title = "Untranslated or source-copied"
    description = "Target is empty, identical to the source, or contains no Japanese script."
    default_severity = Severity.BLOCKER

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for seg in script:
            if seg.do_not_translate:
                continue
            if not seg.is_translated:
                yield self.finding(
                    config, "Target is empty.", segment=seg, excerpt=seg.source
                )
                continue
            stripped = _strip_markup(seg.target).strip()
            if seg.target.strip() == seg.source.strip() and LATIN_WORD.search(stripped):
                yield self.finding(
                    config,
                    "Target is identical to the source — likely a copied row.",
                    segment=seg,
                    excerpt=seg.target,
                    suggestion="If the string is intentionally left in English, mark it do-not-translate.",
                )
            elif stripped and not _has_japanese(stripped) and LATIN_WORD.search(stripped):
                yield self.finding(
                    config,
                    "Target contains no kana or kanji.",
                    segment=seg,
                    excerpt=seg.target,
                    severity=Severity.MAJOR,
                )


@register
class Charset(Check):
    id = "ja.charset"
    tier = 1
    title = "Font atlas coverage"
    description = "Flags 機種依存文字 and other characters outside the build's character repertoire."
    default_severity = Severity.BLOCKER

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        encoding = config.charset
        if not encoding:
            return
        for seg in script:
            if not seg.is_translated:
                continue
            bad = []
            for ch in _strip_markup(seg.target):
                if ch in "\n\t":
                    continue
                try:
                    ch.encode(encoding)
                except UnicodeEncodeError:
                    if ch not in bad:
                        bad.append(ch)
            if bad:
                shown = " ".join(f"{c} (U+{ord(c):04X})" for c in bad[:6])
                yield self.finding(
                    config,
                    f"Character(s) outside {encoding}: {shown}. These render as tofu "
                    "or corrupt the string if the font atlas is built from that repertoire.",
                    segment=seg,
                    excerpt=seg.target,
                    suggestion="Substitute an in-repertoire equivalent, or file a font request.",
                    meta={"characters": bad},
                )


@register
class WaveDash(Check):
    id = "ja.wave_dash"
    tier = 1
    title = "Wave dash / fullwidth tilde"
    description = "U+301C and U+FF5E look identical and break round-trips through Shift-JIS."
    default_severity = Severity.MINOR

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        seen: dict[str, list[str]] = {"\u301c": [], "\uff5e": []}
        for seg in script:
            for ch in seen:
                if ch in seg.target:
                    seen[ch].append(seg.id)
        if seen["\u301c"] and seen["\uff5e"]:
            for ch, ids in seen.items():
                name = "WAVE DASH U+301C" if ch == "\u301c" else "FULLWIDTH TILDE U+FF5E"
                for sid in ids:
                    seg = script.segments[sid]
                    yield self.finding(
                        config,
                        f"Mixed tilde codepoints in this file; this string uses {name}.",
                        segment=seg,
                        excerpt=seg.target,
                        suggestion="Normalize the whole file to one codepoint — pick the one the engine's font ships.",
                        meta={"codepoint": f"U+{ord(ch):04X}"},
                    )


@register
class Mojibake(Check):
    id = "ja.mojibake"
    tier = 1
    title = "Mojibake"
    description = "Replacement characters and classic UTF-8-read-as-Latin-1 sequences."
    default_severity = Severity.BLOCKER

    SIGNATURES = ("\ufffd", "ã\x81", "ã\x82", "â\x80", "ï¿½", "æ\x97")

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for seg in script:
            for sig in self.SIGNATURES:
                if sig in seg.target:
                    yield self.finding(
                        config,
                        "Target shows mojibake — the file was decoded with the wrong encoding "
                        "somewhere upstream.",
                        segment=seg,
                        excerpt=seg.target,
                        suggestion="Re-export from the source of truth as UTF-8; do not hand-repair.",
                    )
                    break


@register
class Punctuation(Check):
    id = "ja.punctuation"
    tier = 1
    title = "Punctuation width and convention"
    description = "Half-width punctuation in Japanese prose, ASCII ellipsis, and odd 三点リーダー counts."
    default_severity = Severity.MINOR

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for seg in script:
            text = _strip_markup(seg.target)
            if not _has_japanese(text):
                continue

            # Runs of two or more ASCII dots are an ellipsis, and the ellipsis
            # rule below reports them with the right advice (…… as a pair).
            # Letting the period rule claim them too turns one wrong ellipsis
            # into two findings, and doubled counts are how a report loses a
            # reviewer's trust.
            ellipses = [m.span() for m in re.finditer(r"\.{2,}", text)]

            for half, full in FULLWIDTH_PUNCT.items():
                # A half-width mark is only wrong when it sits against Japanese
                # script; inside an embedded English phrase it is correct.
                for m in re.finditer(re.escape(half), text):
                    if half == "." and any(s <= m.start() < e for s, e in ellipses):
                        continue
                    if _adjacent_to_japanese(text, m.start()):
                        yield self.finding(
                            config,
                            f"Half-width {half} in Japanese prose; convention is {full}.",
                            segment=seg,
                            excerpt=seg.target,
                            span=(m.start(), m.end()),
                            suggestion=f"Replace with {full}.",
                        )
                        break

            if "..." in text:
                yield self.finding(
                    config,
                    "ASCII ellipsis in Japanese prose; convention is the 三点リーダー ….",
                    segment=seg,
                    excerpt=seg.target,
                    suggestion="Replace ... with …… (a pair).",
                )

            runs = re.findall(r"…+", text)
            for run in runs:
                if len(run) % 2 == 1:
                    yield self.finding(
                        config,
                        f"三点リーダー appears in a run of {len(run)}; Japanese typesetting "
                        "sets them in even pairs.",
                        segment=seg,
                        excerpt=seg.target,
                        suggestion="Use …… or ………… as the pause length requires.",
                        severity=Severity.INFO,
                    )
                    break

            if re.search(r"[。、！？]\s+[" + KANA + KANJI + "]", text):
                yield self.finding(
                    config,
                    "Space after full-width punctuation; the glyph already carries its own spacing.",
                    segment=seg,
                    excerpt=seg.target,
                    suggestion="Delete the space.",
                )

            if re.search(r"[！？][。、]", text):
                yield self.finding(
                    config,
                    "Full stop or comma directly after ！ or ？ — redundant in Japanese.",
                    segment=seg,
                    excerpt=seg.target,
                    severity=Severity.INFO,
                )


def _adjacent_to_japanese(text: str, index: int) -> bool:
    before = text[index - 1] if index > 0 else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    return bool(JAPANESE.match(before or "") or JAPANESE.match(after or ""))


@register
class Kinsoku(Check):
    id = "ja.kinsoku"
    tier = 1
    title = "Kinsoku shori (禁則処理)"
    description = "Line breaks that strand punctuation at a line start or an opening bracket at a line end."
    default_severity = Severity.MAJOR

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for seg in script:
            width = seg.max_width or config.default_max_width
            if not seg.is_translated:
                continue
            if width:
                # The same laid-out string length.budget measures, so the two
                # checks agree on where the line breaks fall.
                expanded = layout_text(
                    seg.target, config.placeholder_widths, config.default_placeholder_width,
                    also=seg.source,
                )
                lines = wrap_by_width(expanded, width, config.ambiguous_wide)
            else:
                lines = [l for l in iter_lines(_strip_markup(seg.target))]
                if len(lines) < 2:
                    continue

            for i, line in enumerate(lines):
                if not line:
                    continue
                if i > 0 and line[0] in KINSOKU_NOT_FIRST:
                    yield self.finding(
                        config,
                        f"Line {i + 1} begins with {line[0]}, which 行頭禁則 forbids.",
                        segment=seg,
                        excerpt=seg.target,
                        suggestion="Rephrase so the break falls elsewhere, or enable kinsoku in the text renderer.",
                        meta={"line": i + 1, "char": line[0], "wrapped": lines},
                    )
                if i < len(lines) - 1 and line[-1] in KINSOKU_NOT_LAST:
                    yield self.finding(
                        config,
                        f"Line {i + 1} ends with {line[-1]}, which 行末禁則 forbids.",
                        segment=seg,
                        excerpt=seg.target,
                        meta={"line": i + 1, "char": line[-1], "wrapped": lines},
                    )


@register
class Spacing(Check):
    id = "ja.spacing"
    tier = 1
    title = "Spacing"
    description = "Full-width spaces, doubled spaces, and trailing whitespace."
    default_severity = Severity.MINOR

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for seg in script:
            if not seg.is_translated:
                continue
            target = seg.target
            if target != target.rstrip() or target != target.lstrip():
                yield self.finding(
                    config,
                    "Leading or trailing whitespace in the target.",
                    segment=seg,
                    excerpt=repr(target),
                    severity=Severity.MINOR,
                )
            if "  " in _strip_markup(target):
                yield self.finding(
                    config,
                    "Doubled half-width space.",
                    segment=seg,
                    excerpt=target,
                    severity=Severity.INFO,
                )
            if "\u3000" in target and "\u3000" not in seg.source:
                yield self.finding(
                    config,
                    "Full-width space (U+3000) in the target. Intentional in some layouts, "
                    "but frequently an IME artifact.",
                    segment=seg,
                    excerpt=target,
                    severity=Severity.INFO,
                )


@register
class NumberWidth(Check):
    id = "ja.number_width"
    tier = 1
    title = "Digit width consistency"
    description = "Mixed half-width and full-width digits within one file."
    default_severity = Severity.MINOR

    FW_DIGIT = re.compile(r"[０-９]")
    HW_DIGIT = re.compile(r"[0-9]")

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        fw, hw = [], []
        for seg in script:
            text = _strip_markup(seg.target)
            if self.FW_DIGIT.search(text):
                fw.append(seg)
            if self.HW_DIGIT.search(text):
                hw.append(seg)
        if fw and hw:
            minority = fw if len(fw) <= len(hw) else hw
            style = "full-width" if minority is fw else "half-width"
            majority = "half-width" if minority is fw else "full-width"
            for seg in minority:
                yield self.finding(
                    config,
                    f"Uses {style} digits where the rest of the file uses {majority}.",
                    segment=seg,
                    excerpt=seg.target,
                    suggestion=f"Normalize to {majority} digits across the file.",
                )
