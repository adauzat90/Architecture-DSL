"""The diagnostic types live in :mod:`barndsl.issues` (TD-4).

A module that reports or reads diagnostics imports ``Issue``/``Severity`` from
there instead of loading the whole validator for two types. The wall helpers
``fixtures`` needs (``exterior_walls``, ``clear_box``) live in ``geometry``, so
``fixtures`` sits below ``validation`` instead of importing it back.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

import barndsl
from barndsl import geometry, issues, validation

SRC = Path(barndsl.__file__).parent


def test_the_types_load_without_the_rest_of_barndsl(monkeypatch):
    # issues.py has no barndsl imports, so it loads as a lone file.
    spec = importlib.util.spec_from_file_location("lone_issues", SRC / "issues.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "lone_issues", mod)
    spec.loader.exec_module(mod)
    issue = mod.Issue(mod.Severity.WARNING, "X_CODE", "msg", room="a", line=3, hint="fix it")
    report = mod.ValidationReport([issue])
    assert report.summary() == "VALID — 0 error(s), 1 warning(s), 0 info(s)"
    assert str(issue) == "line 3: warning[X_CODE] (a): msg\n    hint: fix it"


@pytest.mark.parametrize("name", ["Issue", "Severity", "ValidationReport"])
def test_the_old_import_paths_still_give_the_same_types(name):
    assert getattr(validation, name) is getattr(issues, name) is getattr(barndsl, name)


@pytest.mark.parametrize("name", ["exterior_walls", "clear_dimensions", "clear_box"])
def test_validation_still_exposes_the_wall_helpers(name):
    assert getattr(validation, name) is getattr(geometry, name)


MODULES = {p.stem for p in SRC.glob("*.py")}


def _imports(module: str) -> set[str]:
    """The barndsl modules ``module`` imports, in any form and anywhere in it
    (inside functions too). A name taken from the package itself counts as
    ``__init__``, which imports everything."""
    tree = ast.parse((SRC / f"{module}.py").read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if name.startswith("barndsl."):
                out.add(name.split(".")[1])
            elif node.level and name:
                out.add(name.split(".")[0])
            elif node.level or name == "barndsl":  # from . import x / from barndsl import x
                out.update(a.name if a.name in MODULES else "__init__" for a in node.names)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "barndsl":
                    out.add("__init__")
                elif a.name.startswith("barndsl."):
                    out.add(a.name.split(".")[1])
    return out


def _reachable(module: str) -> set[str]:
    seen: set[str] = set()
    todo = [module]
    while todo:
        m = todo.pop()
        if m not in seen and m in MODULES:
            seen.add(m)
            todo.extend(_imports(m))
    return seen


def test_issues_imports_nothing_from_barndsl():
    assert _imports("issues") == set()


@pytest.mark.parametrize("module", ["issues", "geometry", "fixtures"])
def test_modules_below_the_validator_never_reach_it(module):
    # Not directly, not inside a function (how the old cycle hid), and not
    # through another module.
    assert "validation" not in _reachable(module)
