"""The one decorator this package needs from the beartype plumbing.

Here and not in a project that names Nix, so that a program can take this
package without ``huggorm-bindings``, a compiled extension linked against one
Nix version. See the note above ``dependencies`` in ``pyproject.toml``.

``pynix/tests/test_beartype_instrumentation.py`` shows that the suites run
under beartype; the attribute this decorator sets is beartype's own.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def no_runtime_type_check[F: Callable[..., Any]](func: F) -> F:
    """Exempt ``func`` from beartype's runtime checks, keeping the static ones.

    ``typing.no_type_check`` sets the same attribute and also carries a typing
    meaning: pyright erases the signature and stops reading the body. This
    decorator is an identity function to a type checker, so the annotations
    and the body stay checked.

    Every declaration helper here needs it. ``file_option()`` says it returns
    ``str | None``, because that is the type of the attribute the declaration
    becomes, and it returns a :class:`~libpynix.command.Spec`. beartype would
    report each call as a violation of an annotation that is doing its job.
    """
    # `__no_type_check__` is the attribute `typing.no_type_check` sets and the
    # one `beartype` looks for; setting it directly is the whole mechanism.
    func.__no_type_check__ = True  # type: ignore[attr-defined]  # functions accept arbitrary attributes; typeshed does not declare this one
    return func
