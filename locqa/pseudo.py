"""Pseudolocalization.

Run before a single word is translated. It answers two questions that
otherwise surface in the last week of a project: does the UI survive text
30–40% longer than English, and does the build render non-ASCII at all.

Placeholders and markup are passed through untouched, because the point is to
test the layout, not to find out whether your own substitution still works.
A pseudolocalized build with mangled placeholders teaches nothing except that
the pseudolocalizer is broken.
"""

from __future__ import annotations

import math
import re

from .checks.integrity import tokens

# Latin-1/Latin Extended-A stand-ins: same skeleton, visibly not English, and
# every one of them is outside ASCII so a font or encoding gap shows up as
# tofu rather than passing silently.
ACCENTS = {
    "a": "á", "b": "ƀ", "c": "ç", "d": "ð", "e": "é", "f": "ƒ", "g": "ğ",
    "h": "ĥ", "i": "í", "j": "ĵ", "k": "ķ", "l": "ĺ", "m": "ɱ", "n": "ñ",
    "o": "ó", "p": "þ", "q": "ɋ", "r": "ŕ", "s": "ŝ", "t": "ţ", "u": "ú",
    "v": "ṽ", "w": "ŵ", "x": "ẋ", "y": "ý", "z": "ž",
    "A": "Á", "B": "Ɓ", "C": "Ç", "D": "Ð", "E": "É", "F": "Ƒ", "G": "Ğ",
    "H": "Ĥ", "I": "Í", "J": "Ĵ", "K": "Ķ", "L": "Ĺ", "M": "Ϻ", "N": "Ñ",
    "O": "Ó", "P": "Þ", "Q": "Ǫ", "R": "Ŕ", "S": "Ŝ", "T": "Ţ", "U": "Ú",
    "V": "Ṽ", "W": "Ŵ", "X": "Ẋ", "Y": "Ý", "Z": "Ž",
}

PAD = "·"
_ESCAPE = re.compile(r"\\[nrt\\]")
BRACKETS = ("⟦", "⟧")


def pseudolocalize(
    text: str,
    expansion: float = 1.3,
    brackets: tuple[str, str] = BRACKETS,
    accent: bool = True,
) -> str:
    """Accent, pad, and bracket a string, leaving its machinery intact.

    The brackets are the useful part: they make truncation visible. A label
    that reads `⟦Ŝéţţíñğŝ···` has been clipped by its container, and you can
    see it in a screenshot without knowing what the string was.
    """
    if not text:
        return f"{brackets[0]}{brackets[1]}"

    # Escapes are machinery too: loc sheets store a break as backslash-n, and
    # accenting the n turns `\n` into `\ñ` — a build with no line breaks.
    protected = sorted(
        [(t.start, t.end) for t in tokens(text)]
        + [m.span() for m in _ESCAPE.finditer(text)]
    )

    out: list[str] = []
    prose = 0
    index = 0
    while index < len(text):
        span = next(((s, e) for s, e in protected if s == index), None)
        if span:
            out.append(text[span[0]:span[1]])   # placeholder or tag, verbatim
            index = span[1]
            continue
        char = text[index]
        out.append(ACCENTS.get(char, char) if accent else char)
        if not char.isspace():
            prose += 1
        index += 1

    # Expansion is measured against the prose only. Padding the placeholders
    # too would inflate the string by an amount that depends on how many slots
    # it happens to have, which is not a property of the translation.
    padding = max(0, math.ceil(prose * expansion) - prose)
    return f"{brackets[0]}{''.join(out)}{PAD * padding}{brackets[1]}"
