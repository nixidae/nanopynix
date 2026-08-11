"""The patched Nix, the private store, and how a test drives a build.

**Two test modules build this example, and both need the same three things:**
the patched Nix that `nix/nix-master.nix` builds, the environment of nanopynix
built against that same Nix, and a private chroot store that holds both. This
module is that setup, so `test_sealed_graph.py` and `test_projects.py` each
state what they build and neither repeats how.

**The build goes into a private chroot store, and not into the store of the
machine.** The system daemon does not offer `builder-rpc-v0`, so a build that
the daemon runs never reaches the new code path. A chroot store makes the
client do the building itself, and the store directory stays `/nix/store`, so
every dependency substitutes from the binary cache rather than building.

`../run.sh` sets the same three things up for a reader, and `../README.md`
gives each command.

**The first run builds the patched Nix, and that takes about 20 minutes.** The
work directory survives the run, so a second run builds nothing. `DDRN_WORK`
names that directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from collections.abc import Sequence

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parent
REPO = EXAMPLE.parents[2]

#: The features that the patched Nix needs to evaluate and to build a graph.
FEATURES = "nix-command flakes ca-derivations dynamic-derivations"

#: The feature that a **build** of a planner needs. Nix compares it in
#: `DerivationBuildingGoal::tryToBuild`, which runs only after substitution
#: failed, so a consumer that substitutes every output needs no such feature.
#: These tests build everything, because the store is private and no cache has
#: ever served it.
SYSTEM_FEATURES = "builder-rpc-v0 recursive-nix"


async def run(command: Sequence[str], **environ: str) -> str:
    """Run `command`, and give back its standard output with no trailing space."""
    result = await anyio.run_process(
        list(command),
        env={**os.environ, **environ},
        check=False,
        stderr=None,
    )
    if result.returncode != 0:
        raise AssertionError(f"{command[0]} failed with {result.returncode}: {' '.join(command)}")
    return result.stdout.decode().strip()


async def run_failing(command: Sequence[str], **environ: str) -> str:
    """Run `command`, require that it fails, and give back what it printed.

    **The message of the failure is the thing under test**, so this one keeps
    the standard error rather than let it reach the terminal. A build that
    passed would be the defect here, so success raises.
    """
    result = await anyio.run_process(list(command), env={**os.environ, **environ}, check=False)
    if result.returncode == 0:
        raise AssertionError(f"{command[0]} was expected to fail, and it succeeded: {' '.join(command)}")
    return (result.stdout + result.stderr).decode()


class Toolchain:
    """The patched Nix, the planner environment, and the store that holds both.

    `run.sh` sets the same three things up, and this class is that script as a
    fixture. The work directory survives the run, so a second run of this test
    builds nothing.
    """

    def __init__(self, work: Path) -> None:
        self.work = work
        self.store = work / "store"
        self.nix = ""
        self.env = ""

    @property
    def cli(self) -> str:
        return f"{self.nix}/bin/nix"

    async def prepare(self) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        # `FLAKE_COMPATISH_DISABLE_OVERRIDES` makes this evaluation agree with
        # a flake evaluation, the same way every CI workflow sets it.
        environ = {"FLAKE_COMPATISH_DISABLE_OVERRIDES": "1"}
        self.nix = await run(
            [
                "nix",
                "build",
                "--impure",
                "--expr",
                f"(import {REPO} {{ }}).nanopynixMaster.nix-cli",
                "--no-link",
                "--print-out-paths",
                "--print-build-logs",
            ],
            **environ,
        )
        self.env = await run(
            [
                "nix",
                "build",
                "--impure",
                "--expr",
                f'(import {REPO} {{ }}).nanopynixMaster.pythonSet.mkVirtualEnv "ddrn-planner-env" {{ nanopynix = [ ]; }}',
                "--no-link",
                "--print-out-paths",
                "--print-build-logs",
            ],
            **environ,
        )

        # `--no-check-sigs` is needed and is safe. Both inputs were built on
        # this machine, so no cache signed them, and a chroot store refuses an
        # unsigned path. The store is a private directory of this test.
        self.store.mkdir(parents=True, exist_ok=True)
        await run(
            [
                self.cli,
                "copy",
                "--to",
                str(self.store),
                "--no-check-sigs",
                self.nix,
                self.env,
                "--extra-experimental-features",
                "nix-command flakes",
            ]
        )

    def command(self, expression: Path, *arguments: str) -> list[str]:
        """The command that builds `expression`, in the private store.

        An attribute of the file, when the file has one, is the first entry of
        `arguments`: `nix build` reads an installable where this puts it.
        """
        return [
            self.cli,
            "build",
            "--store",
            str(self.store),
            "--file",
            str(expression),
            # A string names a store path that this evaluation did not make,
            # which only an impure evaluation may do. A caller that passes the
            # derivation, as `nix/checks.nix` does, needs neither the flag nor
            # `builtins.storePath`.
            "--argstr",
            "nanopynixEnv",
            self.env,
            "--impure",
            *arguments,
            "--no-link",
            "--print-out-paths",
            "--print-build-logs",
            "--extra-experimental-features",
            FEATURES,
            "--system-features",
            SYSTEM_FEATURES,
        ]

    async def build(self, expression: Path, *arguments: str) -> str:
        """Build `expression`, and give back its output path."""
        return await run(self.command(expression, *arguments))

    async def build_failing(self, expression: Path, *arguments: str) -> str:
        """Build `expression`, which must fail, and give back what it printed."""
        return await run_failing(self.command(expression, *arguments))

    async def cat(self, path: str) -> str:
        return await run(
            [self.cli, "--store", str(self.store), "--extra-experimental-features", FEATURES, "store", "cat", path]
        )


def environment_of(report: str) -> str:
    """The store path that a report names under its `environment` heading.

    `../tests/sealed.nix` and `../check-application.nix` write that heading,
    and the path is the line under it.
    """
    lines = report.splitlines()
    return lines[lines.index("environment") + 1].strip()


def prefix_of(report: str) -> str:
    """The store path that a report names on its `prefix` line.

    `../check-uv.nix`, `../check-packaged.nix` and `../check-workspace.nix`
    write one labelled line instead of a heading. The label is the name of the
    environment that the check ran, and it is the same store path.
    """
    for line in report.splitlines():
        if line.startswith("prefix"):
            return line.split(maxsplit=1)[1].strip()
    raise AssertionError(f"the report names no prefix:\n{report}")


def work_directory(name: str) -> Path:
    """Where the patched Nix and the private store live, between runs.

    `DDRN_WORK` names one directory for every module, so a machine that has
    built the patched Nix once never builds it again.
    """
    named = os.environ.get("DDRN_WORK")
    if named:
        return Path(named)
    return Path(os.environ.get("TMPDIR", "/tmp")) / name
