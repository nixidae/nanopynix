"""The eight real projects of `../projects`, built end to end against PyPI.

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
| `native-app` | two source distributions that hold C code, and the compiler that `buildTools` names |

`../run-uv.sh` is the demonstration rather than the gate. It builds two of
these projects one step at a time, and it prints what each step produced.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import anyio
import pytest
from toolchain import EXAMPLE, Toolchain, environment_of, nix_string, prefix_of

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
    "native-app": "native-app",
}

#: What the whole workspace resolves to. **The test states this, and the graph
#: does not.** A selection that resolved to the wrong set would otherwise build
#: and report itself as good.
EVERY_MEMBER = '["ws-cli" "ws-core" "rich" "markdown-it-py" "mdurl" "pygments"]'


async def check(toolchain: Toolchain, expression: str, **arguments: str) -> str:
    """Build one check of the example, and read the report that it wrote.

    **The check runs as a derivation, and not from this process.** A chroot
    store puts the store under a prefix, and every symbolic link of an
    environment is absolute, so a program of that environment does not run
    here. `../check.nix` gives the whole reason.
    """
    return toolchain.cat(await toolchain.build(EXAMPLE / expression, **arguments))


class TestEveryProjectPlans:
    """The first step of a build, which registers the graph and builds none."""

    @pytest.mark.parametrize(("project", "environment"), NAMES.items())
    async def test_the_planner_submits_a_derivation(self, toolchain: Toolchain, project: str, environment: str) -> None:
        # **The planner submits a `.drv`, and not an environment.** Its own
        # output is the root of the graph that it registered, and the name of
        # that root is the name of the environment.
        root = await toolchain.build(UV, project=str(PROJECTS / project))
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
        there = await check(toolchain, "check-packaged.nix", editableTree=str(other))

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

        report = await check(toolchain, "check-sdist.nix", editableTree=str(other))
        assert str(other) not in report


class TestTheWorkspace:
    """One lock file, and the selections that it does not record."""

    async def test_the_whole_workspace_installs_every_member(self, toolchain: Toolchain) -> None:
        report = await check(
            toolchain,
            "check-workspace.nix",
            venvName=nix_string("workspace-all"),
            expect=EVERY_MEMBER,
        )
        # The `ws-cli` line reads the other member, so this also proves the
        # edge between two editables of one workspace.
        assert "ws-cli" in report
        assert "ws-core" in report

    async def test_one_member_of_that_same_lock_file(self, toolchain: Toolchain) -> None:
        report = await check(
            toolchain,
            "check-workspace.nix",
            venvName=nix_string("workspace-core"),
            select='{ "ws-core" = []; }',
            expect='["ws-core"]',
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
            venvName=nix_string("workspace-core-sdist"),
            expect=EVERY_MEMBER,
            fromSdist='["ws-core"]',
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
            project=str(PROJECTS / "packaged-app"),
            editableTree=str(PROJECTS / "packaged-app"),
            application='["packaged-app"]',
            venvName=nix_string("packaged-app-bin"),
        )
        assert "bin/packaged-app" in report


#: The arguments that give `uv.nix` the application of the workspace, as Nix
#: source. `Toolchain.source` adds `nanopynixEnv` to every call.
WORKSPACE_APP = {
    "project": str(PROJECTS / "workspace"),
    "application": '["ws-cli"]',
    "venvName": '"workspace-app"',
}


class TestTheApplicationAsADerivation:
    """`app`, which is the shape that every reader of `meta` needs.

    **`builtins.outputOf` gives a string, and a string carries no `meta`.** So
    `nix run` refuses it, `lib.getExe` refuses it, and a NixOS module refuses
    it. `uv.nix` wraps the application in a derivation that holds one symbolic
    link, and `ddrn/NIX-FACTS.md` gives each measurement.
    """

    async def test_the_wrapper_names_the_program(self, toolchain: Toolchain) -> None:
        # One member, so `mainProgram` takes the name of that member.
        source = toolchain.source(UV, "app.meta.mainProgram", **WORKSPACE_APP)
        assert await toolchain.evaluate(source) == "ws-cli"

    async def test_the_wrapper_takes_the_name_of_the_program(self, toolchain: Toolchain) -> None:
        # **The store path is named after the program, and not after the
        # environment.** `nix build` writes `result/bin/ws-cli`, and a reader
        # of the path sees which program it holds.
        wrapper = await toolchain.build(
            UV,
            "app",
            **WORKSPACE_APP,
        )
        assert wrapper.endswith("-ws-cli"), wrapper

        # **The whole content is one symbolic link.** So the closure grows by
        # this path alone, and the application behind it stays one store path
        # however many wrappers name it.
        assert toolchain.list_files(wrapper) == []
        assert toolchain.link_target(wrapper).endswith("-workspace-app")

    async def test_a_program_that_the_application_does_not_hold(self, toolchain: Toolchain) -> None:
        # **The build verifies the name rather than trust it.** A member that
        # installs a console script of another name would otherwise give an
        # `app` whose `mainProgram` names nothing, and `nix run` would fail
        # with a message about a missing file.
        report = await toolchain.build_failing(
            UV,
            "app",
            **WORKSPACE_APP,
            mainProgram=nix_string("not-a-program"),
        )
        assert "holds no program called 'not-a-program'" in report
        assert "ws-cli" in report

    async def test_an_environment_has_no_application(self, toolchain: Toolchain) -> None:
        # `app` of a call that names no application is a mistake, and not the
        # environment under another name.
        plain = toolchain.source(UV, "app", project=str(PROJECTS / "workspace"))
        refusal = await toolchain.evaluate_failing(plain)
        assert "names no application" in refusal


class TestTheEnvironmentAsADerivation:
    """`env`, which is what makes one command build an environment.

    **The result of `uv.nix` is the planner, so a build of it gives a `.drv`.**
    A reader of the command line then builds `"$drv^out"`, which is two
    commands for one environment. `env` is that second step as a derivation,
    so `nix build --file ./uv.nix env` is the whole build.

    It is the same shape as `app`, and `../application.nix` gives the reason
    that shape is necessary.
    """

    async def test_one_command_builds_the_environment(self, toolchain: Toolchain) -> None:
        # **The whole content is one symbolic link**, the way `app` is, so the
        # closure grows by this path alone.
        wrapper = await toolchain.build(UV, "env", project=str(PROJECTS / "pure-app"))
        assert wrapper.endswith("-pure-app"), wrapper
        assert toolchain.list_files(wrapper) == []

        environment = toolchain.link_target(wrapper)
        assert environment.endswith("-pure-app"), environment
        assert environment != wrapper

        # `result/bin/python` is what a reader of the README runs next.
        assert (toolchain.path_in_store(environment) / "bin" / "python").exists()

    async def test_the_environment_of_an_application(self, toolchain: Toolchain) -> None:
        # **`app` and `env` of one call are the two halves of one project.**
        # The environment of an application takes the `-env` suffix, so a user
        # who installs both gets one environment and not two copies of it.
        wrapper = await toolchain.build(UV, "env", **WORKSPACE_APP)
        assert wrapper.endswith("-workspace-app-env"), wrapper
        assert toolchain.list_files(wrapper) == []

        environment = toolchain.link_target(wrapper)
        assert environment.endswith("-workspace-app-env"), environment

        # Every program of the application is a link into that same path.
        application = toolchain.link_target(await toolchain.build(UV, "app", **WORKSPACE_APP))
        program = toolchain.path_in_store(application) / "bin" / "ws-cli"
        assert str(program.readlink()) == f"{environment}/bin/ws-cli"


class TestTheDevelopmentShell:
    """`shell`, which is the environment that a user works in.

    **A shell of this lab has to set `DDRN_EDITABLE_ROOT`.** A PEP 660 editable
    member redirects to that variable rather than to a store path, so one
    environment serves every tree. Every `check-*.nix` of the example sets it,
    and a shell that did not would give an environment whose editable members
    import nothing.

    **These tests evaluate, and they build nothing.** `mkShellNoCC` refuses to
    build by design, so the derivation itself is the whole subject.
    """

    #: The project that these tests read. It installs itself as an editable, so
    #: the variable decides whether `import packaged_app` answers.
    PROJECT = str(PROJECTS / "packaged-app")

    async def test_the_shell_names_the_tree_of_the_project(self, toolchain: Toolchain) -> None:
        # **The value is the directory of this machine, and not the copy of it
        # in the store.** So the shell reads the tree that the user edits.
        source = toolchain.source(UV, "shell.DDRN_EDITABLE_ROOT", project=self.PROJECT)
        assert await toolchain.evaluate(source, evaluator=toolchain.host_evaluator) == self.PROJECT

    async def test_the_shell_instantiates_and_takes_the_name_of_the_environment(self, toolchain: Toolchain) -> None:
        drv = await toolchain.drv_of(UV, "shell", evaluator=toolchain.host_evaluator, project=self.PROJECT)
        assert drv.endswith("-packaged-app-shell.drv"), drv

    async def test_the_shell_carries_the_environment(self, toolchain: Toolchain) -> None:
        # `mkShell` puts `packages` at the front of `nativeBuildInputs`, so
        # this reads the list that stdenv will put on the PATH.
        target = toolchain.source(UV, project=self.PROJECT)
        source = f"builtins.toJSON (builtins.elem {target}.env {target}.shell.nativeBuildInputs)"
        assert await toolchain.evaluate(source, evaluator=toolchain.host_evaluator) == "true"


class TestThePlannerEnvironmentArgument:
    """`nanopynixEnv`, and the default that keeps it off the command line."""

    async def test_a_call_that_names_no_planner_environment(self, toolchain: Toolchain) -> None:
        # **This test writes the expression itself.** `Toolchain.source` gives
        # `nanopynixEnv` to every call, which is the argument under test, so
        # the helper cannot ask this question.
        #
        # The default builds the environment out of this repository, and this
        # evaluates it rather than build it: the build is the 20 minute one
        # that `toolchain.py` describes.
        project = PROJECTS / "pure-app"
        source = f'(import {UV} {{ project = "{project}"; }}).env.drvPath'
        drv = await toolchain.evaluate(source, evaluator=toolchain.host_evaluator)
        assert drv.endswith("-pure-app.drv"), drv


class TestADirectoryThatIsNotAProject:
    """What `project` says when it names the wrong directory.

    **Nix names the file that is absent, and it says nothing about what that
    file is for.** So a reader who gave the wrong directory, and a reader whose
    project never ran `uv lock`, read the same message about a path. `uv.nix`
    asks `builtins.pathExists` before it reads, and it names both files.

    **These tests write the expression themselves**, because the message has to
    hold the directory that the caller gave, and `Toolchain.source` gives
    `nanopynixEnv` as well. They evaluate nothing else: the check is outermost,
    so no lock file is read and no planner environment is built.
    """

    async def test_a_directory_that_holds_neither_file(self, toolchain: Toolchain, tmp_path: Path) -> None:
        source = f'(import {UV} {{ project = "{tmp_path}"; }}).env.drvPath'
        refusal = await toolchain.evaluate_failing(source)
        assert f"{tmp_path} holds no pyproject.toml and no uv.lock" in refusal

    async def test_a_project_that_never_ran_uv_lock(self, toolchain: Toolchain, tmp_path: Path) -> None:
        # The message names the one file that is absent, and it names the
        # command that writes the file.
        await anyio.Path(tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "unlocked"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        source = f'(import {UV} {{ project = "{tmp_path}"; }}).env.drvPath'
        refusal = await toolchain.evaluate_failing(source)
        assert f"{tmp_path} holds no uv.lock" in refusal
        assert "Run `uv lock`" in refusal


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
            venvName=nix_string("binary-nixpkgs"),
            fromNixpkgs="{ pyzmq = (import <nixpkgs> {}).python3Packages.pyzmq; }",
        )
        assert "pyzmq" in report


class TestAPackageThatCompiles:
    """`buildTools`, against two source distributions of PyPI.

    **`tests/sealed.nix` covers the same argument over a fixture, and this
    covers what a fixture cannot.** The backend here is the `setuptools` that
    each project declares, the source is the archive that the index serves, and
    `customize_compiler` of `setuptools._distutils` is what reads `CPPFLAGS`
    and `LDFLAGS`. The fixture backend mirrors that function, and a mirror is
    not the thing.

    `check-native.nix` names the tools, and `ddrn/PACKAGING-FACTS.md` records
    what each one is for.
    """

    async def test_two_packages_build_from_source_and_run(self, toolchain: Toolchain) -> None:
        report = await check(toolchain, "check-native.nix")

        # `psutil` holds C code and links nothing outside the interpreter, so
        # the compiler alone answers it.
        assert "psutil       7." in report
        # **`cffi` needs the header and the library of `libffi`.** The header
        # reaches the build through `CPPFLAGS` and the library through
        # `LDFLAGS`, and `sizeof` runs the library that the linker found.
        assert "sizeof(long)=8" in report

        # Neither wheel came from the index: `tool.uv.no-binary-package` names
        # both, so the planner chose the source distribution of each.
        assert ".so" in report

    async def test_the_compiler_reaches_no_environment(self, toolchain: Toolchain) -> None:
        # `-g0` of `../graph.nix` is what keeps it out. A `.so` with debug
        # information names every include directory of its build, so the scan
        # of the output would make the compiler and each `dev` output a
        # reference. `ddrn/PACKAGING-FACTS.md` measures what that costs.
        report = await check(toolchain, "check-native.nix")
        closure = await toolchain.closure_of(prefix_of(report))

        forbidden = ("gcc-wrapper", "binutils-", "linux-headers-", "pkg-config-")
        found = [name for name in closure if name.endswith("-dev") or name.startswith(forbidden)]
        assert found == [], found

        # `libffi` is a real dependency, so the rule above did not empty the
        # closure: the extension of `cffi` links against it.
        assert any(name.startswith("libffi-") for name in closure), closure


class TestTheProjectDeclaresItsBuildTools:
    """`[tool.ddrn.build-tools]`, and the argument that overrides it.

    **The build above proves the table works**, because `check-native.nix`
    names no tool and the two extensions still compile. These three tests cover
    what a build cannot say cheaply: that the two routes agree, that an
    argument replaces one entry, and what a wrong name says.

    Each one evaluates and builds nothing.
    """

    #: The same two entries as `projects/native-app/pyproject.toml`, in the
    #: spelling that a caller of `uv.nix` writes.
    #:
    #: **The `with` is necessary.** Nix does not evaluate the arguments of a
    #: call against each other, so a value here cannot name the `pkgs` of the
    #: same call. `uv.nix` takes the same nixpkgs by default.
    ARGUMENT = (
        "with (import <nixpkgs> {}); { psutil = [ stdenv.cc ]; cffi = [ stdenv.cc pkg-config libffi.dev libffi ]; }"
    )

    def planner(self, toolchain: Toolchain, **arguments: str) -> str:
        return toolchain.source(UV, "drvPath", project=nix_string(PROJECTS / "native-app"), **arguments)

    async def test_the_table_and_the_argument_give_one_planner(self, toolchain: Toolchain) -> None:
        """**The move to `pyproject.toml` changed no derivation.**

        A planner names its build tools through the document that it carries,
        so two calls that end at the same store paths end at the same `.drv`.
        This is the proof that the table is the argument, and not another
        route to a similar result.
        """
        declared = await toolchain.evaluate(self.planner(toolchain))
        given = await toolchain.evaluate(self.planner(toolchain, buildTools=self.ARGUMENT))
        assert declared == given

    async def test_an_argument_replaces_the_entry_that_it_names(self, toolchain: Toolchain) -> None:
        # An empty list is how a caller takes the tools of one package away,
        # and it leaves the entry of every other package alone.
        declared = await toolchain.evaluate(self.planner(toolchain))
        overridden = await toolchain.evaluate(self.planner(toolchain, buildTools="{ cffi = [ ]; }"))
        assert declared != overridden

    async def test_a_value_that_names_no_attribute_of_nixpkgs(self, toolchain: Toolchain, tmp_path: Path) -> None:
        # The message names the package, the value and where the value came
        # from. `stdenv.cxx` is the plausible misspelling of `stdenv.cc`.
        shutil.copytree(PROJECTS / "native-app", tmp_path, dirs_exist_ok=True)
        declaration = anyio.Path(tmp_path / "pyproject.toml")
        text = await declaration.read_text(encoding="utf-8")
        await declaration.write_text(text.replace('["stdenv.cc"]', '["stdenv.cxx"]'), encoding="utf-8")

        source = toolchain.source(UV, "drvPath", project=nix_string(tmp_path))
        refusal = await toolchain.evaluate_failing(source)
        assert "[tool.ddrn.build-tools] of native-app gives 'psutil'" in refusal
        assert "'stdenv.cxx', and nixpkgs holds no attribute of that name" in refusal


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
