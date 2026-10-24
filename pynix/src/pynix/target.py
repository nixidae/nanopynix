"""Shared command-line evaluation target handling."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import anyio.to_thread
import huggorm
from anyio import Path as AsyncPath

_FLAKE_PREFIX = "flake:"


class EvaluationTargetError(RuntimeError):
    """An evaluation target or attribute selection is invalid."""


@dataclass(frozen=True)
class FileReference:
    """A resolved ``--file`` argument."""

    arguments: tuple[str, ...]
    """What the evaluator receives, best first, with no fragment.

    Almost every shape resolves to one string. A bare name gives two, because
    such a name reaches the lookup path and the flake registry both, and the
    lookup path answers first.
    """

    fragment: str | None
    """The attribute path after the first ``#``, or ``None``."""

    local_path: Path | None
    """The local file or directory, when the argument names one that exists."""

    expression: str | None = None
    """The Nix expression that ``--file -`` read from standard input, or ``None``.

    ``arguments`` is empty then. The expression names a relative path from the
    working directory, as ``nix --file -`` has it.
    """


_STDIN = "-"

#: What standard input held, and the stream it came from. A command resolves
#: ``--file`` more than once -- ``build`` asks for the local file before it
#: evaluates -- and a second read of a pipe finds it empty.
_stdin_text: tuple[TextIO, str] | None = None


async def _read_stdin() -> str:
    """Read standard input to its end once, and give each later caller the same text.

    Keyed on the stream, so a caller that replaces ``sys.stdin`` reads afresh.
    """
    global _stdin_text  # noqa: PLW0603 -- one standard input per process, like the stream it caches
    if _stdin_text is not None and _stdin_text[0] is sys.stdin:
        return _stdin_text[1]
    stream = sys.stdin
    text: str = await anyio.to_thread.run_sync(stream.read)
    _stdin_text = (stream, text)
    return text


def names_stdin(raw: str | None) -> bool:
    """Report whether a ``--file`` argument is ``-``, with or without a fragment."""
    return raw is not None and raw.partition("#")[0] == _STDIN


async def _existing_local_path(candidate: str) -> Path | None:
    """Return the local path that *candidate* names, or ``None``."""
    if not candidate:
        return None
    try:
        if await AsyncPath(candidate).exists():
            return Path(candidate)
    except OSError:
        # A name that the file system refuses -- too long, or an embedded NUL.
        # Such a name is not a local path, which is the whole question here.
        return None
    return None


async def resolve_file_reference(raw: str) -> FileReference:  # noqa: PLR0911 -- one return per rule the docstring lists
    """Resolve a ``--file`` argument into what the evaluator understands.

    The evaluator resolves four shapes itself, in ``lookup_file_arg``: a
    pseudo-URL, the ``flake:`` prefix, a ``<name>`` lookup path, and an
    ordinary path. This function adds two things on top of them, and it
    changes none of the four.

    The first addition is the fragment. ``--flake`` splits on ``#`` already,
    and ``--file`` did not, so an attribute path had to go in ``--attr``.

    The second is a bare name. ``github:NixOS/nixpkgs`` reaches the evaluator
    as ``flake:`` plus the reference, which is the branch that fetches the
    tree. The file inside that tree is then evaluated as an ordinary Nix file.
    This does not read ``flake.nix``, and it is not ``--flake``.

    ``-`` comes first, and it is not a path: the expression on standard input,
    as ``nix --file -`` reads it, with a fragment after it as for a file.

    Seven rules apply to the rest, in this order:

    1. the whole argument names a local path that exists;
    2. the part before the first ``#`` names a local path that exists;
    3. that part starts with ``.``, ``/`` or ``~``, so it is a path that is
       absent;
    4. it is a ``<name>`` lookup path already;
    5. Nix fetches it already, which is a pseudo-URL or the ``flake:`` prefix;
    6. it names a scheme, such as ``github:``, so it is a flake reference;
    7. anything else is a bare name, and it gives two candidates:
       ``<name>`` first, and ``flake:name`` second.

    Rule 1 and rule 2 are why ``-f nixpkgs`` still reads a directory named
    ``nixpkgs`` in the working directory. Rule 3 is why an absent
    ``./default.nix`` reports a missing file, and not a missing flake. Write
    a relative path with ``./``, because rule 7 reads a bare word as a name.

    **Rule 7 asks the lookup path before it asks the registry, because
    ``--file`` is the old-style door.** ``NIX_PATH`` is how a name became a
    tree before flakes existed, so ``-f nixpkgs#hello`` is
    ``nix-build '<nixpkgs>' -A hello`` and reaches the same tree. The registry
    answers only when the lookup path holds no such name. See
    :func:`pynix._evaluate.open_file_reference`, which decides that from what the evaluator
    reports rather than from a copy of the rules of ``EvalState::findFile``.
    """
    if not raw:
        raise EvaluationTargetError("--file needs a value")
    if names_stdin(raw):
        expression = await _read_stdin()
        if not expression.strip():
            raise EvaluationTargetError("--file - read no expression from standard input")
        _, _, fragment = raw.partition("#")
        return FileReference(arguments=(), fragment=fragment or None, local_path=None, expression=expression)

    # A path that exists exactly as written wins, '#' and all. A file name that
    # holds a '#' is rare, and the caller who has one wrote it deliberately.
    whole = await _existing_local_path(raw)
    if whole is not None:
        return FileReference(arguments=(raw,), fragment=None, local_path=whole)

    head, _, tail = raw.partition("#")
    fragment = tail or None
    if not head:
        raise EvaluationTargetError(f"--file {raw!r} names no reference before the '#'")

    local = await _existing_local_path(head)
    if local is not None:
        return FileReference(arguments=(head,), fragment=fragment, local_path=local)

    # A path that the caller wrote as a path stays a path, even when the file
    # is absent. Otherwise a typed `-f ./buidl.nix` would reach the registry,
    # and the caller would read an error about a flake instead of the missing
    # file. `local_path` stays None, because nothing is there to rewrite.
    if head.startswith(("./", "../", "/", "~/")) or head in {".", "..", "~"}:
        return FileReference(arguments=(head,), fragment=fragment, local_path=None)

    # Ask Nix which strings it fetches itself. Repeating the list of schemes
    # here would make this file disagree with the evaluator as soon as Nix adds
    # one, and the disagreement would show as a fetch of 'flake:https://...'.
    if huggorm.is_pseudo_url(head) or head.startswith(_FLAKE_PREFIX) or _is_lookup_path(head):
        return FileReference(arguments=(head,), fragment=fragment, local_path=None)

    # A reference that names its scheme goes to the registry alone. `github:` is
    # a flake reference and nothing else, so a lookup path named 'github:NixOS'
    # would be an accident. A name with no ':' is the ambiguous one.
    if ":" in head:
        return FileReference(arguments=(f"{_FLAKE_PREFIX}{head}",), fragment=fragment, local_path=None)

    return FileReference(
        arguments=(f"<{head}>", f"{_FLAKE_PREFIX}{head}"),
        fragment=fragment,
        local_path=None,
    )


