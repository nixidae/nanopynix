from __future__ import annotations

import sys
from pathlib import Path

from sphinx.application import Sphinx

_DOCS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_DOCS_DIR))
sys.path.insert(0, str(_DOCS_DIR.parent / "pynix" / "src"))
sys.path.insert(0, str(_DOCS_DIR.parent / "nix" / "shared"))

from _generate_pynix_reference import generate as _generate_pynix_reference  # noqa: E402, I001 -- sys.path must be extended first, and this import cannot be merged into the block above it
from sphinx_shared import (  # noqa: E402 -- sys.path must be extended first
    author as author,
    autodoc_default_options as autodoc_default_options,
    autodoc_member_order as autodoc_member_order,
    autodoc_typehints as autodoc_typehints,
    exclude_patterns as exclude_patterns,
    extensions as extensions,
    html_theme as html_theme,
    intersphinx_mapping as intersphinx_mapping,
    myst_enable_extensions as myst_enable_extensions,
    napoleon_google_docstring as napoleon_google_docstring,
    napoleon_numpy_docstring as napoleon_numpy_docstring,
    napoleon_use_rtype as napoleon_use_rtype,
    smartquotes as smartquotes,
    templates_path as templates_path,
)

project = "nanopynix"
copyright = "2025, Carl Andersson"  # noqa: A001 -- Sphinx conf variable, not the Python builtin


def _on_builder_inited(app: Sphinx) -> None:
    del app
    _generate_pynix_reference()


def setup(app: Sphinx) -> None:
    app.connect("builder-inited", _on_builder_inited)
