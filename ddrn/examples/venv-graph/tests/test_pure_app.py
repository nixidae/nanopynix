"""The real uv project of `../projects/pure-app`, resolved without a network.

`../run-uv.sh` builds this project end to end, and it downloads from PyPI to do
that. This module makes the same decisions against the same `uv.lock`, and it
downloads nothing, so it is the regression test that a change to the reader has
to keep passing.

**`projects/README.md` gives the `uv` commands that wrote the lock file.** Two
of those commands are what this module asserts about:

- `uv init --no-package` makes the root virtual, so no node of the graph builds
  it and `Resolution.members` leaves it out.
- `uv add --group ddrn installer` pins the wheel installer in a PEP 735
  dependency group. The lock file holds it, and the environment must not,
  because the selection of `uv.nix` enables no group.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from ddrn_venv import Lock, LockError, resolve_lock

PROJECT = Path(__file__).resolve().parent.parent / "projects/pure-app"

#: The target of `uv.nix`, which is `pkgs.python3`. That interpreter is 3.14,
#: and `.python-version` of the project says 3.13. Both are correct: the lock
#: file says `requires-python = ">=3.13"`, and a specifier is a range.
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
SELECTION: dict[str, list[str]] = {"pure-app": []}


@pytest.fixture
def lock() -> Lock:
    return Lock.read(PROJECT / "uv.lock")


def members(lock: Lock, selection: dict[str, list[str]], environment: dict[str, str]) -> set[str]:
    """The packages that a node of the graph builds, by name."""
    return {package.name for package in resolve_lock(lock, environment, selection).members}


class TestTheDefaultSelection:
    """What `../run-uv.sh` builds, and what `check-uv.nix` then asserts."""

    def test_the_environment_is_rich_and_its_closure(self, lock: Lock) -> None:
        assert members(lock, SELECTION, ENVIRONMENT) == {"rich", "markdown-it-py", "mdurl", "pygments"}

    def test_the_root_is_virtual_and_builds_nothing(self, lock: Lock) -> None:
        # `uv init --no-package` writes `source = { virtual = "." }`.
        root = next(package for package in lock.packages if package.name == "pure-app")
        assert root.is_virtual
        assert root.wheels == ()
        assert root.sdist is None

    def test_the_installer_is_pinned_and_is_not_installed(self, lock: Lock) -> None:
        # The whole build hangs on this: the lock file pins the tool that
        # installs every wheel, and the environment does not carry it.
        assert any(package.name == "installer" for package in lock.packages)
        assert "installer" not in members(lock, SELECTION, ENVIRONMENT)

    def test_every_artefact_is_a_pure_wheel(self, lock: Lock) -> None:
        # No node of this graph runs `auto-patchelf`, which is what makes this
        # the first project of the example.
        wheels = [wheel for package in lock.packages for wheel in package.wheels]
        assert wheels
        assert all(wheel.name.endswith("-py3-none-any.whl") for wheel in wheels)

    def test_every_artefact_carries_a_sha256(self, lock: Lock) -> None:
        # `uv.nix` passes `hash` to `fetchurl` with no translation, so the
        # spelling that uv writes has to be the spelling that Nix reads.
        files = [file for package in lock.packages for file in (*package.wheels, package.sdist) if file]
        assert files
        assert all(file.hash.startswith("sha256:") for file in files)


class TestTheGroup:
    """PEP 735 dependency groups, which the selection turns on and off."""

    def test_naming_the_group_adds_the_installer(self, lock: Lock) -> None:
        assert members(lock, {"pure-app": ["ddrn"]}, ENVIRONMENT) == {
            "rich",
            "markdown-it-py",
            "mdurl",
            "pygments",
            "installer",
        }


class TestRequiresPython:
    """`requires-python` of the lock file, against the target of the graph."""

    def test_a_target_above_the_floor_is_accepted(self, lock: Lock) -> None:
        assert members(lock, SELECTION, ENVIRONMENT)

    def test_a_target_below_the_floor_is_refused(self, lock: Lock) -> None:
        older = {**ENVIRONMENT, "python_full_version": "3.12.12", "python_version": "3.12"}
        with pytest.raises(LockError, match=re.escape(">=3.13")):
            members(lock, SELECTION, older)
