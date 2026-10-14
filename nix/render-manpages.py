"""Write the manual pages of ``pynix``.

``nix/mk-app.nix`` runs this against the venv of an application, and
``installManPage`` puts what it writes under ``share/man/``. A shell finds
the pages when the package is installed, and so does ``whatis``. Issue #233:
fish describes a command name through ``whatis``, and no package here
installed a page, so ``pynix`` completed as a bare ``command``.

**Most of each page comes from argparse, through public API.**
``argparse_manpage.manpage.Manpage`` renders one parser per page: the root
page plus one page per top-level subcommand, the way ``nix`` ships one page
per subcommand. A nested subcommand stays a section of its parent page. The
hand-written text is the docstring of each command, which is the same string
its ``--help`` shows, plus the EXAMPLES section of the root page.

**The EXAMPLES section lives beside the project, in ``pynix/man/``.**
``${./render-manpages.py}`` reaches the store as one file, so a path beside
this script would not resolve. ``mk-app.nix`` passes the snippet as the
second argument instead.

**The lazy subparsers need one pre-pass.** ``libpynix`` builds each child
parser when something looks it up, and ``Manpage`` reads ``choices``
directly, where a parser that nothing built yet is still ``None``.
``_materialize`` builds every parser first, so the renderer never sees a
placeholder.

**The output is plain roff, whatever the builder detects.** Python 3.14 themes
the usage strings of argparse with ANSI escapes when its color detection
passes, and argparse-manpage 4.7 does not switch them off. troff cannot use
them, and a builder that detects color writes different bytes than one that
does not. Measured: the same renderer wrote colored pages under `nix build`
and plain pages in a shell. `_sanitize` strips every escape sequence, and
guards the text shape that wrapping produces out of a quoted word: a line
that starts with an apostrophe.

**``argparse-manpage`` rides on ``PYTHONPATH`` and stays out of the venv.**
It is a build-time input only. The closure of the application does not grow,
and ``libpynix`` keeps its one dependency.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import pathlib
import re
import sys
from typing import TYPE_CHECKING

# pyright: reportMissingImports=false
# argparse-manpage is a build-time input on PYTHONPATH, set by nix/mk-app.nix.
# It is in no venv, so neither pyright's pythonEnv nor the dev shell resolves
# it. The build fails loudly without it, which is the check that matters.
# pyright: reportUnknownVariableType=false
# argparse-manpage ships no type information, so the `Manpage` import binding
# is unknown. Scoped to this file, which is build glue and imports nothing
# else untyped.
from argparse_manpage.manpage import Manpage

from libpynix import build_parser
from pynix import Pynix

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

#: The NAME text of the root page. The parser's own description is the group
#: help, "pynix — nanopynix CLI", which would read "pynix - pynix — ...". One
#: line, lifted from the first paragraph of docs/pynix/index.md.
_ROOT_DESCRIPTION = "command-line tool for Nix"

#: The footer of every page names the project and where it lives. The
#: repository names no author in any pyproject.toml, so the AUTHOR section
#: stays absent rather than invented.
_PROJECT_URL = "https://github.com/nixidae/nanopynix"

#: The title of the manual that the footer of every page names.
_MANUAL_TITLE = "pynix Manual"

#: The hand-written SEE ALSO section of the root page. Short and stable page
#: furniture, so it lives here. The EXAMPLES section is longer command prose,
#: so it lives in pynix/man/ instead.
_SEE_ALSO = """.BR nix (1)
.PP
.UR https://github.com/nixidae/nanopynix
pynix repository
.UE
"""


#: Every ANSI escape sequence: the ECMA-48 CSI shape plus terminated OSC, so a
#: future theme change stays plain roff. See the module docstring for why the
#: renderer strips them instead of switching the parser colors off.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _sanitize(text: str) -> str:
    """The page *text* as roff that renders without a warning.

    Both `'` and `.` open a control line, so a text line that starts with
    either reads as a request. Wrapping decides where a line starts, so a
    quoted word can land first. An escape keeps the apostrophe literal. A
    content line that starts with `.` holds the same hazard, but this pass
    runs after `Manpage` has mixed requests and content, so it cannot tell
    one `.PP` from one `.profile`. It leaves `.` lines alone.
    """
    plain = _ANSI_ESCAPE.sub("", text)
    return "\n".join(f"\\&{line}" if line.startswith("'") else line for line in plain.split("\n"))


def _child_parsers(parser: argparse.ArgumentParser) -> Iterator[argparse.ArgumentParser]:
    """Each subparser under *parser*, built, in declaration order.

    argparse names no public way to enumerate the actions of a parser, so this
    reads `_actions` and `choices` in one place.
    """
    for action in parser._actions:  # noqa: SLF001 -- see docstring
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001 -- see docstring # type: ignore[reportPrivateUsage] -- see docstring
            # argparse types the choices of a subparsers action loosely, and
            # this map holds None for a parser that nothing built yet, so
            # pyright cannot follow either fact. The lookup below builds the
            # parser, which is the whole point of the lazy map.
            choices: Any = action.choices  # type: ignore[reportUnknownMemberType] -- see above
            for name in list(choices):
                yield choices[name]


def _materialize(parser: argparse.ArgumentParser) -> None:
    """Build every parser under *parser*, so no placeholder survives."""
    for sub in _child_parsers(parser):
        _materialize(sub)


def _page_name(prog: str) -> str:
    """The file name of the page for *prog*. `pynix build` becomes `pynix-build.1`."""
    return f"{prog.replace(' ', '-')}.1"


def _page_data(version: str, description: str) -> dict[str, str]:
    """The hand-written page data that argparse cannot supply."""
    return {
        "description": description,
        "manual_section": "1",
        "manual_title": _MANUAL_TITLE,
        "project_name": "pynix",
        "url": _PROJECT_URL,
        "version": version,
    }


def _new_page(parser: argparse.ArgumentParser, data: dict[str, str]) -> Any:
    """One argparse-manpage page object for *parser*.

    Untyped, so `Any`: the library ships no type information, and pyright
    narrows a declared `Any` back to unknown on assignment, so no call site
    can hold the object in a checked name. The single `Any` boundary here is
    what keeps the two call sites below clean.
    """
    return Manpage(parser, _data=data)


def _render_root(parser: argparse.ArgumentParser, version: str, examples: str | None) -> str:
    """The `pynix.1` page: generated reference plus hand-written sections."""
    man = _new_page(parser, _page_data(version, _ROOT_DESCRIPTION))
    if examples is not None:
        man.add_section("examples", "=", examples)
    man.add_section("see also", "=", _SEE_ALSO)
    return _sanitize(str(man))


def _render_command(parser: argparse.ArgumentParser, version: str) -> str:
    """One `pynix-<command>.1` page.

    The NAME line is the first line of the docstring, which is the same
    string ``libpynix`` passes as ``help=`` when it mounts the subparser.
    """
    short = parser.description.partition("\n")[0] if parser.description else parser.prog
    man = _new_page(parser, _page_data(version, short))
    return _sanitize(str(man))


def _write(out_dir: pathlib.Path, name: str, text: str) -> None:
    (out_dir / name).write_text(text, encoding="utf-8")
    print(f"{name}: {len(text.encode('utf-8'))} bytes")  # noqa: T201 -- the build log is the report


def main() -> None:
    _, out_dir_name, *rest = sys.argv
    out_dir = pathlib.Path(out_dir_name)
    examples = pathlib.Path(rest[0]).read_text(encoding="utf-8") if rest else None
    version = importlib.metadata.version("pynix")
    parser = build_parser(Pynix)
    _materialize(parser)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write(out_dir, _page_name(parser.prog), _render_root(parser, version, examples))
    for sub in _child_parsers(parser):
        _write(out_dir, _page_name(sub.prog), _render_command(sub, version))


main()
