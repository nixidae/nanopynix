"""The real uv project of `../projects/vendored-app`, resolved with no network.

`../run-uv.sh` builds this project end to end, and it downloads nothing for
either dependency. This module makes the same decisions against the same
`uv.lock`, and it is the regression test that a change to the reader has to
keep passing.

**A `path` source is the one shape whose artefact has no address.** Every
package of an index gives a `url` on each row of `wheels` and on `sdist`. Here
the location is on the source, and the row carries the file name and the hash:

    [[package]]
    name = "vendored"
    source = { path = "vendor/vendored-1.0.0-py3-none-any.whl" }
    wheels = [ { filename = "vendored-1.0.0-py3-none-any.whl", hash = "sha256:7353…" } ]

    [[package]]
    name = "archived"
    source = { path = "vendor/archived-1.0.0.tar.gz" }
    sdist = { hash = "sha256:9207…" }

The two entries are the two shapes that the kind takes, and this module asserts
both. `uv.nix` copies each file into the store with `builtins.path`, so the
wheel installs and the archive builds through its backend. Nothing else about
the route changes: the menu holds a row for each file, and the planner picks
one out of the menu the way it picks an artefact of an index.

`test_git_and_url_sources.py` covers the two kinds that name a place on the
network. This module covers the kind that names a place on this machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ddrn_venv import Lock, Package, resolve_lock

PROJECT = Path(__file__).resolve().parent.parent / "projects/vendored-app"

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
SELECTION: dict[str, list[str]] = {"vendored-app": []}

#: Both files of `vendor/` carry this version, and `uv.lock` pins it.
VERSION = "1.0.0"


@pytest.fixture
def lock() -> Lock:
    return Lock.read(PROJECT / "uv.lock")


def members(lock: Lock, selection: dict[str, list[str]], environment: dict[str, str]) -> set[str]:
    """The packages that a node of the graph builds, by name."""
    return {package.name for package in resolve_lock(lock, environment, selection).members}


@pytest.fixture
def vendored(lock: Lock) -> Package:
    return next(package for package in lock.packages if package.name == "vendored")


@pytest.fixture
def archived(lock: Lock) -> Package:
    return next(package for package in lock.packages if package.name == "archived")


class TestTheTwoPathSources:
    """The wheel and the archive that `vendor/` holds."""

    def test_the_environment_is_those_two_packages(self, lock: Lock) -> None:
        # Neither distribution declares a dependency, so the closure of this
        # project is these two names. That keeps the proof about the kind.
        assert members(lock, SELECTION, ENVIRONMENT) == {"vendored", "archived"}

    def test_each_source_names_a_path_and_nothing_else(self, vendored: Package, archived: Package) -> None:
        # **This is what makes the copy necessary.** A fetch needs an address,
        # and there is none. `uv.nix` reads the path off the source, joins it
        # to the directory of the project, and calls `builtins.path`.
        assert vendored.is_path
        assert archived.is_path
        assert set(vendored.source) == {"path"}
        assert set(archived.source) == {"path"}
        assert vendored.source["path"] == "vendor/vendored-1.0.0-py3-none-any.whl"
        assert archived.source["path"] == "vendor/archived-1.0.0.tar.gz"

    def test_the_wheel_row_gives_a_file_name_and_no_url(self, vendored: Package) -> None:
        # uv writes the name here and writes it nowhere else, so `uv.nix` takes
        # the name of the store path from this key. The path of the source
        # names the same file, and the two agree.
        assert vendored.sdist is None
        assert len(vendored.wheels) == 1
        wheel = vendored.wheels[0]
        assert wheel.filename == "vendored-1.0.0-py3-none-any.whl"
        assert wheel.url == ""
        assert wheel.path == ""
        assert wheel.hash.startswith("sha256:")

    def test_the_archive_row_gives_the_hash_alone(self, archived: Package) -> None:
        # **The `sdist` table of a `path` source carries no file name.** So the
        # name of the store path comes off the path of the source, which is the
        # same rule that a `url` source takes. `uv.nix` has one `fileName` for
        # both, and `ddrn/PACKAGING-FACTS.md` gives the three shapes.
        assert archived.wheels == ()
        assert archived.sdist is not None
        assert archived.sdist.filename == ""
        assert archived.sdist.url == ""
        assert archived.sdist.path == ""
        assert archived.sdist.hash.startswith("sha256:")

    def test_both_carry_the_version_of_the_file(self, vendored: Package, archived: Package) -> None:
        assert vendored.version == VERSION
        assert archived.version == VERSION


class TestWhatSurroundsThoseTwo:
    """The root, and the two packages that build the archive."""

    def test_the_root_is_virtual_and_builds_nothing(self, lock: Lock) -> None:
        # `uv init --no-package` writes `source = { virtual = "." }`, so the
        # environment is the closure of the dependencies and nothing else.
        root = next(package for package in lock.packages if package.name == "vendored-app")
        assert root.is_virtual
        assert "vendored-app" not in members(lock, SELECTION, ENVIRONMENT)

    def test_the_backend_is_pinned_and_is_not_installed(self, lock: Lock) -> None:
        # **`flit-core` builds `archived`, and it stays out of the
        # environment.** `uv.lock` pins it in the `ddrn` dependency group, and
        # the selection enables no group. The graph installs it into the build
        # of `archived` only, which `../scripts/check-vendored.py` proves.
        installed = members(lock, SELECTION, ENVIRONMENT)
        for name in ("flit-core", "installer"):
            assert any(package.name == name for package in lock.packages)
            assert name not in installed

    def test_the_lock_file_records_no_build_system(self, lock: Lock) -> None:
        # The backend of the archive is inside the archive, so a planner of
        # `archived` reads `pyproject.toml` out of the file that it received.
        # `test_local_sdist.py` covers that read.
        text = (PROJECT / "uv.lock").read_text(encoding="utf-8")
        assert "build-system" not in text
