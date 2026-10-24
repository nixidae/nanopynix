"""``pynix._impl`` imports every implementation module that is there."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path


def test_the_impl_package_imports_every_module_that_is_there() -> None:
    """A subcommand reads its implementation off ``pynix._impl``, so a module
    the package does not import raises ``AttributeError`` the first time that
    command runs, and no import-time check sees it. Leaving ``main`` out cost
    every ``pynix-lsp`` end-to-end test a ``Server process exited with return
    code: 1``, and every other suite stayed green.

    A module whose name starts with an underscore is a library that another
    module of ``_impl`` imports by its own name, so the package leaves it out.
    """
    package = importlib.import_module("pynix._impl")
    path = Path(str(package.__file__))
    on_disk = {
        file.stem for file in path.parent.glob("*.py") if file.stem != "__init__" and not file.stem.startswith("_")
    }
    assert on_disk, "no implementation modules found; the glob is wrong"

    imported = {
        alias.asname or alias.name
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.ImportFrom) and node.module == "pynix._impl"
        for alias in node.names
    }
    assert imported == on_disk, (
        "pynix/_impl/__init__.py and the files in that directory disagree. "
        f"only on disk: {sorted(on_disk - imported)}; only imported: {sorted(imported - on_disk)}"
    )
