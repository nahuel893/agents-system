"""Loads `examples/demo/app.py` by file path for the test suite (ADR-004 PR5).

`examples/` ships no `__init__.py` -- design.md's D7 deliberately keeps it out
of the installed package's own import surface, matching the invocation this
change now documents (`python examples/demo/app.py`, not `python -m
examples.demo.app`). A plain `import examples.demo.app` statement is not
reliable from the test suite either (it would need the repo root itself on
`sys.path`, which pytest's default "prepend" import mode does not add), so
this loads it by path instead -- the same way the interpreter loads a script
it is handed directly.

Every test that used to `import`/`patch` `agents_system.demo` (before PR5
moved it under `examples/`) goes through the `demo_app` module object this
exposes instead, e.g. `patch.object(demo_app, "role_is_read_only", ...)`.
"""

from __future__ import annotations

import importlib.util
import pathlib
import types

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_DEMO_APP_PATH = _REPO_ROOT / "examples" / "demo" / "app.py"


def _load() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("examples_demo_app", _DEMO_APP_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load the demo entrypoint from {_DEMO_APP_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo_app = _load()
