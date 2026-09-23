"""Check modules.

Importing a check module runs its `@register` decorators, which is the only
way a check enters the registry. `load_builtins` is called lazily by
`select()` rather than at package import, so that importing `locqa` stays
cheap and a broken check module fails at the point it is needed.
"""

from __future__ import annotations

import importlib

BUILTIN_MODULES = ("integrity", "japanese", "text", "voice", "graph")


def load_builtins() -> None:
    for name in BUILTIN_MODULES:
        importlib.import_module(f"{__name__}.{name}")
