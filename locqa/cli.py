"""Command line interface.

Exit codes are the contract, and the third one is the one that matters:

    0   clean, or findings below --fail-on
    1   findings at or above --fail-on
    2   locqa itself failed

A linter that crashes and exits 0 reads as a passing build, which is worse
than having no linter, because the build is now green for a reason nobody
checked. Every unexpected exception lands on 2.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .checks.base import Config, parse_severity, registry, run_checks, select
from .model import Script, Severity
from .parsers import PARSERS, load

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

FORMATS = ("console", "json", "junit", "csv", "html")


def _load_config(path: str | None) -> Config:
    if not path:
        return Config()
    if not os.path.exists(path):
        raise SystemExit(f"locqa: config not found: {path}")
    return Config.load(path)


def _write(text: str, out: str | None) -> None:
    if out:
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    else:
        # Output piped on Windows defaults to a codepage that cannot encode
        # Japanese. Replacing the unencodable characters is better than dying
        # with exit 2 before a single finding is printed. PYTHONIOENCODING=utf-8
        # (the Makefile sets it) avoids the substitution entirely.
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            encoding = sys.stdout.encoding or "ascii"
            sys.stdout.flush()
            sys.stdout.buffer.write(text.encode(encoding, errors="replace"))
            sys.stdout.flush()


def _threshold(value: str) -> Severity | None:
    return None if value.lower() == "none" else parse_severity(value)


# -- check -------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    script = load(args.file, translations=args.translation or [])
    checks = select(only=args.only, exclude=args.exclude, tier=args.tier)
    findings = run_checks(script, config, checks)

    if args.format == "html":
        from .htmlreport import to_html

        _write(to_html(script, findings, config, checks_run=len(checks)), args.output)
    else:
        from . import report

        renderer = report.REPORTERS[args.format]
        if args.format == "console":
            text = renderer(script, findings, color=_use_color(args))
        else:
            text = renderer(script, findings)
        _write(text, args.output)

    threshold = _threshold(args.fail_on)
    if threshold is None:
        return EXIT_CLEAN
    return EXIT_FINDINGS if any(f.severity >= threshold for f in findings) else EXIT_CLEAN


def _use_color(args: argparse.Namespace) -> bool:
    if args.no_color or args.output:
        return False
    # NO_COLOR is the de facto standard; respect it before asking the terminal.
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


# -- graph -------------------------------------------------------------------


def _dot(script: Script) -> str:
    lines = ["digraph script {", "  rankdir=LR;", '  node [shape=box, fontname="Helvetica"];']
    depths = script.depths()
    for node_id, node in script.nodes.items():
        label = node_id.replace('"', '\\"')
        attrs = [f'label="{label}"']
        if node.kind == "menu":
            attrs.append("shape=diamond")
        if node_id in script.entry_nodes:
            attrs.append("style=bold")
        if node_id not in depths:
            attrs.append("color=red")           # unreachable: nothing points here
            attrs.append("fontcolor=red")
        lines.append(f'  "{node_id}" [{", ".join(attrs)}];')
    for node_id, node in script.nodes.items():
        for successor in node.successors:
            lines.append(f'  "{node_id}" -> "{successor}";')
    lines.append("}")
    return "\n".join(lines) + "\n"


def cmd_graph(args: argparse.Namespace) -> int:
    script = load(args.file, translations=args.translation or [])
    if not script.has_graph:
        print(
            f"locqa: {os.path.basename(args.file)} has no branch structure "
            "(flat formats carry no graph).",
            file=sys.stderr,
        )
        return EXIT_CLEAN

    if args.dot:
        _write(_dot(script), args.output)
        return EXIT_CLEAN

    depths = script.depths()
    predecessors = script.predecessors()
    lines = [f"{script.name} — {len(script.nodes)} nodes, entry: {', '.join(script.entry_nodes)}", ""]
    for node_id, node in script.nodes.items():
        depth = depths.get(node_id)
        mark = f"d{depth}" if depth is not None else "unreachable"
        lines.append(f"  {node_id}  [{node.kind}, {mark}]")
        if predecessors.get(node_id):
            lines.append(f"      ← {', '.join(predecessors[node_id])}")
        if node.successors:
            lines.append(f"      → {', '.join(node.successors)}")
    _write("\n".join(lines) + "\n", args.output)
    return EXIT_CLEAN


# -- pseudo ------------------------------------------------------------------


def cmd_pseudo(args: argparse.Namespace) -> int:
    import csv
    import io

    from .pseudo import pseudolocalize

    script = load(args.file)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["key", "source", "pseudo"])
    for segment in script:
        if segment.do_not_translate:
            writer.writerow([segment.id, segment.source, segment.source])
            continue
        writer.writerow(
            [segment.id, segment.source, pseudolocalize(segment.source, args.expansion)]
        )
    _write(buffer.getvalue(), args.output)
    return EXIT_CLEAN


# -- checks ------------------------------------------------------------------


def cmd_checks(args: argparse.Namespace) -> int:
    rows = sorted(registry().values(), key=lambda c: (c.tier, c.id))
    lines = []
    for tier in (1, 2):
        label = "string-level" if tier == 1 else "graph-level"
        lines.append(f"Tier {tier} — {label}")
        for check in (c for c in rows if c.tier == tier):
            flag = " (needs graph)" if check.requires_graph else ""
            lines.append(
                f"  {check.id:26} {check.default_severity.label:8} {check.title}{flag}"
            )
        lines.append("")
    lines.append(f"{len(rows)} checks.")
    _write("\n".join(lines) + "\n", args.output)
    return EXIT_CLEAN


# -- wiring ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="locqa",
        description="Localization QA for game scripts, EN → JA. Offline; no API keys.",
    )
    parser.add_argument("--version", action="version", version=f"locqa {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_input(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("file", help=f"input file ({', '.join(sorted(PARSERS))})")
        sub.add_argument(
            "-t", "--translation", action="append",
            help="translation file to join on (Ren'Py); repeatable",
        )

    check = subparsers.add_parser("check", help="run checks and report")
    add_input(check)
    check.add_argument("-c", "--config", help="project YAML")
    check.add_argument("-r", "--format", choices=FORMATS, default="console")
    check.add_argument("-o", "--output", help="write to a file instead of stdout")
    check.add_argument(
        "--fail-on", default="major", metavar="LEVEL",
        help="exit 1 at this severity or above: blocker, major, minor, info, none",
    )
    check.add_argument("--only", action="append", metavar="ID",
                       help="run only this check or namespace; repeatable")
    check.add_argument("--exclude", action="append", metavar="ID",
                       help="skip this check or namespace; repeatable")
    check.add_argument("--tier", type=int, choices=(1, 2), help="run one tier only")
    check.add_argument("--no-color", action="store_true")
    check.set_defaults(func=cmd_check)

    graph = subparsers.add_parser("graph", help="show the recovered dialogue graph")
    add_input(graph)
    graph.add_argument("--dot", action="store_true", help="emit Graphviz DOT")
    graph.add_argument("-o", "--output")
    graph.set_defaults(func=cmd_graph)

    pseudo = subparsers.add_parser("pseudo", help="write a pseudolocalized sheet")
    pseudo.add_argument("file")
    pseudo.add_argument("-o", "--output")
    pseudo.add_argument("--expansion", type=float, default=1.3,
                        help="target length as a multiple of the source (default 1.3)")
    pseudo.set_defaults(func=cmd_pseudo)

    checks = subparsers.add_parser("checks", help="list every check")
    checks.add_argument("-o", "--output")
    checks.set_defaults(func=cmd_checks)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit as exc:                   # argparse and explicit bail-outs
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return EXIT_ERROR
        return int(exc.code or 0)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"locqa: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:                    # noqa: BLE001 - see module docstring
        print(f"locqa: unexpected failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
