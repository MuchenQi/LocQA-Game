"""Format parsers.

Each parser module exposes `parse(path, **kwargs) -> Script` and lowers its
native format into the canonical model. `load` picks one by extension; that
is the whole of the coupling between formats and checks.
"""

from __future__ import annotations

import importlib
import os

from ..model import Script

# Extension → module name in this package.
PARSERS = {
    ".xlf": "xliff",
    ".xliff": "xliff",
    ".csv": "sheet",
    ".tsv": "sheet",
    ".rpy": "renpy",
}


def load(path: str, **kwargs) -> Script:
    """Parse a file into a Script, choosing the parser by extension."""
    ext = os.path.splitext(path)[1].lower()
    module_name = PARSERS.get(ext)
    if module_name is None:
        raise ValueError(
            f"no parser for {ext or path!r} "
            f"(known extensions: {', '.join(sorted(PARSERS))})"
        )
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    module = importlib.import_module(f"{__name__}.{module_name}")
    return module.parse(path, **kwargs)
