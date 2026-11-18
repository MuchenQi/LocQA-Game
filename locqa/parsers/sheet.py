"""Loc sheets — CSV and TSV.

The spreadsheet is the format most translation actually arrives in, and it is
the one with no schema. Every studio names its columns differently and every
export tool mangles them differently, so the parser matches headers against a
synonym table rather than making the user write a column map. `key`,
`StringID`, and `identifier` all resolve; so do `japanese`, `JP`, and
`target_text`.

Malformed cells raise rather than defaulting. A width column containing `n/a`
that silently becomes "no budget" means the length check quietly stops running
for that row, and a check that silently stops running is worse than one that
was never written.
"""

from __future__ import annotations

import csv
import os
import re

from ..model import Script, Segment

# Canonical field → header synonyms, compared with separators and case
# stripped, so `Source Text`, `source_text`, and `SourceText` are one key.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "id": ("id", "key", "stringid", "strid", "msgid", "identifier", "tag", "keyname"),
    "source": (
        "source", "sourcetext", "sourcestring", "english", "en", "enus",
        "src", "original", "originaltext", "base", "basetext",
    ),
    "target": (
        "target", "targettext", "targetstring", "japanese", "ja", "jajp", "jp",
        "jptext", "translation", "translated", "translatedtext", "localized",
    ),
    "speaker": ("speaker", "character", "char", "chara", "actor", "who", "npc"),
    "node_id": ("nodeid", "node", "scene", "sceneid", "block", "section", "context"),
    "max_width": (
        "maxwidth", "width", "charlimit", "characterlimit", "limit", "maxchars",
        "maxlen", "maxlength", "charmax", "lengthlimit", "widthlimit",
    ),
    "max_lines": ("maxlines", "lines", "linelimit", "maxrows", "numlines"),
    "is_choice": ("ischoice", "choice", "ismenu", "menuoption", "option"),
    "do_not_translate": ("donottranslate", "dnt", "notranslate", "locked", "keepsource"),
    "notes": ("notes", "note", "comment", "comments", "description", "remarks", "instructions"),
}

REQUIRED = ("source", "target")

_TRUE = {"true", "t", "yes", "y", "1", "x", "✓", "o", "○"}
_FALSE = {"false", "f", "no", "n", "0", "", "-", "×", "✗"}

_NORMALIZE = re.compile(r"[^a-z0-9]+")


def _normalize(header: str) -> str:
    return _NORMALIZE.sub("", header.strip().lower())


def resolve_columns(headers: list[str]) -> dict[str, str]:
    """Map canonical field names to the actual headers present.

    Unmatched fields are absent from the result rather than present-and-None,
    so callers can ask `"speaker" in cols` and mean it. A header is claimed by
    at most one field; where two headers match the same field, the leftmost
    wins, which is the column a human reading the sheet would take as primary.
    """
    normalized = [(h, _normalize(h)) for h in headers]
    claimed: set[str] = set()
    resolved: dict[str, str] = {}

    for field, synonyms in SYNONYMS.items():
        for header, norm in normalized:
            if header in claimed or not norm:
                continue
            if norm in synonyms:
                resolved[field] = header
                claimed.add(header)
                break
    return resolved


def _int(value: str, path: str, row: int, column: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise ValueError(
            f"{os.path.basename(path)}:{row}: column {column!r} is {value!r}, "
            "which is not a whole number. Leave it blank if the string has no budget."
        ) from None


def _bool(value: str, path: str, row: int, column: str) -> bool:
    text = (value or "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(
        f"{os.path.basename(path)}:{row}: column {column!r} is {value!r}, "
        f"which is not a yes/no value."
    )


def _delimiter(path: str, sample: str) -> str:
    default = "\t" if path.lower().endswith(".tsv") else ","
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        return default                      # single column, or too short to sniff


def parse(path: str, **_) -> Script:
    """Read a loc sheet into a Script. Flat: no nodes unless a node column exists."""
    # utf-8-sig: Excel writes a BOM, and a BOM on the first header turns `key`
    # into `﻿key`, which then matches no synonym at all.
    with open(path, encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        reader = csv.DictReader(fh, delimiter=_delimiter(path, sample))
        headers = reader.fieldnames or []
        cols = resolve_columns(list(headers))

        missing = [f for f in REQUIRED if f not in cols]
        if missing:
            raise ValueError(
                f"{os.path.basename(path)}: no column found for {', '.join(missing)}. "
                f"Headers present: {', '.join(headers) or '(none)'}. "
                f"Accepted names for {missing[0]}: {', '.join(SYNONYMS[missing[0]][:6])}…"
            )

        script = Script(
            name=os.path.splitext(os.path.basename(path))[0],
            source_format="sheet",
        )

        for offset, row in enumerate(reader):
            line = offset + 2                   # +1 for the header, +1 for 1-indexing
            if not any((v or "").strip() for v in row.values()):
                continue                        # blank spacer row

            def cell(field: str) -> str:
                return (row.get(cols[field]) or "").strip() if field in cols else ""

            def text(field: str) -> str:
                # Source and target keep their whitespace. str.strip() also
                # removes U+3000, and a leading full-width space is the most
                # common IME artifact there is — stripping it here would hide
                # it from ja.spacing, which exists to report it.
                return row.get(cols[field]) or ""

            seg_id = cell("id") or f"row{line}"
            segment = Segment(
                id=seg_id,
                source=text("source"),
                target=text("target"),
                node_id=cell("node_id") or None,
                speaker=cell("speaker") or None,
                max_width=_int(cell("max_width"), path, line, cols.get("max_width", "")),
                max_lines=_int(cell("max_lines"), path, line, cols.get("max_lines", "")),
                is_choice=_bool(cell("is_choice"), path, line, cols.get("is_choice", ""))
                if "is_choice" in cols
                else False,
                do_not_translate=_bool(
                    cell("do_not_translate"), path, line, cols.get("do_not_translate", "")
                )
                if "do_not_translate" in cols
                else False,
                notes=cell("notes"),
                location=f"{os.path.basename(path)}:{line}",
            )
            try:
                script.add_segment(segment)
            except ValueError:
                # Duplicate keys in a loc sheet mean two rows fight over one
                # string at build time, and which one wins depends on the
                # importer. Name both rows so it can be fixed.
                first = script.segments[seg_id].location
                raise ValueError(
                    f"{os.path.basename(path)}:{line}: duplicate key {seg_id!r}, "
                    f"first seen at {first}."
                ) from None

    return script
