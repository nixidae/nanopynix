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
7. The application becomes a derivation that carries `meta.mainProgram`, and
   the build of it refuses a program that the application does not hold.
8. A source distribution that holds C code builds when, and only when, the
   caller names a compiler at the `buildTools` argument.

**This test builds the patched Nix.** That takes about 20 minutes the first
time, and nothing after that, because the work directory holds the result.
`DDRN_WORK` names that directory.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pytest
from toolchain import EXAMPLE, HERE, environment_of

if TYPE_CHECKING:
    from toolchain import Toolchain

pytestmark = pytest.mark.nix

#: The expression that every test of this module builds. Each attribute of it
#: is one planner or one check.
SEALED = HERE / "sealed.nix"


async def test_the_sealed_graph_builds_and_runs(toolchain: Toolchain, tmp_path: Path) -> None:
    # **The planner submits a `.drv`, and not an environment.** Its own output
    # is the root of the graph that it registered, and nothing is downloaded
    # or built by that step.
    root = await toolchain.build(SEALED, "planner")
    assert root.endswith("-sealed-venv.drv"), root

    check = await toolchain.build(SEALED, "check")
    report = toolchain.cat(check)

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

    other_check = await toolchain.build(SEALED, "check", editableTree=str(other))
    other_report = toolchain.cat(other_check)
    assert "a tree that this test wrote a moment ago" in other_report

    # One environment, and two checks that read it. Prove it, rather than say
    # it: each report names the environment that it ran.
    assert environment_of(report) == environment_of(other_report)
    assert environment_of(report).endswith("-sealed-venv")


async def test_the_same_project_ships_as_a_source_distribution(toolchain: Toolchain) -> None:
    """The release form of `demo-app`, out of the same graph and the same tree.

    **Two PEP 517 hooks run, and the second reads what the first wrote.**
    `build_sdist` writes the archive that an index would serve, and
    `build_wheel` builds a wheel out of that archive. Neither node can exist
    before the plan runs, which is the argument of this whole example.

    The proof is the path that the module came from. An editable install of
    this same project reports a path under `$DDRN_EDITABLE_ROOT`, and this one
    reports a path inside the environment.
    """
    root = await toolchain.build(SEALED, "sdistPlanner")
    assert root.endswith("-sealed-sdist-venv.drv"), root

    check = await toolchain.build(SEALED, "sdistCheck")
    report = toolchain.cat(check)

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


async def test_a_project_below_the_top_of_a_tree_and_of_an_archive(toolchain: Toolchain) -> None:
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
    root = await toolchain.build(SEALED, "nestedPlanner")
    assert root.endswith("-sealed-nested-venv.drv"), root

    check = await toolchain.build(SEALED, "nestedCheck")
    report = toolchain.cat(check)

    assert "hello from the project below the top of its tree" in report
    assert "hello from the project below the top of its archive" in report

    # **A `local-wheel` install holds the code**, which is the difference
    # between a git source and a workspace member. No variable named a tree for
    # this environment, and the module is inside it.
    environment = environment_of(report)
    lines = report.splitlines()
    module = lines[lines.index("loaded from") + 1].strip()
    assert module.startswith(environment), module


async def test_a_chain_of_three_planners_builds(toolchain: Toolchain) -> None:
    """The bound of `sdistPlannerDepth`, reached and not passed.

    **A planner of a source distribution may need a planner of its own.** The
    backend of `link-a` is `link-b`, which is a source distribution, and the
    backend of that one is `link-c`. So three planners run, one inside the
    build of the one above it, and the wheel `link-d` ends the chain.

    `sdistPlannerDepth` of `../default.nix` is 3, and this is the root that
    uses every level of it.
    """
    root = await toolchain.build(SEALED, "chainPlanner")
    assert root.endswith("-link-a-venv.drv"), root

    check = await toolchain.build(SEALED, "chainCheck")
    report = toolchain.cat(check)

    # The module exists only if each of the three planners ran and each build
    # that they registered succeeded.
    environment = environment_of(report)
    lines = report.splitlines()
    module = lines[lines.index("the chain built") + 1].strip()
    assert module.startswith(environment), module


