"""The Nix language server, and the dialects it understands.

Issue #107 moved this package out of ``pynix``, and ``pynix_lsp.cli`` holds the
one name it runs under: the ``pynix-lsp`` program. Issue #123 removed the
``pynix lsp`` alias.
"""

from __future__ import annotations

import pynix  # noqa: F401  # pyright: ignore[reportUnusedImport] -- pynix installs deferimport, and this package's modules load after it
