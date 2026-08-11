"""The `uvCheck` argument of `../uv.nix`, which asks uv about the lock file.

**The planner is the check, and this is the backstop.**
`ddrn_venv._freshness` compares the lock file with the `pyproject.toml` of each
member, and `ddrn_venv._workspace` answers which directories are members.
`Lock::satisfies` of uv holds 21 ways for a lock file to be stale, and those
two comparisons cover the ones that a build reaches. `uvCheck` runs
`uv lock --check` in a derivation of its own, and the planner waits for it.

Two things need proof, and this module gives each one:

- **The check raises no false alarm.** Every project of `../projects` passes
  it, so a user who turns the argument on gets no refusal of a lock file that
  uv itself accepts.
- **The check refuses what the planner accepts.** A marker on one dependency
  is the gap that `_freshness` documents, and uv finds it.

The build needs `pkgs.uv` and `pkgs.python3`, and it needs no network and no
patched Nix. `uv lock --check --offline` reaches the network only when the
lock file gave uv a reason to resolve again, so a refusal offline is a
refusal.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = pytest.mark.nix

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parent

#: Every project of the example. Each one passes, and a failure of any one is a
#: false alarm that would stop a build that uv calls current.
PROJECTS = [
    "pure-app",
    "packaged-app",
    "workspace",
    "binary-app",
    "git-app",
    "vendored-app",
    "local-index",
]

#: `uv.nix` asks for this, and `lockCheck` reads none of it. The path needs to
#: exist for nothing, because the attribute is lazy.
UNUSED_ENV = "/nix/store/0000000000000000000000000000000-unused"


def command(project: Path) -> Sequence[str]:
    """The `nix build` that builds the check of one project, and nothing else."""
    return [
        "nix",
        "build",
        "--impure",
        "--file",
        str(EXAMPLE / "uv.nix"),
        "lockCheck",
        "--arg",
        "project",
        str(project),
        "--argstr",
        "nanopynixEnv",
        UNUSED_ENV,
        "--no-link",
        "--print-out-paths",
        # Nix prints the last lines of the log of a build that failed, and the
        # default is 25. The diagnostic of this check is longer than that.
        "--log-lines",
        "100",
    ]


async def check(project: Path) -> tuple[int, str]:
    """Build the check of `project`, and give back its code and its output."""
    result = await anyio.run_process(list(command(project)), env=dict(os.environ), check=False)
    return result.returncode, (result.stdout + result.stderr).decode()


class TestEveryProjectPasses:
    """No false alarm, which is the condition for the argument to be usable."""

    @pytest.mark.parametrize("name", PROJECTS)
    async def test_uv_calls_the_lock_file_current(self, name: str) -> None:
        code, output = await check(EXAMPLE / "projects" / name)
        assert code == 0, output


class TestWhatItCatches:
    """The gap of `_freshness`, and the edit that opens it."""

    async def test_a_marker_on_one_dependency_is_a_refusal(self, tmp_path: Path) -> None:
        # `_freshness` drops every marker, because uv simplifies a marker
        # against `requires-python` and a reader that compared the text would
        # call an ordinary lock file stale. So this edit passes the planner and
        # builds an environment that still holds `rich` on Linux.
        project = tmp_path / "workspace"
        shutil.copytree(EXAMPLE / "projects/workspace", project)
        member = project / "packages/ws-cli/pyproject.toml"
        member.write_text(
            member.read_text(encoding="utf-8").replace(
                '"rich>=15.0.0",',
                "\"rich>=15.0.0 ; sys_platform == 'darwin'\",",
            ),
            encoding="utf-8",
        )

        code, output = await check(project)
        assert code != 0
        assert "Run `uv lock` in the project, and build again." in output

        # `--frozen` resolves nothing, so it finds no marker and says so. The
        # refusal comes from `--check`, and this is the line that proves the
        # two modes differ.
        assert "only checked for validity" in output

    async def test_a_workspace_that_grew_a_member_is_a_refusal(self, tmp_path: Path) -> None:
        # `check_members` finds this one as well, so the two agree here. The
        # value of the case is that agreement: the backstop does not contradict
        # the reader on what the reader does implement.
        project = tmp_path / "workspace"
        shutil.copytree(EXAMPLE / "projects/workspace", project)
        (project / "packages/ws-extra").mkdir()
        (project / "packages/ws-extra/pyproject.toml").write_text(
            '[project]\nname = "ws-extra"\nversion = "0.1.0"\nrequires-python = ">=3.13"\ndependencies = []\n',
            encoding="utf-8",
        )
        root = project / "pyproject.toml"
        root.write_text(
            root.read_text(encoding="utf-8").replace(
                '"packages/ws-cli",',
                '"packages/ws-cli",\n    "packages/ws-extra",',
            ),
            encoding="utf-8",
        )

        code, output = await check(project)
        assert code != 0
        assert "Run `uv lock` in the project, and build again." in output

        # A missing member is structural, so `--frozen` names it exactly. That
        # is the whole reason the failure path asks uv a second time: `--check`
        # resolves, and a sandbox has no index to resolve against, so it
        # reports the package that it could not read.
        assert "Missing workspace member `ws-extra`" in output