async def test_a_chain_of_four_planners_names_the_bound(toolchain: Toolchain) -> None:
    """One link more, and the graph refuses rather than recurse for ever.

    `link-0` sits on top of the same chain, so it needs a fourth planner.
    `../default.nix` instantiates a planner for each source distribution down
    to `sdistPlannerDepth` levels and no further, so the artefact of `link-c`
    reaches the fourth level with no planner attached.

    **The outer planner still builds.** It registers the graph and submits it,
    and nothing there reads past the first level. The failure arrives when the
    third planner evaluates `graph.nix` and asks for a fourth.
    """
    root = await toolchain.build(SEALED, "tooDeepPlanner")
    assert root.endswith("-link-0-venv.drv"), root

    output = await toolchain.build_graph_failing(SEALED, "tooDeepPlanner")

    # The message has to name the file and the constant to change, because a
    # reader who meets it has a chain that this bound does not fit.
    assert "link_c-1.0.0.tar.gz needs a planner" in output
    assert "sdistPlannerDepth" in output


class TestAProjectThatHoldsCCode:
    """`buildTools`, which is what a node gets and what no other node gets.

    **A node of the graph carries no compiler.** `../graph.nix` gives every
    node `coreutils`, `unzip`, `tar`, `gzip` and the interpreter, so a
    compiler in that list would enter the closure of every pure Python wheel
    as well. `buildTools` of `../default.nix` names one for one package.

    `fixtures/demo-native` is the archive that needs it. The backend compiles
    every `.c` of the project, so the wheel holds a module that no pure Python
    build could write.
    """

    async def test_the_compiler_reaches_the_one_package_that_names_it(self, toolchain: Toolchain) -> None:
        root = await toolchain.build(SEALED, "nativePlanner")
        assert root.endswith("-sealed-native-venv.drv"), root

        check = await toolchain.build(SEALED, "nativeCheck")
        report = toolchain.cat(check)

        # The import of the compiled module is what makes this line, so the
        # line exists only if `cc` ran inside the build of the archive.
        assert "hello from C, which gives 42" in report

        # **The wheel says which interpreter it is for, and the file name of
        # the module says it again.** `sysconfig` gives the backend the suffix
        # of this build, and a wheel that claimed `py3-none-any` would install
        # a binary under a tag that means "any interpreter, any platform".
        lines = report.splitlines()
        module = lines[lines.index("the module") + 1].strip()
        assert module.startswith("_speedup."), module
        assert module.endswith(".so"), module

    async def test_a_build_that_names_no_compiler(self, toolchain: Toolchain) -> None:
        # **The outer planner still builds.** It registers the graph and
        # submits it, and no compiler is needed for that. The failure arrives
        # in the node that runs the backend.
        root = await toolchain.build(SEALED, "nativeNoToolsPlanner")
        assert root.endswith("-sealed-native-venv.drv"), root

        output = await toolchain.build_graph_failing(SEALED, "nativeNoToolsPlanner")

        # **The message names the program, and not a directory.** The backend
        # runs `cc`, and the node holds no directory that carries one, so the
        # interpreter raises where the compiler would have run.
        assert "No such file or directory: 'cc'" in output


class TestTheApplicationAsADerivation:
    """`../application.nix`, over the fixtures.

    **`builtins.outputOf` gives a string, and a string carries no `meta`.** So
    `nix run`, `lib.getExe` and a NixOS module each refuse it. `application.nix`
    wraps the application in a derivation whose whole content is one symbolic
    link, and `ddrn/NIX-FACTS.md` gives each measurement.

    `uv.nix` is the other caller of that file, and `test_projects.py` covers it
    over a real lock file. This class covers the file with no network.
    """

    async def test_the_wrapper_takes_the_name_of_the_program(self, toolchain: Toolchain) -> None:
        # **The store path is named after the program, and not after the
        # environment.** So `result/bin/demo-lib` and the path itself agree.
        wrapper = await toolchain.build(SEALED, "app")
        assert wrapper.endswith("-demo-lib"), wrapper

        # The whole content is one symbolic link, so the closure grows by this
        # path alone and the application behind it stays one store path.
        assert toolchain.list_files(wrapper) == []
        assert toolchain.link_target(wrapper).endswith("-sealed-app")

    async def test_the_wrapper_names_the_program(self, toolchain: Toolchain) -> None:
        source = toolchain.source(SEALED, "app.meta.mainProgram")
        assert await toolchain.evaluate(source) == "demo-lib"

    async def test_a_program_that_the_application_does_not_hold(self, toolchain: Toolchain) -> None:
        # **The build verifies the name rather than trust it.** A member that
        # installs a console script of another name would otherwise give a
        # `mainProgram` that names nothing, and `nix run` would fail with a
        # message about a missing file.
        report = await toolchain.build_failing(SEALED, "appWrongProgram")
        assert "holds no program called 'not-a-program'" in report
        # The refusal lists what the application does hold.
        assert "demo-lib" in report
