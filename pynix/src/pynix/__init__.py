from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

import deferimport

# Every module that loads after this line imports lazily, so a plain import
# at the top of a module costs nothing until a name is used. A runtime type
# checker needs real objects, and `DEFERIMPORT=eager` makes this a no-op.
#
# Denied, so eager: rapidfuzz picks its native module inside
# `with contextlib.suppress(ImportError):`, where an import goes lazy, so its
# fallback never ran and the first call raised. pynix/tests/test_lazy_imports.py
# resolves every lazy import to find the next one.
deferimport.install("deny=rapidfuzz")

from libpynix import Command, build_parser, complete, dispatch, group, parse_args
from pynix import _impl
from pynix._script import ShebangError, command_base_dir_of, expand_script
from pynix._settings import PynixCommand, running
from pynix._version import version_document, version_line
from pynix.build import Build
from pynix.config import Config
from pynix.copy import Copy
from pynix.derivation import Derivation
from pynix.develop import Develop, PrintDevEnv
from pynix.edit import Edit
from pynix.env import Env, Shell
from pynix.eval import Eval
from pynix.flake import Flake
from pynix.fmt import Fmt
from pynix.hash import Hash
from pynix.key import Key
from pynix.log import Log
from pynix.nar import Nar
from pynix.path_info import PathInfo
from pynix.registry import Registry
from pynix.repl import Repl
from pynix.run import Run
from pynix.sbom import Sbom
from pynix.search import Search
from pynix.store import Store
from pynix.why_depends import WhyDepends

# **One list, and the order is the order `pynix --help` prints.**
#
# It used to be a union annotation, because clypi eval()s `Pynix.subcommand`
# against this module's globals while the class body runs. Issue #214 replaced
# clypi with argparse, so this is an ordinary list and every name in it is an
# ordinary import.
#
# `pynix-lsp` is not here. It is its own program, which is what an editor calls
# and what sits beside `pynix` on the PATH of the dev shell. The alias it used
# to have cost a static union and a runtime one, a meta test to keep the two in
# step, a third question in `checks.pynix-isolated`, and a dev shell that loaded
# 647 modules where a release build loads 202. Issues #107 and #123.
Pynix = group(
    "pynix",
    help="pynix — nanopynix CLI",
    subcommands=[
        Build,
        Config,
        Copy,
        Eval,
        Derivation,
        Develop,
        Edit,
        Env,
        Flake,
        Fmt,
        Hash,
        Key,
        Log,
        Nar,
        PathInfo,
        PrintDevEnv,
        Registry,
        Repl,
        Run,
        Sbom,
        Search,
        # `nix shell` is an alias of `nix env shell` (`src/nix/main.cc:166`), and so is this.
        Shell,
        Store,
        WhyDepends,
    ],
)


def root_parser() -> argparse.ArgumentParser:
    """The parser of `pynix`, with the flags that only the root takes.

    The dests are not field names, so no command receives them.
    """
    parser = build_parser(Pynix)
    parser.add_argument(
        "--version", dest="show_version", action="store_true", help="Show the version of pynix and of Nix."
    )
    parser.add_argument("--json", dest="version_json", action="store_true", help="With --version: print JSON.")
    return parser


def _command_of(parser: argparse.ArgumentParser, namespace: argparse.Namespace) -> Command:
    """The command *namespace* names, or the version and an exit when it asks for that."""
    if namespace.version_json and not namespace.show_version:
        parser.error("--json before the command needs --version")
    if namespace.show_version:
        # As `nix --version`, which prints and exits whatever else the line holds.
        if namespace.version_json:
            sys.stdout.write(json.dumps(version_document(), sort_keys=True, indent=2) + "\n")
        else:
            sys.stdout.write(version_line() + "\n")
        raise SystemExit(0)
    return dispatch(parser, namespace)


def parse(argv: Sequence[str]) -> Command:
    """The command that *argv* names, built and ready to run.

    What `main` does, without running anything. A test drives the real parser
    through this rather than a double, so a change to a declaration is a change
    the test sees.
    """
    parser = root_parser()
    return _command_of(parser, parse_args(parser, list(argv)))


def main() -> None:
    # **The parse comes first, and the set-up after it.** A shell completion
    # and `--help` both end inside this function, and neither needs a logger, a
    # process title or a traceback handler. `pynix._impl.main` holds the
    # measurement.
    parser = root_parser()
    complete(parser)
    try:
        script = expand_script(sys.argv[1:])
    except ShebangError as exc:
        # Nix's words and exit status, before a logger exists (`args.cc:178`).
        sys.exit(f"error: {exc}")
    with command_base_dir_of(script.base_dir):
        command = _command_of(parser, parse_args(parser, script.argv))
        _impl.main.prepare(command.verbosity if isinstance(command, PynixCommand) else None)
        with running(command):
            _impl.main.run(command.run)


if __name__ == "__main__":
    main()
