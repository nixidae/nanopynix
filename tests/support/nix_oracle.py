"""The ``nix`` command as an oracle, and the version that it has to be.

A test that compares a pynix command with ``nix ... --json`` compares two
implementations of one algorithm. That comparison means something only when
both run the same version of Nix.

**nanopynix links one Nix, and the command on ``PATH`` is another program.**
``nix/shell.nix`` puts no ``nix`` in the shell, so ``PATH`` gives the Nix of
the machine whichever band the shell belongs to. On the default band the two
agree by accident, because the machine runs the version that the band pins.
Measured on 2026-08-12, in the shell of the master band::

    command -v nix   ->  /run/current-system/sw/bin/nix
    nix --version    ->  nix (Nix) 2.34.8
    running_nix_version()  ->  2.36.0pre-ddnix+2

The flake fingerprint is the first algorithm where those two disagree. Nix
master puts the name of the scheme in front of the fingerprint of an input,
and it writes ``;hasRevCount`` for a lazy ``revCount`` rather than force the
value. Nix 2.34 does neither. So the two commands print two hashes over one
flake, and every other key of the object is equal. The difference is real and
it is not a defect of pynix, so a test must not report it as one.

Ask for the oracle through this module. It skips when the versions differ, and
the message names both.
"""

from __future__ import annotations

import shutil

import pytest

from nanopynix.settings import running_nix_version
from tests.support.subprocess_output import run_process


def _release(version: str) -> str:
    """The part of a version that two builds of one Nix share.

    A package of this repository carries a build suffix after ``+``, such as
    ``2.36.0pre-ddnix+2``, and the command that the same source builds reports
    ``2.36.0pre-ddnix``. The two are one Nix, so the suffix goes.
    """
    return version.split("+", 1)[0]


async def require_nix_oracle() -> str:
    """The name of a ``nix`` command that matches the linked Nix.

    Skips the test when no such command is on ``PATH``, and when the command
    that is there reports another version.
    """
    if shutil.which("nix") is None:
        pytest.skip("the nix CLI is the oracle for this test, and it is not on PATH")
    result = await run_process(["nix", "--version"])
    if result.returncode != 0:
        pytest.skip(f"the nix CLI is the oracle for this test, and it does not run: {result.describe()}")
    # `nix (Nix) 2.34.8`, and the version is the last field.
    reported = result.stdout.split()[-1]
    linked = running_nix_version()
    if _release(reported) != _release(linked):
        pytest.skip(
            f"the nix CLI on PATH is {reported}, and nanopynix links {linked}. "
            "An oracle of another version answers a different question."
        )
    return "nix"
