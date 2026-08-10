"""The uv workspace of `../projects/workspace`, without a network.

`../run-uv.sh` builds this workspace end to end. This module makes the same
decisions against the same `uv.lock`, and it downloads nothing.

**The workspace exists for what one project cannot reach.** A workspace holds
more than one local package under one lock file, so three things happen that
`packaged-app` never meets:

1. **One lock file resolves to more than one environment.** `uv.lock` records
   no selection, so the caller names the members. `uv.nix` defaults to every
   member, which is what `uv sync --all-packages` takes.
2. **Two local packages install together**, each an editable of its own
   directory, and one of them reads the other.
3. **A member redirects at its own directory.** `source = { editable =
   "packages/ws-core" }` names a directory under the project, so the PEP 660
   redirect has to name that directory and not the workspace.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from ddrn_venv import Lock, resolve_lock

PROJECT = Path(__file__).resolve().parent.parent / "projects/workspace"

#: The target of `uv.nix`, which is `pkgs.python3`.
ENVIRONMENT = {
    "implementation_name": "cpython",
    "implementation_version": "3.14.6",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.14.6",
    "python_version": "3.14",
    "sys_platform": "linux",
    "extra": "",
}

#: What `uv.nix` takes when the caller names no selection: every member of
#: `[manifest] members`, with no extra and no group.
EVERY_MEMBER: dict[str, list[str]] = {"workspace-root": [], "ws-cli": [], "ws-core": []}

#: One member of the two, which is the selection that has to differ.
ONE_MEMBER: dict[str, list[str]] = {"ws-core": []}


@pytest.fixture
def lock() -> Lock:
    return Lock.read(PROJECT / "uv.lock")


@pytest.fixture
def pyproject() -> dict[str, object]:
    with (PROJECT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def members(lock: Lock, selection: dict[str, list[str]], environment: dict[str, str]) -> set[str]:
    return {package.name for package in resolve_lock(lock, environment, selection).members}


class TestTheManifest:
    """`[manifest] members`, which uv writes for a workspace only."""

    def test_it_names_every_member_of_the_workspace(self, lock: Lock) -> None:
        # **These are package names, and not directories.**
        # `[tool.uv.workspace] members` of `pyproject.toml` holds the
        # directories, and the two lists do not agree entry for entry.
        assert set(lock.members) == {"workspace-root", "ws-cli", "ws-core"}

    def test_the_project_file_names_the_directories_instead(self, pyproject: dict[str, object]) -> None:
        tool = pyproject["tool"]
        assert isinstance(tool, dict)
        assert tool["uv"]["workspace"]["members"] == ["packages/ws-core", "packages/ws-cli"]

    def test_a_single_project_lock_file_has_no_manifest(self) -> None:
        # `uv.nix` falls back to the name of the project when this is empty, so
        # the default selection of a single project does not change.
        single = Lock.read(PROJECT.parent / "packaged-app/uv.lock")
        assert single.members == ()


class TestTheSelection:
    """One lock file, and one environment for each selection of it."""

    def test_every_member_gives_both_members_and_the_closure(self, lock: Lock) -> None:
        assert members(lock, EVERY_MEMBER, ENVIRONMENT) == {
            "ws-cli",
            "ws-core",
            "rich",
            "markdown-it-py",
            "mdurl",
            "pygments",
        }

    def test_one_member_gives_that_member_alone(self, lock: Lock) -> None:
        # `ws-core` depends on nothing, so this selection reaches no artefact
        # of the index at all. That is the proof that the selection decides the
        # environment, and the lock file does not.
        assert members(lock, ONE_MEMBER, ENVIRONMENT) == {"ws-core"}

    def test_the_virtual_root_installs_nothing(self, lock: Lock) -> None:
        # The root takes part in the resolution and ships nothing, so it is a
        # member of `[manifest]` and not a member of the environment.
        assert "workspace-root" in lock.members
        assert "workspace-root" not in members(lock, EVERY_MEMBER, ENVIRONMENT)

    def test_no_build_tool_reaches_the_environment(self, lock: Lock) -> None:
        installed = members(lock, EVERY_MEMBER, ENVIRONMENT)
        assert not installed & {"hatchling", "editables", "installer", "pathspec", "pluggy"}


class TestTheLocalMembers:
    """Each member is an editable of its own directory."""

    def test_each_member_names_the_directory_that_holds_it(self, lock: Lock) -> None:
        # **This is the value that the PEP 660 redirect has to carry.** A
        # single project says `.` here, and `uv.nix` then redirects at the
        # tree itself. A member says a directory under the project, and a
        # redirect that dropped it would write a `.pth` that names no package.
        sources = {package.name: package.source for package in lock.packages if package.is_local}
        assert sources == {
            "workspace-root": {"virtual": "."},
            "ws-cli": {"editable": "packages/ws-cli"},
            "ws-core": {"editable": "packages/ws-core"},
        }

    def test_one_member_depends_on_the_other(self, lock: Lock) -> None:
        cli = next(package for package in lock.packages if package.name == "ws-cli")
        assert {edge.name for edge in cli.dependencies} == {"rich", "ws-core"}

    def test_the_edge_between_members_is_not_an_artefact(self, lock: Lock) -> None:
        # A workspace edge resolves to a local directory, so no node of the
        # graph fetches it and `uv.nix` makes no `fetchurl` for it.
        core = next(package for package in lock.packages if package.name == "ws-core")
        assert core.wheels == ()
        assert core.sdist is None
