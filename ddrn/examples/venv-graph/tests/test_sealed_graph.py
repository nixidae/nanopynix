"""The whole example, built over artefacts that no index serves.

`../run.sh` is the proof over the real lock file, and every artefact of that
lock comes from PyPI. This module runs the same graph over `./fixtures`, which
are derivations of this repository, so no node of the graph reaches an index.

**The build still substitutes from `cache.nixos.org`.** `bash`, `coreutils`
and `python3` come from nixpkgs, the same way they do for any derivation. What
the fixtures remove is the dependency on PyPI, and on the versions that PyPI
serves today.

What one run proves:

1. A planner runs inside a build, and submits a **graph** rather than one
   derivation.
2. A source distribution builds. Its backend is declared inside the archive,
   which no evaluation has read, so the node for it is a second planner.
3. A PEP 660 editable reads a tree outside the store, and two trees give two
   answers **from one environment**.
4. The same local project ships instead: `build_sdist` writes the archive that
   an index would serve, and `build_wheel` builds a wheel out of it. That
   environment holds the code and follows no tree.
5. A project below the top of its tree builds, and so does one below the top of
   its archive. These are the two source kinds of `uv.lock` that name a place:
   a git checkout and a URL.
6. A chain of source distributions reaches `sdistPlannerDepth` and builds, and
   one link more makes `graph.nix` throw with the name of that constant.

**This test builds the patched Nix.** That takes about 20 minutes the first
time, and nothing after that, because the work directory holds the result.
`DDRN_WORK` names that directory.
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
REPO = EXAMPLE.parents[2]

#: The features that the patched Nix needs to evaluate and to build a graph.
FEATURES = "nix-command flakes ca-derivations dynamic-derivations"

#: The feature that a **build** of a planner needs. Nix compares it in
#: `DerivationBuildingGoal::tryToBuild`, which runs only after substitution
#: failed, so a consumer that substitutes every output needs no such feature.
#: This test builds everything, because its store is private and no cache has
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

    def command(self, attribute: str, *arguments: str) -> list[str]:
        """The command that builds one attribute of `sealed.nix`."""
        return [
            self.cli,
            "build",
            "--store",
            str(self.store),
            "--file",
            str(HERE / "sealed.nix"),
            attribute,
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

    async def build(self, attribute: str, *arguments: str) -> str:
        """Build one attribute of `sealed.nix`, and give back its output path."""
        return await run(self.command(attribute, *arguments))

    async def build_failing(self, attribute: str, *arguments: str) -> str:
        """Build one attribute that must fail, and give back what it printed."""
        return await run_failing(self.command(attribute, *arguments))

    async def cat(self, path: str) -> str:
        return await run(
            [self.cli, "--store", str(self.store), "--extra-experimental-features", FEATURES, "store", "cat", path]
        )


def environment_of(report: str) -> str:
    """The store path that the report names as the environment it read."""
    lines = report.splitlines()
    return lines[lines.index("environment") + 1].strip()


@pytest.fixture(scope="module")
def work() -> Path:
    """Where the patched Nix and the private store live, between runs."""
    named = os.environ.get("DDRN_WORK")
    if named:
        return Path(named)
    return Path(os.environ.get("TMPDIR", "/tmp")) / "ddrn-sealed-venv"


async def test_the_sealed_graph_builds_and_runs(work: Path, tmp_path: Path) -> None:
    toolchain = Toolchain(work)
    await toolchain.prepare()

    # **The planner submits a `.drv`, and not an environment.** Its own output
    # is the root of the graph that it registered, and nothing is downloaded
    # or built by that step.
    root = await toolchain.build("planner")
    assert root.endswith("-sealed-venv.drv"), root

    check = await toolchain.build("check")
    report = await toolchain.cat(check)

    # The console script exists only if the installer that the planner
    # bootstrapped wrote it. An unpacked wheel carries no such file.
    assert "hello from demo-lib" in report
    # `demo-tool` is a source distribution, and `demoback` is another artefact
    # of the same list. This line exists only if the second planner read
    # `pyproject.toml` out of the archive.
    assert "demo-tool was built by demoback" in report
    assert "hello from the tree that the sealed check named" in report

    # **The editable proof.** A second tree reads back differently from the
    # same environment, and nothing about the environment is rebuilt.
    other = tmp_path / "other-tree"
    shutil.copytree(EXAMPLE / "tests/fixtures/demo-app", other)
    for path in other.rglob("*"):
        path.chmod(path.stat().st_mode | 0o200)
    module = other / "src/demoapp/__init__.py"
    await anyio.Path(module).write_text(
        (await anyio.Path(module).read_text()).replace(
            "the tree that the sealed check named",
            "a tree that this test wrote a moment ago",
        )
    )

    other_check = await toolchain.build("check", "--arg", "editableTree", str(other))
    other_report = await toolchain.cat(other_check)
    assert "a tree that this test wrote a moment ago" in other_report

    # One environment, and two checks that read it. Prove it, rather than say
    # it: each report names the environment that it ran.
    assert environment_of(report) == environment_of(other_report)
    assert environment_of(report).endswith("-sealed-venv")


async def test_the_same_project_ships_as_a_source_distribution(work: Path) -> None:
    """The release form of `demo-app`, out of the same graph and the same tree.

    **Two PEP 517 hooks run, and the second reads what the first wrote.**
    `build_sdist` writes the archive that an index would serve, and
    `build_wheel` builds a wheel out of that archive. Neither node can exist
    before the plan runs, which is the argument of this whole example.

    The proof is the path that the module came from. An editable install of
    this same project reports a path under `$DDRN_EDITABLE_ROOT`, and this one
    reports a path inside the environment.
    """
    toolchain = Toolchain(work)
    await toolchain.prepare()

    root = await toolchain.build("sdistPlanner")
    assert root.endswith("-sealed-sdist-venv.drv"), root

    check = await toolchain.build("sdistCheck")
    report = await toolchain.cat(check)

    environment = environment_of(report)
    assert environment.endswith("-sealed-sdist-venv")

    lines = report.splitlines()
    module = lines[lines.index("loaded from") + 1].strip()
    # The environment holds the code. The archive carried it, and nothing
    # outside the store answers for it.
    assert module.startswith(environment), module

    # The tree of the fixture says this, and it reached the archive, so the
    # code that the wheel installed is the code that the project holds.
    assert "hello from the tree that the sealed check named" in report


async def test_a_project_below_the_top_of_a_tree_and_of_an_archive(work: Path) -> None:
    """The two source kinds of `uv.lock` that name a place, in one graph.

    **Both may put the project below the top of what they name**, and `uv.lock`
    writes `subdirectory` when they do. `pypa/hatch` is the case that both
    fixture lock files record: the repository declares one project at its root,
    and `hatchling` lives in `backend`.

        nested-app    a tree, the way a git checkout arrives. One PEP 517
                      `build_wheel` call over `backend/` is the whole build,
                      and the backend comes out of the menu.
        demo-nested   an archive, the way a URL source arrives. A planner reads
                      the declaration under `backend/` out of it, and a second
                      node builds the wheel. That project carries its own
                      backend, with the `backend-path` of PEP 517 and an empty
                      `requires`, which is what `hatchling` declares.

    **Each one carries a decoy above the project.** The top of the tree and the
    top of the archive both declare `the_backend_of_the_wrong_project`, which
    no artefact of this menu supplies, so a build that ignored the subdirectory
    fails rather than install the wrong thing.
    """
    toolchain = Toolchain(work)
    await toolchain.prepare()

    root = await toolchain.build("nestedPlanner")
    assert root.endswith("-sealed-nested-venv.drv"), root

    check = await toolchain.build("nestedCheck")
    report = await toolchain.cat(check)

    assert "hello from the project below the top of its tree" in report
    assert "hello from the project below the top of its archive" in report

    # **A `local-wheel` install holds the code**, which is the difference
    # between a git source and a workspace member. No variable named a tree for
    # this environment, and the module is inside it.
    environment = environment_of(report)
    lines = report.splitlines()
    module = lines[lines.index("loaded from") + 1].strip()
    assert module.startswith(environment), module


async def test_a_chain_of_three_planners_builds(work: Path) -> None:
    """The bound of `sdistPlannerDepth`, reached and not passed.

    **A planner of a source distribution may need a planner of its own.** The
    backend of `link-a` is `link-b`, which is a source distribution, and the
    backend of that one is `link-c`. So three planners run, one inside the
    build of the one above it, and the wheel `link-d` ends the chain.

    `sdistPlannerDepth` of `../default.nix` is 3, and this is the root that
    uses every level of it.
    """
    toolchain = Toolchain(work)
    await toolchain.prepare()

    root = await toolchain.build("chainPlanner")
    assert root.endswith("-link-a-venv.drv"), root

    check = await toolchain.build("chainCheck")
    report = await toolchain.cat(check)

    # The module exists only if each of the three planners ran and each build
    # that they registered succeeded.
    environment = environment_of(report)
    lines = report.splitlines()
    module = lines[lines.index("the chain built") + 1].strip()
    assert module.startswith(environment), module


async def test_a_chain_of_four_planners_names_the_bound(work: Path) -> None:
    """One link more, and the graph refuses rather than recurse for ever.

    `link-0` sits on top of the same chain, so it needs a fourth planner.
    `../default.nix` instantiates a planner for each source distribution down
    to `sdistPlannerDepth` levels and no further, so the artefact of `link-c`
    reaches the fourth level with no planner attached.

    **The outer planner still builds.** It registers the graph and submits it,
    and nothing there reads past the first level. The failure arrives when the
    third planner evaluates `graph.nix` and asks for a fourth.
    """
    toolchain = Toolchain(work)
    await toolchain.prepare()

    root = await toolchain.build("tooDeepPlanner")
    assert root.endswith("-link-0-venv.drv"), root

    output = await toolchain.build_failing("tooDeepVenv")

    # The message has to name the file and the constant to change, because a
    # reader who meets it has a chain that this bound does not fit.
    assert "link_c-1.0.0.tar.gz needs a planner" in output
    assert "sdistPlannerDepth" in output
