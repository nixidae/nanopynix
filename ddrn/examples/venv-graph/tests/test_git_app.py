"""The real uv project of `../projects/git-app`, resolved without a network.

`../run-uv.sh` builds this project end to end, and it clones `pypa/flit` to do
that. This module makes the same decisions against the same `uv.lock`, and it
fetches nothing, so it is the regression test that a change to the reader has
to keep passing.

**A git source is the one shape that carries no artefact.** Every other package
of every lock file of this example gives a `wheels` table, an `sdist` table or
both, and `uv.nix` makes one `fetchurl` for each row. `flit-core` here gives
neither, so the menu holds nothing for it:

    [[package]]
    name = "flit-core"
    version = "3.12.0"
    source = { git = "https://github.com/pypa/flit?subdirectory=flit_core&rev=3.12.0#1c8141…" }

Two consequences follow, and this module asserts both. The planner cannot
resolve this package out of the menu, so it takes the tree route instead. And
`uv.nix` has to fetch the repository during the evaluation, because a commit is
not a hash of the tree that the commit gives.

`test_git_and_url_sources.py` covers the parse of that one string, over the
fixture lock files of uv2nix. This module covers the lock file that `uv add`
wrote here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ddrn_venv import Lock, Package, resolve_lock

PROJECT = Path(__file__).resolve().parent.parent / "projects/git-app"

#: The target of `uv.nix`, which is `pkgs.python3`. `test_pure_app.py` gives
#: the reason that `.python-version` of the project says something else.
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

#: What `uv.nix` selects by default: the project, with no extra and no group.
SELECTION: dict[str, list[str]] = {"git-app": []}

#: The commit that `uv add` resolved for the tag `3.12.0`. uv writes it as the
#: fragment of the reference, and `uv-git.nix` fetches that commit and not the
#: tag, because a tag moves and a commit does not.
COMMIT = "1c8141722066ffe1497e511982feeea0f091ddb6"


@pytest.fixture
def lock() -> Lock:
    return Lock.read(PROJECT / "uv.lock")


def members(lock: Lock, selection: dict[str, list[str]], environment: dict[str, str]) -> set[str]:
    """The packages that a node of the graph builds, by name."""
    return {package.name for package in resolve_lock(lock, environment, selection).members}


@pytest.fixture
def flit_core(lock: Lock) -> Package:
    return next(package for package in lock.packages if package.name == "flit-core")


class TestTheGitSource:
    """The one package that comes from a repository, and not from an index."""

    def test_the_environment_is_that_one_package(self, lock: Lock) -> None:
        # `flit_core` declares `dependencies = []`, so the closure of this
        # project is one name. That keeps the proof about the source kind.
        assert members(lock, SELECTION, ENVIRONMENT) == {"flit-core"}

    def test_it_carries_no_artefact_at_all(self, flit_core: Package) -> None:
        # **This is what makes the git route necessary.** A planner that looked
        # for a wheel or an archive would find neither, and would report that
        # no artefact of the menu fits this target.
        assert flit_core.is_git
        assert flit_core.wheels == ()
        assert flit_core.sdist is None

    def test_the_reference_names_a_commit_and_a_subdirectory(self, flit_core: Package) -> None:
        # uv packs the URL, the query and the fragment into one string.
        # `../uv-git.nix` splits it, and `test_git_and_url_sources.py` runs
        # that file over this reference and over the shapes of uv2nix.
        reference: str = flit_core.source["git"]
        assert reference.startswith("https://github.com/pypa/flit?")
        assert reference.endswith(f"#{COMMIT}")
        assert "subdirectory=flit_core" in reference

    def test_the_root_is_virtual_and_builds_nothing(self, lock: Lock) -> None:
        # `uv init --no-package` writes `source = { virtual = "." }`, so the
        # environment is the closure of the dependency and nothing else.
        root = next(package for package in lock.packages if package.name == "git-app")
        assert root.is_virtual
        assert "git-app" not in members(lock, SELECTION, ENVIRONMENT)

    def test_the_installer_is_pinned_and_is_not_installed(self, lock: Lock) -> None:
        assert any(package.name == "installer" for package in lock.packages)
        assert "installer" not in members(lock, SELECTION, ENVIRONMENT)


class TestWhatTheLockFileDoesNotSay:
    """The two things that `uv.lock` leaves to the reader, for a git source."""

    def test_it_records_no_build_system(self, lock: Lock) -> None:
        # **The backend of the checkout is inside the checkout.** `flit_core`
        # builds itself, with `backend-path = ["."]` and an empty `requires`,
        # and no key of this lock file says so. `scripts/build-local.py` reads
        # the declaration out of the tree that the graph fetched.
        text = (PROJECT / "uv.lock").read_text(encoding="utf-8")
        assert "build-system" not in text
        assert "backend-path" not in text

    def test_it_records_no_hash_of_the_tree(self, flit_core: Package) -> None:
        # **This is what costs the evaluation a fetch.** A commit identifies a
        # tree, and it is not a hash of one, so there is no fixed-output
        # derivation to instantiate. `ddrn/PACKAGING-FACTS.md` records the
        # whole cost, and the one line that would remove it.
        assert set(flit_core.source) == {"git"}
