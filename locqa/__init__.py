"""locqa — localization QA for game scripts, EN → JA.

The public surface is small on purpose:

    from locqa import load, run_checks, Config
    script = load("dialogue.rpy", translations=["dialogue_ja.rpy"])
    findings = run_checks(script, Config.load("project.yml"))
"""

from __future__ import annotations

from .checks.base import (
    Check,
    Config,
    parse_severity,
    register,
    registry,
    run_checks,
    select,
)
from .model import (
    Finding,
    Node,
    Script,
    Segment,
    Severity,
    display_width,
    wrap_by_width,
)
from .parsers import load

__version__ = "0.1.0"

__all__ = [
    "Check",
    "Config",
    "Finding",
    "Node",
    "Script",
    "Segment",
    "Severity",
    "display_width",
    "load",
    "parse_severity",
    "register",
    "registry",
    "run_checks",
    "select",
    "wrap_by_width",
]
