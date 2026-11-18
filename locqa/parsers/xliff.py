"""XLIFF 2.0.

The interesting part is inline codes. XLIFF represents a placeholder as a
`<ph>` element carrying its plain-text equivalent in `equiv`, so a naive
`itertext()` reads "Press  to attack" and every placeholder check silently
passes on a string that has lost its button prompt. The extractor below
lowers codes back to the text the translator saw, which is also the text the
engine will substitute into.

Only 2.0 is implemented. 1.2 uses a different element vocabulary
(`trans-unit`, `source-language`) and is rejected with a message saying so,
rather than half-parsed into a Script that looks fine and is empty.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

from ..model import Node, Script, Segment

XLIFF_20 = "urn:oasis:names:tc:xliff:document:2.0"

# <note category="..."> → canonical Segment field. Studios pick their own
# category names, so the common spellings are all accepted.
NOTE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "speaker": ("speaker", "character", "char", "actor", "who"),
    "node_id": ("node", "nodeid", "scene", "sceneid", "block", "section"),
    "max_width": ("maxwidth", "max-width", "width", "charlimit", "char-limit", "limit"),
    "max_lines": ("maxlines", "max-lines", "lines", "linelimit"),
    "is_choice": ("choice", "ischoice", "is-choice", "menu"),
}
_CATEGORY_LOOKUP = {
    alias: field for field, aliases in NOTE_CATEGORIES.items() for alias in aliases
}

_TRUE = {"true", "yes", "1", "y"}


def _local(tag: str) -> str:
    """Element tag without its namespace."""
    return tag.rsplit("}", 1)[-1]


def _inline_text(element: ET.Element) -> str:
    """Flatten an element's content, lowering inline codes to their equivalents.

    `<ph equiv="{BTN_A}"/>` becomes `{BTN_A}`; a paired `<pc>` contributes its
    equivStart/equivEnd around its own content. Codes with no declared
    equivalent fall back to a visible `{id}` token, so a dropped code is still
    a dropped *something* that placeholder.parity can see, rather than
    vanishing into an empty string.
    """
    out: list[str] = [element.text or ""]
    for child in element:
        tag = _local(child.tag)
        if tag == "ph":
            out.append(child.get("equiv") or "{%s}" % (child.get("id") or "ph"))
        elif tag in ("sc", "ec"):
            equiv = child.get("equiv")
            if equiv:
                out.append(equiv)
        elif tag == "pc":
            out.append(child.get("equivStart") or "")
            out.append(_inline_text(child))
            out.append(child.get("equivEnd") or "")
        elif tag == "cp":
            hex_value = child.get("hex")
            if hex_value:
                out.append(chr(int(hex_value, 16)))
        else:
            # mrk, and anything else that wraps content rather than replacing it.
            out.append(_inline_text(child))
        out.append(child.tail or "")
    return "".join(out)


def _notes(unit: ET.Element) -> dict[str, str]:
    """Read `<notes><note category="...">` into canonical field names."""
    found: dict[str, str] = {}
    for notes in unit:
        if _local(notes.tag) != "notes":
            continue
        for note in notes:
            if _local(note.tag) != "note":
                continue
            category = (note.get("category") or "").strip().lower()
            field = _CATEGORY_LOOKUP.get(category)
            if field and note.text:
                found[field] = note.text.strip()
    return found


def _int(value: str | None, unit_id: str, field: str) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        raise ValueError(
            f"unit {unit_id!r}: note {field!r} is {value!r}, which is not a whole number."
        ) from None


def parse(path: str, **_) -> Script:
    """Read an XLIFF 2.0 file into a Script."""
    # XLIFF 2.0 is schema-validated and never needs a DTD. Refusing one
    # outright closes off entity-expansion and external-entity tricks, which
    # matters once files arrive from strangers through an upload form.
    with open(path, "rb") as fh:
        if b"<!doctype" in fh.read().lower():
            raise ValueError(
                f"{os.path.basename(path)}: contains a DOCTYPE declaration. "
                "XLIFF does not use one; remove it and re-export."
            )
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"{os.path.basename(path)}: not well-formed XML — {exc}") from None

    if _local(root.tag) != "xliff":
        raise ValueError(
            f"{os.path.basename(path)}: root element is <{_local(root.tag)}>, expected <xliff>."
        )
    version = root.get("version", "")
    if not version.startswith("2."):
        raise ValueError(
            f"{os.path.basename(path)}: XLIFF {version or '(no version)'} is not supported. "
            "Only 2.0 is implemented; export from your TMS as XLIFF 2.0."
        )

    script = Script(
        name=os.path.splitext(os.path.basename(path))[0],
        source_lang=root.get("srcLang", "en"),
        target_lang=root.get("trgLang", "ja"),
        source_format="xliff",
    )

    for unit in root.iter():
        if _local(unit.tag) != "unit":
            continue
        unit_id = unit.get("id")
        if not unit_id:
            continue

        meta = _notes(unit)
        # translate="no" on the unit is XLIFF's do-not-translate marker: the
        # string is present so the pipeline keeps its slot, not so it gets a
        # translation.
        dnt = (unit.get("translate") or "").strip().lower() == "no"

        # A unit may hold several <segment>s; they are one string to the
        # player, so they are joined rather than reported separately.
        sources: list[str] = []
        targets: list[str] = []
        for segment in unit:
            if _local(segment.tag) not in ("segment", "ignorable"):
                continue
            for part in segment:
                if _local(part.tag) == "source":
                    sources.append(_inline_text(part))
                elif _local(part.tag) == "target":
                    targets.append(_inline_text(part))

        node_id = meta.get("node_id")
        script.add_segment(
            Segment(
                id=unit_id,
                source="".join(sources),
                target="".join(targets),
                node_id=node_id,
                speaker=meta.get("speaker"),
                max_width=_int(meta.get("max_width"), unit_id, "max-width"),
                max_lines=_int(meta.get("max_lines"), unit_id, "max-lines"),
                is_choice=(meta.get("is_choice", "").lower() in _TRUE),
                do_not_translate=dnt,
                notes=meta.get("notes", ""),
                location=f"{os.path.basename(path)}:{unit_id}",
            )
        )
        if node_id and node_id not in script.nodes:
            script.add_node(Node(id=node_id, kind="line"))

    return script
