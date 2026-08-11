"""The real uv project of `../projects/local-index`, resolved with no network.

`../run-uv.sh` builds this project end to end, and it downloads nothing for
either dependency. This module makes the same decisions against the same
`uv.lock`, and it is the regression test that a change to the reader has to
keep passing.

**The location of an artefact here has three parts, and the row holds the
last one.** uv puts the directory of the registry on the source, and the name
of the file on the row:

    [[package]]
    name = "stocked"
    source = { registry = "index" }
    wheels = [ { path = "stocked-1.0.0-py3-none-any.whl" } ]

uv joins the root of the workspace, the path of the registry and the path of
the row, in `to_registry_wheel` and in `to_source_dist`
(`crates/uv-resolver/src/lock/mod.rs`). A reader that joined the row to the
project alone would name a file that does not exist, so this is a divergence
that fails loudly rather than one that installs the wrong file.

`test_vendored_app.py` covers the other kind of file that this machine holds.
There the source *is* the file, and the row carries the name and the hash.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from ddrn_venv import Lock, Package, resolve_lock

PROJECT = Path(__file__).resolve().parent.parent / "projects/local-index"

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
SELECTION: dict[str, list[str]] = {"local-index": []}

#: The directory that `pyproject.toml` names as the default index, and that
#: every row of these two packages is relative to.
REGISTRY = "index"

VERSION = "1.0.0"


@pytest.fixture
def lock() -> Lock:
    return Lock.read(PROJECT / "uv.lock")


def members(lock: Lock, selection: dict[str, list[str]], environment: dict[str, str]) -> set[str]:
    """The packages that a node of the graph builds, by name."""
    return {package.name for package in resolve_lock(lock, environment, selection).members}


@pytest.fixture
def stocked(lock: Lock) -> Package:
    return next(package for package in lock.packages if package.name == "stocked")


@pytest.fixture
def crated(lock: Lock) -> Package:
    return next(package for package in lock.packages if package.name == "crated")


class TestTheRegistryIsADirectory:
    """The two packages that come out of `index/`."""

    def test_the_environment_is_those_two_packages(self, lock: Lock) -> None:
        # Neither distribution declares a dependency, so the closure of this
        # project is these two names. That keeps the proof about the location.
        assert members(lock, SELECTION, ENVIRONMENT) == {"stocked", "crated"}

    def test_each_source_names_the_directory_and_not_the_file(self, stocked: Package, crated: Package) -> None:
        # **The source is a registry, and the value is a path.** uv reads a
        # value with no scheme as a directory (`RegistrySourceWire`), so this
        # is not a package of an index of the network.
        for package in (stocked, crated):
            assert package.source == {"registry": REGISTRY}
            assert not package.is_path
            assert not package.is_local
            assert not package.is_url
            assert not package.is_git

    def test_the_wheel_row_names_the_file_under_that_directory(self, stocked: Package) -> None:
        assert stocked.sdist is None
        assert len(stocked.wheels) == 1
        wheel = stocked.wheels[0]
        assert wheel.path == "stocked-1.0.0-py3-none-any.whl"
        assert wheel.url == ""
        assert wheel.filename == ""
        # The file that the graph reads is the join of the three parts, and
        # `uv.nix` makes that join. This asserts that the file is there.
        assert (PROJECT / REGISTRY / wheel.path).is_file()

    def test_the_archive_row_names_the_file_under_that_directory(self, crated: Package) -> None:
        assert crated.wheels == ()
        assert crated.sdist is not None
        assert crated.sdist.path == "crated-1.0.0.tar.gz"
        assert crated.sdist.url == ""
        assert crated.sdist.filename == ""
        assert (PROJECT / REGISTRY / crated.sdist.path).is_file()

    def test_the_row_alone_names_no_file_of_the_project(self, stocked: Package, crated: Package) -> None:
        # **This is the failure that the three-part join avoids.** A reader
        # that took the row as a path of the project would look for these two
        # files at the top of the project, and neither one is there.
        assert not (PROJECT / stocked.wheels[0].path).exists()
        assert crated.sdist is not None
        assert not (PROJECT / crated.sdist.path).exists()

    def test_neither_row_carries_a_hash(self, stocked: Package, crated: Package) -> None:
        # A flat registry of a directory serves the file itself, so uv records
        # no digest. `uv.nix` copies the file with `builtins.path`, which needs
        # none, and no fetch of the menu holds one of these two rows.
        assert stocked.wheels[0].hash == ""
        assert crated.sdist is not None
        assert crated.sdist.hash == ""

    def test_the_key_of_a_row_identifies_it(self, stocked: Package) -> None:
        # The menu is keyed by this string, and a row with no URL falls back
        # to the path. Two rows of one lock file never share it.
        assert stocked.wheels[0].key == "stocked-1.0.0-py3-none-any.whl"

    def test_both_carry_the_version_of_the_file(self, stocked: Package, crated: Package) -> None:
        assert stocked.version == VERSION
        assert crated.version == VERSION


class TestWhatSurroundsThoseTwo:
    """The root, the second index, and the backend of the archive."""

    def test_the_root_is_virtual_and_builds_nothing(self, lock: Lock) -> None:
        root = next(package for package in lock.packages if package.name == "local-index")
        assert root.is_virtual
        assert "local-index" not in members(lock, SELECTION, ENVIRONMENT)

    def test_the_group_comes_from_the_second_index(self, lock: Lock) -> None:
        # **`pyproject.toml` names two indexes**, and the local one is the
        # default. So the two tools of the `ddrn` group still come from PyPI,
        # and each one carries a URL and a hash like any other artefact.
        for name in ("flit-core", "installer"):
            package = next(item for item in lock.packages if item.name == name)
            assert package.source == {"registry": "https://pypi.org/simple"}
            assert package.wheels[0].url.startswith("https://")
            assert package.wheels[0].hash.startswith("sha256:")

    def test_the_backend_is_pinned_and_is_not_installed(self, lock: Lock) -> None:
        installed = members(lock, SELECTION, ENVIRONMENT)
        for name in ("flit-core", "installer"):
            assert name not in installed

    def test_the_lock_file_records_no_absolute_path(self, lock: Lock) -> None:
        # **A lock file of this repository has to read the same on every
        # machine.** uv writes the absolute path of a local index into
        # `[package.metadata]` when a `tool.uv.sources` entry names that index
        # by name. `projects/README.md` gives the command that avoids it.
        text = (PROJECT / "uv.lock").read_text(encoding="utf-8")
        assert "file:///" not in text
        assert str(PROJECT) not in text

    def test_the_index_of_the_project_is_flat_and_is_the_default(self) -> None:
        # The reader takes none of this: `uv.lock` already holds the answer.
        # This asserts the shape that wrote the lock file, so that a reader of
        # the test knows which uv command to repeat.
        with (PROJECT / "pyproject.toml").open("rb") as handle:
            document = tomllib.load(handle)
        indexes = document["tool"]["uv"]["index"]
        local = next(entry for entry in indexes if entry["name"] == "local")
        assert local["url"] == REGISTRY
        assert local["format"] == "flat"
        assert local["default"] is True
