"""The part of a job that actually runs locqa.

Pure: bytes and config text in, a result dict out, no database. That is what
lets jobs.py run it in a separate process with a hard timeout — the child
needs nothing from the parent but its arguments, and can be killed at any
point without leaving a half-written row behind.
"""

from __future__ import annotations

import os
import re
import tempfile
import traceback

from locqa import Config, load, run_checks, select
from locqa.htmlreport import to_html
from locqa.parsers import PARSERS
from locqa.report import summarize

ALLOWED_EXTENSIONS = frozenset(PARSERS)
# Only Ren'Py pairs a script with separate translation files.
TRANSLATION_EXTENSIONS = frozenset({".rpy"})

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class InputError(Exception):
    """The user's file or config is wrong. Retrying will not help."""


def safe_filename(name: str) -> str:
    """A basename that cannot escape the temp directory or confuse a parser.

    The extension is preserved because it is how `locqa.load` picks a parser,
    and the stem is preserved (sanitized) because it becomes the report title.
    """
    base = os.path.basename(name.replace("\\", "/")) or "upload"
    stem, ext = os.path.splitext(base)
    stem = _UNSAFE.sub("_", stem).strip("._") or "upload"
    return f"{stem[:80]}{ext.lower()}"


def extension(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def validate_config(yaml_text: str, name: str = "config") -> Config:
    """Parse a config the way a run will, raising InputError on any problem."""
    try:
        return Config.from_yaml(yaml_text or "", source=name)
    except (ValueError, RuntimeError) as exc:
        raise InputError(str(exc)) from None


def analyze(
    filename: str,
    data: bytes,
    config_yaml: str,
    config_name: str = "config",
    translation_filename: str | None = None,
    translation_data: bytes | None = None,
) -> dict:
    """Run every check over one upload. Returns summary, findings, and HTML."""
    config = validate_config(config_yaml, config_name)
    with tempfile.TemporaryDirectory(prefix="locqa-") as tmp:
        path = os.path.join(tmp, safe_filename(filename))
        with open(path, "wb") as fh:
            fh.write(data)
        translations = []
        if translation_filename and translation_data is not None:
            # A distinct prefix, so a translation sharing the script's name
            # cannot overwrite it.
            tpath = os.path.join(tmp, "t_" + safe_filename(translation_filename))
            with open(tpath, "wb") as fh:
                fh.write(translation_data)
            translations.append(tpath)

        try:
            script = load(path, translations=translations)
        except UnicodeDecodeError:
            raise InputError(
                f"{safe_filename(filename)} is not UTF-8. Re-export it as UTF-8 and upload again."
            ) from None
        except (ValueError, KeyError) as exc:
            raise InputError(str(exc)) from None

        checks = select()
        findings = run_checks(script, config, checks)
        html = to_html(script, findings, config, checks_run=len(checks))

    by_id = script.segments
    return {
        "summary": {
            **summarize(findings),
            "segments": len(script),
            "format": script.source_format,
            "has_graph": script.has_graph,
            "checks_run": len(checks),
        },
        "findings": [
            {
                **f.as_dict(),
                "location": by_id[f.segment_id].location
                if f.segment_id and f.segment_id in by_id else "",
            }
            for f in findings
        ],
        "html": html,
    }


def child_result(kwargs: dict) -> tuple[str, object]:
    """("ok", result), ("input", message), or ("crash", message + traceback)."""
    try:
        return "ok", analyze(**kwargs)
    except InputError as exc:
        return "input", str(exc)
    except Exception as exc:                    # noqa: BLE001 - reported, not swallowed
        return "crash", f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}"


def main() -> None:
    """`python -m locqa_web.analysis`: pickled kwargs on stdin, pickled
    (kind, payload) on stdout. Spawned by jobs.run_isolated, nothing else."""
    import pickle
    import sys

    kwargs = pickle.loads(sys.stdin.buffer.read())
    sys.stdout.buffer.write(pickle.dumps(child_result(kwargs)))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
