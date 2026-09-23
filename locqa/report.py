"""Reporters: console, JSON, JUnit, CSV.

Four audiences, one finding list. The console is for a person reading a
terminal, JSON for anything downstream, JUnit for a CI dashboard that already
knows how to display test failures, and CSV for the reviewer who is going to
open it in a spreadsheet next to the loc sheet itself.

Counts are computed once and shared, so no two reporters can disagree about
how many blockers a build has.
"""

from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
from collections import Counter

from .model import Finding, Script, Severity

# ANSI, kept local so nothing else in the package knows about terminals.
_COLOR = {
    Severity.BLOCKER: "\033[31;1m",
    Severity.MAJOR: "\033[33m",
    Severity.MINOR: "\033[36m",
    Severity.INFO: "\033[2m",
}
_DIM = "\033[2m"
_RESET = "\033[0m"


def summarize(findings: list[Finding]) -> dict:
    """Counts by severity and by check, for any reporter that wants a header."""
    by_severity = Counter(f.severity.label for f in findings)
    return {
        "total": len(findings),
        "by_severity": {
            s.label: by_severity.get(s.label, 0) for s in reversed(list(Severity))
        },
        "by_check": dict(Counter(f.check_id for f in findings).most_common()),
    }


def _location(script: Script, finding: Finding) -> str:
    if finding.segment_id and finding.segment_id in script.segments:
        return script.segments[finding.segment_id].location
    return ""


def _header(script: Script) -> str:
    bits = [script.source_format or "script", f"{script.source_lang}→{script.target_lang}"]
    return f"{script.name} — {', '.join(bits)}, {len(script)} segments"


# -- console -----------------------------------------------------------------


def console(script: Script, findings: list[Finding], color: bool = True) -> str:
    """Human-readable report for a terminal."""

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    lines = [_header(script), ""]

    if not findings:
        lines.append(paint("  No findings.", "\033[32m" if color else ""))
        lines.append("")
        return "\n".join(lines)

    for finding in findings:
        severity = paint(f"{finding.severity.label.upper():8}", _COLOR[finding.severity])
        where = finding.segment_id or finding.node_id or "-"
        lines.append(f"  {severity} {finding.check_id}  {paint(where, _DIM)}")
        lines.append(f"           {finding.message}")
        if finding.excerpt:
            lines.append(f"           {paint('« ' + finding.excerpt + ' »', _DIM)}")
        if finding.suggestion:
            lines.append(f"           {paint('→ ' + finding.suggestion, _DIM)}")
        location = _location(script, finding)
        if location:
            lines.append(f"           {paint(location, _DIM)}")
        lines.append("")

    counts = summarize(findings)["by_severity"]
    tally = ", ".join(f"{n} {name.lower()}" for name, n in counts.items() if n)
    lines.append(f"  {len(findings)} findings: {tally}")
    lines.append("")
    return "\n".join(lines)


# -- JSON --------------------------------------------------------------------


def to_json(script: Script, findings: list[Finding], indent: int = 2) -> str:
    """Machine-readable report. The stable contract for anything downstream."""
    payload = {
        "script": {
            "name": script.name,
            "format": script.source_format,
            "source_lang": script.source_lang,
            "target_lang": script.target_lang,
            "segments": len(script),
            "has_graph": script.has_graph,
        },
        "summary": summarize(findings),
        "findings": [
            {**finding.as_dict(), "location": _location(script, finding)}
            for finding in findings
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=indent)


# -- JUnit -------------------------------------------------------------------


def to_junit(script: Script, findings: list[Finding]) -> str:
    """JUnit XML, so a CI dashboard renders findings as test failures.

    One suite per check and one case per finding. A check that fired is a
    suite with failures; the suite name is the check id, so a dashboard's
    history shows which rule regressed rather than only that something did.
    """
    root = ET.Element(
        "testsuites",
        name=f"locqa:{script.name}",
        tests=str(len(findings)),
        failures=str(len(findings)),
    )

    by_check: dict[str, list[Finding]] = {}
    for finding in findings:
        by_check.setdefault(finding.check_id, []).append(finding)

    for check_id, hits in by_check.items():
        suite = ET.SubElement(
            root,
            "testsuite",
            name=check_id,
            tests=str(len(hits)),
            failures=str(len(hits)),
        )
        for finding in hits:
            case = ET.SubElement(
                suite,
                "testcase",
                name=finding.segment_id or finding.node_id or check_id,
                classname=f"{script.name}.{check_id}",
            )
            failure = ET.SubElement(
                case,
                "failure",
                message=finding.message,
                type=finding.severity.label,
            )
            detail = [finding.message]
            if finding.excerpt:
                detail.append(f"target: {finding.excerpt}")
            if finding.suggestion:
                detail.append(f"fix: {finding.suggestion}")
            location = _location(script, finding)
            if location:
                detail.append(f"at: {location}")
            failure.text = "\n".join(detail)

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


# -- CSV ---------------------------------------------------------------------

CSV_FIELDS = [
    "severity", "priority", "check", "segment", "node", "location",
    "message", "source", "target", "suggestion", "reach",
]


def to_csv(script: Script, findings: list[Finding]) -> str:
    """Flat rows, for opening beside the loc sheet the strings came from."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for finding in findings:
        segment = script.segments.get(finding.segment_id or "")
        writer.writerow(
            {
                "severity": finding.severity.label,
                "priority": round(finding.priority, 3),
                "check": finding.check_id,
                "segment": finding.segment_id or "",
                "node": finding.node_id or "",
                "location": _location(script, finding),
                "message": finding.message,
                "source": segment.source if segment else "",
                "target": segment.target if segment else "",
                "suggestion": finding.suggestion,
                "reach": round(finding.reach, 3),
            }
        )
    return buffer.getvalue()


REPORTERS = {
    "console": console,
    "json": to_json,
    "junit": to_junit,
    "csv": to_csv,
}
