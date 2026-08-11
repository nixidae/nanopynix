"""The seven real projects of `../projects`, built end to end against PyPI.

`test_sealed_graph.py` builds the same machinery over `./fixtures`, which are
derivations of this repository, so no node of that graph reaches an index. This
module is the other half: the lock files that `uv` itself wrote, the artefacts
that PyPI serves today, and the checks that read each environment back.

**These tests need a network, and that is what `network` marks.** A failure
here may be an outage of an index rather than a defect of this example, and the
marker keeps that failure legible. `-m "not network"` leaves them out.

Each project exercises what the ones before it cannot:

| project | what it adds |
| --- | --- |
| `pure-app` | a virtual root, and every artefact a pure wheel |
| `packaged-app` | a PEP 660 editable whose backend needs a closure, and one package built from its source distribution |
| `workspace` | two members of one lock file, and two selections of it |
| `binary-app` | a manylinux wheel that `auto-patchelf` has to reach |
| `git-app` | a dependency that `uv.lock` names by a commit |
| `vendored-app` | two dependencies that no index serves |
| `local-index` | a registry that is a directory of the project |

`../run-uv.sh` is the demonstration rather than the gate. It builds two of
these projects one step at a time, and it prints what each step produced.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from toolchain import EXAMPLE, Toolchain, environment_of, prefix_of, work_directory

pytestmark = [pytest.mark.nix, pytest.mark.network]

UV = EXAMPLE / "uv.nix"
PROJECTS = EXAMPLE / "projects"

#: Each project, and the name that the environment of it takes. The two differ
#: for the workspace, whose directory is `workspace` and whose root project is
#: `workspace-root`.
NAMES = {
    "pure-app": "pure-app",
    "packaged-app": "packaged-app",
    "workspace": "workspace-root",
    "binary-app": "binary-app",
    "git-app": "git-app",
    "vendored-app": "vendored-app",
    "local-index": "local-index",
}

#: What the whole workspace resolves to. **The test states this, and the graph
#: does not.** A selection that resolved to the wrong set would otherwise build
#: and report itself as good.
EVERY_MEMBER = '["ws-cli" "ws-core" "rich" "markdown-it-py" "mdurl" "pygments"]'


@pytest.fixture(scope="module")
async def toolchain() -> Toolchain:
    """The patched Nix and the private store, prepared once for this module.

    The work directory outlives the run, so this fixture has no teardown. The
    store holds the patched Nix, and a second run of any module reuses it.
    """
    ready = Toolchain(work_directory("ddrn-venv-graph-uv"))
    await ready.prepare()
    return ready


async def check(toolchain: Toolchain, expression: str, *arguments: str) -> str:
    """Build one check of the example, and read the report that it wrote.

    **The check runs as a derivation, and not from this process.** A chroot
    store puts the store under a prefix, and every symbolic link of an
    environment is absolute, so a program of that environment does not run
    here. `../check.nix` gives the whole reason.
    """
    return await toolchain.cat(await toolchain.build(EXAMPLE / expression, *arguments))


class TestEveryProjectPlans:
    """The first step of a build, which registers the graph and builds none."""

    @pytest.mark.parametrize(("project", "environment"), NAMES.items())
    async def test_the_planner_submits_a_derivation(self, toolchain: Toolchain, project: str, environment: str) -> None:
        # **The planner submits a `.drv`, and not an environment.** Its own
        # output is the root of the graph that it registered, and the name of
        # that root is the name of the environment.
        root = await toolchain.build(UV, "--arg", "project", str(PROJECTS / project))
        assert root.endswith(f"-{environment}.drv"), root


class TestPureApp:
    """Every artefact a pure wheel, and a root that builds nothing."""

    async def test_the_environment_holds_rich_and_its_closure(self, toolchain: Toolchain) -> None:
        report = await check(toolchain, "check-uv.nix")
        assert "rich" in report


class TestPackagedApp:
    """A PEP 660 editable, and the same project shipped as an archive."""

    async def test_the_editable_reads_the_tree_of_the_project(self, toolchain: Toolchain) -> None:
        report = await check(toolchain, "check-packaged.nix")
        assert "packaged-app" in report

    async def test_one_environment_reads_two_trees(self, toolchain: Toolchain, tmp_path: Path) -> None:
        # **The editable proof.** The `.pth` that hatchling wrote expands
        # `$DDRN_EDITABLE_ROOT` when the interpreter starts, so a second tree
        # reads back differently from the **same** environment, and nothing
        # about the environment is rebuilt.
        other = tmp_path / "other-tree"
        shutil.copytree(PROJECTS / "packaged-app", other)
        for path in other.rglob("*"):
            path.chmod(path.stat().st_mode | 0o200)
        module = other / "src/packaged_app/__init__.py"
        module.write_text(
            module.read_text(encoding="utf-8").replace(
                'WHICH_TREE = "the tree that this repository holds"',
                'WHICH_TREE = "a tree that this test wrote a moment ago"',
            ),
            encoding="utf-8",
        )

        here = await check(toolchain, "check-packaged.nix")
        there = await check(toolchain, "check-packaged.nix", "--arg", "editableTree", str(other))

        assert "a tree that this test wrote a moment ago" in there
        assert "a tree that this test wrote a moment ago" not in here
        assert prefix_of(here) == prefix_of(there)

    async def test_the_same_project_ships_as_a_source_distribution(self, toolchain: Toolchain) -> None:
        # `uv build` writes the archive that an index serves, and a user who
        # installs from it runs `build_wheel` over what the archive holds. The
        # graph performs both steps, and the archive between them is a store
        # path.
        report = await check(toolchain, "check-sdist.nix")
        assert "packaged-app" in report

    async def test_a_shipped_project_follows_no_tree(self, toolchain: Toolchain, tmp_path: Path) -> None:
        # This environment holds the code, so the tree that
        # `$DDRN_EDITABLE_ROOT` names answers for nothing.
        other = tmp_path / "ignored-tree"
        shutil.copytree(PROJECTS / "packaged-app", other)

        report = await check(toolchain, "check-sdist.nix", "--arg", "editableTree", str(other))
        assert str(other) not in report


class TestTheWorkspace:
    """One lock file, and the selections that it does not record."""

    async def test_the_whole_workspace_installs_every_member(self, toolchain: Toolchain) -> None:
        report = await check(
            toolchain,
            "check-workspace.nix",
            "--argstr",
            "venvName",
            "workspace-all",
            "--arg",
            "expect",
            EVERY_MEMBER,
        )
        # The `ws-cli` line reads the other member, so this also proves the
        # edge between two editables of one workspace.
        assert "ws-cli" in report
        assert "ws-core" in report

    async def test_one_member_of_that_same_lock_file(self, toolchain: Toolchain) -> None:
        report = await check(
            toolchain,
            "check-workspace.nix",
            "--argstr",
            "venvName",
            "workspace-core",
            "--arg",
            "select",
            '{ "ws-core" = []; }',
            "--arg",
            "expect",
            '["ws-core"]',
        )
        assert "ws-core" in report

    async def test_one_member_ships_and_the_other_one_links(self, toolchain: Toolchain) -> None:
        # `ws-core` installs from the archive that its own backend wrote, so it
        # holds its own code and stops following the tree. `ws-cli` stays a PEP
        # 660 editable and imports `ws_core`, so this also proves that the two
        # forms compose.
        report = await check(
            toolchain,
            "check-workspace.nix",
            "--argstr",
            "venvName",
            "workspace-core-sdist",
            "--arg",
            "expect",
            EVERY_MEMBER,
            "--arg",
            "fromSdist",
            '["ws-core"]',
        )
        assert "ws-cli" in report


class TestTheApplication:
    """One member as a program, and not as an environment."""

    async def test_the_workspace_ships_one_program(self, toolchain: Toolchain) -> None:
        report = await check(toolchain, "check-application.nix")
        assert "bin/ws-cli" in report
        assert environment_of(report).endswith("-workspace-app-env")

    async def test_a_project_that_is_its_own_environment(self, toolchain: Toolchain) -> None:
        # `uv init --package` made a project that is a member of its own
        # environment, so the template of the shape is the node with the
        # `-editable` suffix and not a wheel node.
        report = await check(
            toolchain,
            "check-application.nix",
            "--arg",
            "project",
            str(PROJECTS / "packaged-app"),
            "--arg",
            "editableTree",
            str(PROJECTS / "packaged-app"),
            "--arg",
            "application",
            '["packaged-app"]',
            "--argstr",
            "venvName",
            "packaged-app-bin",
        )
        assert "bin/packaged-app" in report


#: The arguments that give `uv.nix` the application of the workspace. A test of
#: `app` writes an expression, and not a command line, so it needs them here.
WORKSPACE_APP = f'{{ project = {PROJECTS / "workspace"}; application = ["ws-cli"]; venvName = "workspace-app"; }}'


def target(toolchain: Toolchain, extra: str = "") -> str:
    """The `uv.nix` of the workspace application, as a Nix expression."""
    arguments = f'{WORKSPACE_APP} // {{ nanopynixEnv = "{toolchain.env}"; {extra} }}'
    return f"(import {UV} ({arguments}))"


class TestTheApplicationAsADerivation:
    """`app`, which is the shape that every reader of `meta` needs.

    **`builtins.outputOf` gives a string, and a string carries no `meta`.** So
    `nix run` refuses it, `lib.getExe` refuses it, and a NixOS module refuses
    it. `uv.nix` wraps the application in a derivation that holds one symbolic
    link, and `ddrn/NIX-FACTS.md` gives each measurement.
    """

    async def test_the_wrapper_names_the_program(self, toolchain: Toolchain) -> None:
        # One member, so `mainProgram` takes the name of that member.
        name = await toolchain.evaluate(f"{target(toolchain)}.app.meta.mainProgram")
        assert name == "ws-cli"

    async def test_the_wrapper_takes_the_name_of_the_program(self, toolchain: Toolchain) -> None:
        # **The store path is named after the program, and not after the
        # environment.** `nix build` writes `result/bin/ws-cli`, and a reader
        # of the path sees which program it holds.
        wrapper = await toolchain.build(
            UV,
            "app",
            "--arg",
            "project",
            str(PROJECTS / "workspace"),
            "--arg",
            "application",
            '["ws-cli"]',
            "--argstr",
            "venvName",
            "workspace-app",
        )
        assert wrapper.endswith("-ws-cli"), wrapper

        # **The whole content is one symbolic link.** So the closure grows by
        # this path alone, and the application behind it stays one store path
        # however many wrappers name it.
        listing = await toolchain.list_files(wrapper)
        assert listing.count("\n") == 0, listing
        assert listing.split("->")[-1].strip().endswith("-workspace-app"), listing

    async def test_a_program_that_the_application_does_not_hold(self, toolchain: Toolchain) -> None:
        # **The build verifies the name rather than trust it.** A member that
        # installs a console script of another name would otherwise give an
        # `app` whose `mainProgram` names nothing, and `nix run` would fail
        # with a message about a missing file.
        report = await toolchain.build_failing(
            UV,
            "app",
            "--arg",
            "project",
            str(PROJECTS / "workspace"),
            "--arg",
            "application",
            '["ws-cli"]',
            "--argstr",
            "mainProgram",
            "not-a-program",
            "--argstr",
            "venvName",
            "workspace-app",
        )
        assert "holds no program called 'not-a-program'" in report
        assert "ws-cli" in report

    async def test_an_environment_has_no_application(self, toolchain: Toolchain) -> None:
        # `app` of a call that names no application is a mistake, and not the
        # environment under another name.
        plain = f'(import {UV} {{ project = {PROJECTS / "workspace"}; nanopynixEnv = "{toolchain.env}"; }})'
        refusal = await toolchain.evaluate_failing(f"{plain}.app")
        assert "names no application" in refusal


class TestBinaryApp:
    """A wheel that holds compiled code, and the same package from nixpkgs."""

    async def test_auto_patchelf_reaches_a_manylinux_wheel(self, toolchain: Toolchain) -> None:
        # A manylinux wheel names `/lib64/ld-linux-x86-64.so.2` as its ELF
        # interpreter and gives no RPATH, and neither exists on this system.
        report = await check(toolchain, "check-binary.nix")
        assert "pyzmq" in report

    async def test_nixpkgs_supplies_the_same_package(self, toolchain: Toolchain) -> None:
        # The graph builds no wheel at all for that name. Both reports run the
        # same code, so the difference is only where the code came from.
        report = await check(
            toolchain,
            "check-binary.nix",
            "--argstr",
            "venvName",
            "binary-nixpkgs",
            "--arg",
            "fromNixpkgs",
            "{ pyzmq = (import <nixpkgs> {}).python3Packages.pyzmq; }",
        )
        assert "pyzmq" in report


class TestTheSourcesThatNoIndexServes:
    """The three projects whose artefacts an index cannot answer for."""

    async def test_a_dependency_from_a_git_checkout(self, toolchain: Toolchain) -> None:
        # `uv.lock` records a commit of `pypa/flit` and no file, so the menu
        # holds nothing to download. `flit_core` builds itself, with the
        # `backend-path` of PEP 517.
        report = await check(toolchain, "check-git.nix")
        assert "flit" in report

    async def test_two_dependencies_that_the_project_holds(self, toolchain: Toolchain) -> None:
        # One of the two is a wheel, which the store takes whole. The other is
        # an archive, so the graph plans it and builds it.
        report = await check(toolchain, "check-vendored.nix")
        assert "vendored-app" in report

    async def test_a_registry_that_is_a_directory_of_the_project(self, toolchain: Toolchain) -> None:
        # The location has three parts: the project, the registry and the row.
        # uv joins the same three in `to_registry_wheel`.
        report = await check(toolchain, "check-index.nix")
        assert "stocked" in report
