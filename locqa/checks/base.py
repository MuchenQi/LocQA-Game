"""Check protocol, registry, configuration, and the runner.

A check is a class with an id, a declared default severity, and a `run` that
yields Findings. It reads the canonical model and nothing else, so the same
check fires on XLIFF, a loc sheet, and a Ren'Py script without knowing which
it was handed.

The runner owns three things the checks deliberately do not: reach weighting,
duplicate collapsing, and sort order. Keeping those central is what makes a
report's counts trustworthy — a check cannot inflate its own priority, and an
exact repeat is reported once. Deciding that two *different* findings are one
bug needs knowledge only the check has, so that part lives in the checks.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..model import Finding, Script, Segment, Severity

# -- configuration -----------------------------------------------------------

_SEVERITY_BY_NAME = {s.name.lower(): s for s in Severity}


def parse_severity(value: object) -> Severity:
    """Accept `BLOCKER`, `blocker`, `3`, or a Severity. Reject anything else.

    Config typos must fail loudly: a misspelled severity that silently falls
    back to a default is how a blocker gets downgraded without anyone noticing.
    """
    if isinstance(value, Severity):
        return value
    if isinstance(value, bool):                     # bool is an int; not a severity
        raise ValueError(f"not a severity: {value!r}")
    if isinstance(value, int):
        return Severity(value)
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _SEVERITY_BY_NAME:
            return _SEVERITY_BY_NAME[key]
    raise ValueError(
        f"not a severity: {value!r} (expected one of "
        f"{', '.join(s.name for s in Severity)})"
    )


@dataclass
class Config:
    """Project settings. Every field has a working default.

    `Config()` with no arguments is a valid strict-ish profile, so a user can
    point the tool at a file before writing any configuration at all.
    """

    # Target-language surface
    charset: str = "shift_jis"                  # font atlas repertoire; cp932 for vendor extensions
    ambiguous_wide: bool = True                 # East Asian Ambiguous renders wide in a JA font

    # Length budgets
    default_max_width: int | None = None        # applied where a segment declares none
    default_placeholder_width: int = 8
    placeholder_widths: dict[str, int] = field(default_factory=dict)

    # Terminology
    glossary: dict[str, list[str]] = field(default_factory=dict)
    forbidden: dict[str, str] = field(default_factory=dict)
    do_not_translate: list[str] = field(default_factory=list)

    # Check behaviour
    enforce_register: bool = True
    severity_overrides: dict[str, Severity] = field(default_factory=dict)

    # Triage
    reach_weighting: bool = True
    min_reach: float = 0.45

    path: str = ""                              # where this was loaded from, for reports

    def __post_init__(self) -> None:
        # An override naming a check that does not exist is a typo, and a typo
        # here is the silent-downgrade failure parse_severity exists to stop:
        # `placeholder.parit: info` would leave the real check at BLOCKER while
        # its author believes it was quietened, or the reverse. Checks must
        # therefore be registered before a Config that overrides them is built.
        known = registry() if self.severity_overrides else {}
        unknown = sorted(k for k in self.severity_overrides if k not in known)
        if unknown:
            hints = []
            for key in unknown:
                close = difflib.get_close_matches(key, known, n=1)
                hints.append(f"{key!r}" + (f" (did you mean {close[0]!r}?)" if close else ""))
            raise ValueError(
                f"severity_overrides names unknown check(s): {', '.join(hints)}. "
                "Run `locqa checks` for the list."
            )
        self.severity_overrides = {
            k: parse_severity(v) for k, v in self.severity_overrides.items()
        }
        if not 0.0 < self.min_reach <= 1.0:
            raise ValueError(f"min_reach must be in (0, 1]: {self.min_reach}")

    def severity_for(self, check_id: str, default: Severity) -> Severity:
        """The configured severity for a check, or the caller's default.

        An override wins over a per-finding severity too. A check raising one
        finding to MAJOR is its own judgement about that hit; an override is
        the project saying it does not want to hear about this check at that
        level, and the project gets the last word.
        """
        return self.severity_overrides.get(check_id, default)

    @classmethod
    def load(cls, path: str) -> Config:
        """Read a YAML config. Unknown keys are an error, not a warning.

        A typo'd key in a linter config is silent failure by definition — the
        rule the user thought they configured simply never fires.
        """
        try:
            import yaml
        except ModuleNotFoundError as exc:       # pragma: no cover - env-dependent
            raise RuntimeError(
                "Reading a config file needs PyYAML: pip install locqa"
            ) from exc

        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: expected a mapping at the top level")

        known = {f.name for f in cls.__dataclass_fields__.values()} - {"path"}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(
                f"{path}: unknown config key(s): {', '.join(unknown)}. "
                f"Known keys: {', '.join(sorted(known))}"
            )
        try:
            return cls(path=os.path.abspath(path), **raw)
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from None


# -- the check protocol ------------------------------------------------------


class Check:
    """Base class for all checks.

    Subclasses set the class attributes and implement `run`. They yield
    findings rather than returning a list so that a check over a large script
    reports its first hit without walking the whole file first.
    """

    id: str = ""
    tier: int = 1                               # 1 string-level, 2 graph-level
    title: str = ""
    description: str = ""
    default_severity: Severity = Severity.MINOR
    requires_graph: bool = False                # skipped, not failed, on flat input

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        raise NotImplementedError

    def finding(
        self,
        config: Config,
        message: str,
        *,
        segment: Segment | None = None,
        node: str | None = None,
        excerpt: str = "",
        suggestion: str = "",
        severity: Severity | None = None,
        span: tuple[int, int] | None = None,
        meta: dict | None = None,
    ) -> Finding:
        """Build a Finding with the check's id and the configured severity."""
        return Finding(
            check_id=self.id,
            severity=config.severity_for(
                self.id, severity if severity is not None else self.default_severity
            ),
            message=message,
            segment_id=segment.id if segment else None,
            node_id=node or (segment.node_id if segment else None),
            excerpt=excerpt,
            suggestion=suggestion,
            span=span,
            meta=meta or {},
        )

    def __repr__(self) -> str:                  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.id}>"