def _is_lookup_path(candidate: str) -> bool:
    """Report whether *candidate* is a ``<name>`` lookup path."""
    return len(candidate) > 2 and candidate.startswith("<") and candidate.endswith(">")  # noqa: PLR2004 -- '<' and '>' plus one character, which is the rule in lookup_file_arg


@dataclass(frozen=True)
class EvaluationTarget:
    """A file or flake evaluation source with an optional attribute selector."""

    file: str | None
    attr: str | None
    flake: str | None

    @classmethod
    def from_command(cls, command: Any) -> EvaluationTarget:
        """Construct a target from a command declaring the common options."""
        return cls(file=command.file, attr=command.attr, flake=command.flake)

    async def file_reference(self) -> FileReference | None:
        """Resolve ``--file``, or return ``None`` when there is no ``--file``."""
        if self.file is None:
            return None
        return await resolve_file_reference(self.file)

    def selected_attr(self) -> str | None:
        """The attribute path the caller named, or ``None`` when it named none.

        The path can arrive in two places at once: `--file dir#hetztop --attr
        config` names `hetztop.config`. A fragment of `--flake` reaches this
        the same way, because both options carry it after a `#`.
        """
        source = self.file or self.flake or ""
        _, _, fragment = source.partition("#")
        parts = [part for part in (fragment, self.attr) if part]
        return ".".join(parts) or None

    def validate(self, *, required: bool = False) -> None:
        """Validate mutually exclusive sources and attribute selection."""
        if self.file is not None and self.flake is not None:
            raise EvaluationTargetError("--file and --flake are mutually exclusive")
        if required and self.file is None and self.flake is None:
            raise EvaluationTargetError("either --file or --flake is required")
        if self.attr is not None and self.file is None and self.flake is None:
            raise EvaluationTargetError("--attr requires --file or --flake")


#: What the module system stamps on the result of `nixosSystem` and of
#: `homeManagerConfiguration`, and the attribute of each that holds the
#: derivation. home-manager sets its `class` in `modules/default.nix`.
#:
#: **Read `class`, and do not guess from the shape.** A set that holds `config`
#: and `options` is any evaluation of the module system, and there are more of
#: those than these two. A `class` that is not here gets the message that the
#: caller already had.
CONFIGURATION_KINDS: dict[str, tuple[str, str]] = {
    "nixos": ("NixOS", "config.system.build.toplevel"),
    "homeManager": ("home-manager", "config.home.activationPackage"),
}


def configuration_message(kind: tuple[str, str], selected: str | None) -> str:
    """What to tell a caller who selected a configuration and asked for a build."""
    name, path = kind
    subject = selected or "the selected value"
    attribute = f"{selected}.{path}" if selected else path
    return f"{subject} is a {name} configuration, not a derivation.\nTry --attr {attribute}"
