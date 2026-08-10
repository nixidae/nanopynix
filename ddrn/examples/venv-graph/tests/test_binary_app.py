"""The binary-wheel uv project of `../projects/binary-app`, without a network.

`../run-uv.sh` builds this project end to end, twice. This module makes the
same decisions against the same `uv.lock`, and it downloads nothing.

**The project exists for what no other project of `../projects` reaches.**
`pyzmq` ships wheels that hold compiled code, so two things happen here that a
pure wheel never meets:

1. **The wheel needs `auto-patchelf`.** A manylinux wheel links against the
   loader and the libraries of a generic Linux, and NixOS has neither at the
   path the wheel names. The planner marks the node, because `packaging`
   already parsed the tag.
2. **The wheel is a stable-ABI one.** `pyzmq` 27.1.0 ships `cp312-abi3` for
   Linux and no `cp313` or `cp314` wheel at all, so the tag of the wheel names
   an interpreter that is not the target.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ddrn_venv import Artifact, Lock, Target, choose, resolve_lock, target_tags
from packaging.requirements import Requirement

PROJECT = Path(__file__).resolve().parent.parent / "projects/binary-app"

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

SELECTION: dict[str, list[str]] = {"binary-app": []}


@pytest.fixture
def lock() -> Lock:
    return Lock.read(PROJECT / "uv.lock")


@pytest.fixture
def target() -> Target:
    return Target(
        tags=tuple(target_tags("3.14", "linux_x86_64")),
        markers=ENVIRONMENT,
        python_version="3.14",
        platform="linux_x86_64",
    )


def artifacts(lock: Lock, name: str) -> list[Artifact]:
    """Every artefact of one package, in the shape that `choose` reads."""
    package = next(item for item in lock.packages if item.name == name)
    rows = [
        Artifact(package=name, version=package.version or "", filename=file.name, kind="wheel")
        for file in package.wheels
    ]
    if package.sdist is not None:
        rows.append(Artifact(package=name, version=package.version or "", filename=package.sdist.name, kind="sdist"))
    return rows


class TestTheSelection:
    def test_the_environment_holds_pyzmq_alone(self, lock: Lock) -> None:
        installed = {package.name for package in resolve_lock(lock, ENVIRONMENT, SELECTION).members}
        assert installed == {"pyzmq"}

    def test_cffi_is_in_the_lock_and_not_in_the_environment(self, lock: Lock) -> None:
        # `pyzmq` needs `cffi` on PyPy only, and the edge carries the marker
        # that says so. The lock file is a union over every environment it
        # supports, so `cffi` is in the file and not in this environment.
        assert any(package.name == "cffi" for package in lock.packages)
        installed = {package.name for package in resolve_lock(lock, ENVIRONMENT, SELECTION).members}
        assert "cffi" not in installed


class TestTheWheel:
    """PEP 384 and PEP 600, against the wheels that `pyzmq` really ships."""

    def test_the_chosen_wheel_is_the_stable_abi_one(self, lock: Lock, target: Target) -> None:
        chosen = choose(artifacts(lock, "pyzmq"), Requirement("pyzmq==27.1.0"), target)
        assert chosen is not None
        assert chosen.kind == "wheel"
        assert chosen.filename == "pyzmq-27.1.0-cp312-abi3-manylinux_2_26_x86_64.manylinux_2_28_x86_64.whl"

    def test_it_ships_no_wheel_for_this_interpreter(self, lock: Lock) -> None:
        # **This is why the stable ABI matters here.** A tag set that held
        # `cp314-abi3` alone matched nothing, and the planner built the source
        # distribution, which needs a C compiler and `libzmq`.
        names = [file.name for file in next(p for p in lock.packages if p.name == "pyzmq").wheels]
        assert not [name for name in names if "cp314-cp314-" in name]
        assert [name for name in names if "cp312-abi3" in name]

    def test_the_chosen_wheel_needs_auto_patchelf(self, lock: Lock, target: Target) -> None:
        chosen = choose(artifacts(lock, "pyzmq"), Requirement("pyzmq==27.1.0"), target)
        assert chosen is not None
        # `graph.nix` reads this and gives the node the loader and the
        # libraries of this system. A pure wheel gets neither.
        assert chosen.is_platform_wheel

    def test_a_source_distribution_is_there_and_loses(self, lock: Lock) -> None:
        # The fallback exists, so the wheel winning is a decision and not the
        # only option.
        assert next(p for p in lock.packages if p.name == "pyzmq").sdist is not None