# -- registry ----------------------------------------------------------------

_REGISTRY: dict[str, type[Check]] = {}


def register(cls: type[Check]) -> type[Check]:
    """Class decorator. Registers a check under its id."""
    if not cls.id:
        raise ValueError(f"{cls.__name__} has no id")
    existing = _REGISTRY.get(cls.id)
    if existing is not None and existing is not cls:
        raise ValueError(
            f"duplicate check id {cls.id!r}: {existing.__name__} and {cls.__name__}"
        )
    _REGISTRY[cls.id] = cls
    return cls


def registry() -> dict[str, type[Check]]:
    """Every registered check class, keyed by id."""
    _load_builtins()
    return dict(_REGISTRY)


_LOADED = False


def _load_builtins() -> None:
    """Import the built-in check modules so their decorators run."""
    global _LOADED
    if _LOADED:
        return
    from . import load_builtins

    load_builtins()
    _LOADED = True


def select(
    only: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    tier: int | None = None,
) -> list[Check]:
    """Instantiate the selected checks, in a stable order.

    Selectors match either a full id (`ja.kinsoku`) or a namespace (`ja`,
    which takes every `ja.*`). An unmatched selector raises: a typo in
    `--only` that silently runs nothing would report a clean build.
    """
    _load_builtins()
    chosen = dict(_REGISTRY)

    if only:
        chosen = {cid: cls for cid, cls in chosen.items() if _matches(cid, only)}
        for selector in only:
            if not any(_matches(cid, [selector]) for cid in _REGISTRY):
                raise ValueError(f"no check matches {selector!r}")
    if exclude:
        chosen = {cid: cls for cid, cls in chosen.items() if not _matches(cid, exclude)}
    if tier is not None:
        chosen = {cid: cls for cid, cls in chosen.items() if cls.tier == tier}

    return [cls() for _, cls in sorted(chosen.items(), key=lambda kv: (kv[1].tier, kv[0]))]


def _matches(check_id: str, selectors: Sequence[str]) -> bool:
    return any(
        check_id == s or check_id.startswith(s.rstrip(".") + ".") for s in selectors
    )


# -- the runner --------------------------------------------------------------


def run_checks(
    script: Script,
    config: Config | None = None,
    checks: Sequence[Check] | None = None,
) -> list[Finding]:
    """Run checks over a script and return findings, highest priority first."""
    config = config or Config()
    checks = list(checks) if checks is not None else select()

    has_graph = script.has_graph
    findings: list[Finding] = []
    for check in checks:
        if check.requires_graph and not has_graph:
            continue                            # nothing to say, so say nothing
        findings.extend(check.run(script, config))

    _resolve_nodes(script, findings)
    if config.reach_weighting:
        _apply_reach(script, findings, config.min_reach)
    findings = _collapse(findings)
    findings.sort(
        key=lambda f: (-f.priority, f.check_id, f.segment_id or "", f.node_id or "")
    )
    return findings


def _resolve_nodes(script: Script, findings: list[Finding]) -> None:
    """Fill in node_id from the segment, so reach weighting can find a depth."""
    for f in findings:
        if f.node_id is None and f.segment_id:
            seg = script.segments.get(f.segment_id)
            if seg:
                f.node_id = seg.node_id


def _apply_reach(script: Script, findings: list[Finding], min_reach: float) -> None:
    """Scale each finding's sort priority by how early its node is reachable.

    Depth is a triage hint, never a correctness claim, so this touches `reach`
    and never `severity`, and it never drops below `min_reach`. A blocker
    buried twelve branches deep is still a blocker; it just sorts under the one
    on the opening screen.
    """
    if not script.has_graph:
        return
    depths = script.depths()
    if not depths:
        return
    deepest = max(depths.values())
    if deepest <= 0:
        return
    span = 1.0 - min_reach
    for f in findings:
        if f.node_id is None:
            continue
        depth = depths.get(f.node_id)
        if depth is None:
            f.reach = min_reach                 # unreachable: real, but last in line
            continue
        f.reach = 1.0 - span * (depth / deepest)


def _collapse(findings: list[Finding]) -> list[Finding]:
    """Drop exact repeats, keeping the first.

    Two checks describing the same defect on the same string is one bug
    counted twice, and doubled counts are how a QA report loses a reviewer's
    trust. Subsumption between *different* messages belongs to the check that
    knows the relationship — see `length.budget`, which reports the overflow
    rather than the overflow and the line count.
    """
    seen: set[tuple] = set()
    out: list[Finding] = []
    for f in findings:
        key = (f.check_id, f.segment_id, f.node_id, f.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out
