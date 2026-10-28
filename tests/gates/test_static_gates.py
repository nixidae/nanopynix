"""Run the static gates of CI, from inside pytest.

`nix/checks.nix` makes each gate a derivation, and the `static-checks` job
builds them. That job is the authority, and it stays the authority. This module
runs the same commands in the dev shell, so that a lint error or a type error
appears in the run a developer already starts, and not twenty minutes later in
CI.

**These tests do not gate anything, on purpose.** Each one is
`xfail(strict=False)`, so a failing tool reports `xfail` and a clean tool
reports `xpass`, and neither turns the run red. The reason is the split above:
CI already refuses the merge, and a second refusal in the local loop would stop
a developer from running the suite while a lint error is open. The value here
is the signal, and the signal arrives first because
`pytest_collection_modifyitems` in `tests/conftest.py` moves these items to the
front.

**The packaged runner skips every one of them.** `ruff` and `zuban` are dev
shell tools, and neither is in the closure of `nix/tests.nix`. So the
runner that CI executes finds nothing on PATH and skips, which is correct: the
gates in that job are the derivations, and running them twice would only make
the job slower.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from test_support.subprocess_output import run_process

REPO_ROOT = Path(__file__).resolve().parents[2]

# Each gate runs from REPO_ROOT, or from the directory that holds the
# `mypy.ini` it reads, with the command that CLAUDE.md and nix/checks.nix give,
# so that a finding here is a finding there.
#
# `--no-fix` is the one difference, and it is required: the documented
# developer command is `ruff check --fix`, and a test must not rewrite the tree
# it is reading.
_ZUBAN = ("zuban", "mypy", "--python-executable", sys.executable)
# The `typedProjects` of nix/checks.nix.
_TYPED_PROJECTS = (
    ".",
    "libpynix",
    "pynix",
    "pynix/completions",
    "pynix-lsp",
    "completion-spike",
    "nanopynix-testing",
    "pytest-agent",
    "test-support",
)
_GATES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("ruff", ("ruff", "check", "--no-fix"), "."),
    # `ruff-strict.toml` reports zero findings, and CLAUDE.md requires that it
    # stays at zero, so it is a gate in its own right rather than a stricter
    # view of the first one.
    ("ruff-strict", ("ruff", "check", "--no-fix", "--config", "ruff-strict.toml"), "."),
    *((f"zuban:{project}", _ZUBAN, project) for project in _TYPED_PROJECTS),
    # `scripts/` holds hand-written shell that no gate read until
    # `check-shell`. `writeShellApplication` shellchecks the script it
    # generates, and none of these is one of those.
    (
        "shellcheck",
        ("shellcheck", "-x", *sorted(str(path) for path in (REPO_ROOT / "scripts").glob("*.sh"))),
        ".",
    ),
)

pytestmark = [
    # Read by `pytest_collection_modifyitems` in tests/conftest.py, which puts
    # these items after the forked tests and before everything else.
    pytest.mark.static_gate,
    pytest.mark.xfail(
        strict=False,
        reason="the static-checks job of CI is the gate; this run is the local signal",
    ),
]


@pytest.mark.parametrize(("name", "command", "directory"), _GATES, ids=[gate[0] for gate in _GATES])
async def test_static_gate(name: str, command: Sequence[str], directory: str) -> None:
    """Run one static gate against the checkout, and report what it said."""
    executable = shutil.which(command[0])
    if executable is None:
        pytest.skip(f"{command[0]} is not on PATH, which is expected under the packaged runner")

    result = await run_process([executable, *command[1:]], cwd=REPO_ROOT / directory)

    assert result.returncode == 0, f"the {name} gate reported findings: {result.describe()}"
