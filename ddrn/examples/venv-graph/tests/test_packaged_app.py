"""The packaged uv project of `../projects/packaged-app`, without a network.

`../run-uv.sh` builds this project end to end. This module makes the same
decisions against the same `uv.lock`, and it downloads nothing.

**The project exists for what `pure-app` cannot reach.** `uv init --package`
makes a project that installs itself, so three things happen that a virtual
root never meets:

1. The lock file says `source = { editable = "." }`, so the project is a
   member of its own environment and it installs from its own tree.
2. `build-system.requires = ["hatchling", "editables"]` names two packages of
   six. **`uv.lock` records no build system**, so a backend reaches the graph
   only because a dependency group pins it, and the closure of that backend
   comes from the edges of the lock file.
3. `tool.uv.no-binary-package = ["mdurl"]` makes one package build from its
   source distribution although a wheel fits. uv keeps that setting in
   `pyproject.toml` and copies it into no lock file.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from ddrn_venv import Lock, LockError, build_requirements, build_system, resolve_lock
from packaging.requirements import Requirement

PROJECT = Path(__file__).resolve().parent.parent / "projects/packaged-app"

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

SELECTION: dict[str, list[str]] = {"packaged-app": []}


@pytest.fixture
def lock() -> Lock:
    return Lock.read(PROJECT / "uv.lock")


@pytest.fixture
def pyproject() -> dict[str, object]:
    with (PROJECT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def members(lock: Lock, selection: dict[str, list[str]], environment: dict[str, str]) -> set[str]:
    return {package.name for package in resolve_lock(lock, environment, selection).members}


class TestTheSelection:
    def test_the_environment_holds_the_project_and_its_closure(self, lock: Lock) -> None:
        assert members(lock, SELECTION, ENVIRONMENT) == {
            "packaged-app",
            "rich",
            "markdown-it-py",
            "mdurl",
            "pygments",
        }

    def test_the_project_installs_from_its_own_tree(self, lock: Lock) -> None:
        # `uv init --package` writes this, and `uv init --no-package` writes
        # `virtual` instead. `pure-app` is the second kind.
        root = next(package for package in lock.packages if package.name == "packaged-app")
        assert root.source == {"editable": "."}
        assert root.is_local
        assert not root.is_virtual

    def test_no_build_tool_reaches_the_environment(self, lock: Lock) -> None:
        installed = members(lock, SELECTION, ENVIRONMENT)
        assert not installed & {"hatchling", "editables", "installer", "flit-core", "pathspec", "pluggy"}


class TestTheBackendClosure:
    """PEP 517 says the frontend installs `requires` **and what those need**."""

    def test_the_declaration_names_two_packages(self, pyproject: dict[str, object]) -> None:
        backend, requirements = build_system(pyproject)
        assert backend == "hatchling.build"
        assert {requirement.name for requirement in requirements} == {"hatchling", "editables"}

    def test_the_closure_is_six(self, lock: Lock, pyproject: dict[str, object]) -> None:
        # `hatchling` needs four packages to import at all, and none of them is
        # in `build-system.requires`. A planner that installed the declaration
        # alone would fail with `ModuleNotFoundError: packaging`.
        _, requirements = build_system(pyproject)
        closure = build_requirements(lock, ENVIRONMENT, requirements)
        assert {requirement.name for requirement in closure} == {
            "editables",
            "hatchling",
            "packaging",
            "pathspec",
            "pluggy",
            "trove-classifiers",
        }

    def test_every_member_of_the_closure_is_an_exact_pin(self, lock: Lock, pyproject: dict[str, object]) -> None:
        _, requirements = build_system(pyproject)
        for requirement in build_requirements(lock, ENVIRONMENT, requirements):
            assert len(requirement.specifier) == 1
            assert str(requirement.specifier).startswith("==")

    def test_editables_is_needed_and_hatchling_does_not_declare_it(self, lock: Lock) -> None:
        # **Measured, and not assumed.** `hatchling.builders.wheel` reads
        # `from editables import EditableProject` in `build_editable_detection`,
        # and the metadata of hatchling 1.31.0 names four dependencies that do
        # not include it. So the project has to name it.
        hatchling = next(package for package in lock.packages if package.name == "hatchling")
        assert {edge.name for edge in hatchling.dependencies} == {
            "packaging",
            "pathspec",
            "pluggy",
            "trove-classifiers",
        }

    def test_a_backend_that_the_lock_does_not_hold_says_what_to_do(self, lock: Lock) -> None:
        with pytest.raises(LockError, match="uv add --group"):
            build_requirements(lock, ENVIRONMENT, [Requirement("setuptools")])


class TestNoBinaryPackage:
    """`tool.uv.no-binary-package`, which uv writes into no lock file."""

    def test_the_project_names_one(self, pyproject: dict[str, object]) -> None:
        tool = pyproject["tool"]
        assert isinstance(tool, dict)
        assert tool["uv"] == {"no-binary-package": ["mdurl"]}

    def test_the_lock_file_still_carries_the_wheel(self, lock: Lock) -> None:
        # The setting is a decision of the consumer, so uv records every
        # artefact either way and `uv.nix` enumerates both.
        mdurl = next(package for package in lock.packages if package.name == "mdurl")
        assert mdurl.wheels
        assert mdurl.sdist is not None

    def test_the_backend_of_that_source_distribution_is_pinned_too(self, lock: Lock) -> None:
        # `mdurl` builds through `flit_core`, so the group has to pin that as
        # well. This is the cost of the gap: one entry for each backend that
        # any source build reaches.
        assert any(package.name == "flit-core" for package in lock.packages)
