"""Packaging decisions, made by `packaging` and against one menu of artefacts.

Every planner of this example shares this module. The outer planner resolves
the roots of the environment, and the planner of a source distribution
resolves the backend that the archive asks for. Both ask the same two
questions: which artefact satisfies this requirement, and does this target
accept it.

None of this is expressed in the Nix language. `packaging` is the reference
implementation of the packaging PEPs, and it answers:

- PEP 508 environment markers and requirements;
- PEP 440 version specifiers;
- PEP 503 name normalisation;
- PEP 425 and PEP 600 compatibility tags.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.tags import Tag
from packaging.utils import canonicalize_name, parse_wheel_filename

from ._env import env

if TYPE_CHECKING:
    from collections.abc import Iterable

#: The backend that PEP 517 names when a project declares no `build-backend`.
FALLBACK_BACKEND = "setuptools.build_meta:__legacy__"

#: The oldest and the newest glibc minor version that a `manylinux_2_Y` tag
#: may name. PEP 600 defines the scheme, and 2.17 is the floor that
#: `manylinux2014` corresponds to.
GLIBC_MINORS = range(17, 40)


@dataclass(frozen=True, slots=True)
class Artifact:
    """One row of the lock file, with the two store paths that Nix gave it.

    `drv` is the store path of the `fetchurl` derivation, and `out` is the
    store path of its output. The Nix half discards the dependency between the
    two, so Nix instantiates every artefact and downloads none of them.
    `graph.nix` puts the dependency back for the ones a planner chooses.

    `path` is the alternative: an artefact that is already present, because
    some derivation named it as an input. The planner of a source distribution
    receives its own archive that way.
    """

    package: str
    version: str
    filename: str
    kind: str
    drv: str = ""
    out: str = ""
    path: str = ""
    marker: str | None = None
    #: The planner of this source distribution, which `default.nix`
    #: instantiated. Only a row of kind `sdist` carries one.
    planner_drv: str = ""
    planner_out: str = ""

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> Artifact:
        return cls(
            package=row["package"],
            version=row["version"],
            filename=row["filename"],
            kind=row["kind"],
            drv=row.get("drv", ""),
            out=row.get("out", ""),
            path=row.get("path", ""),
            marker=row.get("marker"),
            planner_drv=row.get("plannerDrv", ""),
            planner_out=row.get("plannerOut", ""),
        )

    @property
    def node_name(self) -> str:
        """The name of the node that builds this artefact."""
        return f"{self.package}-{self.version}"

    @property
    def is_platform_wheel(self) -> bool:
        """Whether this wheel carries a binary, which its tag already says.

        A wheel tagged `any` runs on every platform, so it holds no compiled
        code. Any other tag names one platform, and the node that installs it
        needs the loader and the libraries of this system.
        """
        if self.kind != "wheel":
            return False
        _, _, _, wheel_tags = parse_wheel_filename(self.filename)
        return not any(tag.platform == "any" for tag in wheel_tags)

    def as_plan(self) -> dict[str, str]:
        """The artefact, in the shape that `graph.nix` reads.

        `filename` is the name that the index gave this artefact.
        `install-wheel.sh` needs it, because the store path of the fetch has a
        hash in front of that name and `installer` reads the name.
        """
        # `package` and `version` travel too, because a node of kind `sdist`
        # is a planner and has to name the distribution that it plans.
        common = {"filename": self.filename, "package": self.package, "version": self.version}
        if self.planner_drv:
            common |= {"plannerDrv": self.planner_drv, "plannerOut": self.planner_out}
        if self.path:
            return {**common, "path": self.path}
        return {**common, "drv": self.drv, "out": self.out}


@dataclass(frozen=True, slots=True)
class Target:
    """What this environment runs on, in the terms that `packaging` uses.

    A planner runs on one machine and plans for another, so the target is a
    value and not a property of the interpreter that reads this file.
    """

    #: The PEP 425 tags this environment accepts.
    tags: frozenset[Tag]
    #: The PEP 508 marker environment.
    markers: dict[str, str]

    def accepts(self, marker: str | None) -> bool:
        """Whether a PEP 508 marker holds for this target."""
        if marker is None:
            return True
        return Marker(marker).evaluate(self.markers)


def target_tags(python_version: str, platform: str) -> set[Tag]:
    """The tags this environment accepts, as PEP 425 and PEP 600 define them.

    Built by hand rather than by `packaging.tags.sys_tags`, because a planner
    plans for a target that is not the machine it runs on.
    """
    major, minor = python_version.split(".")
    interpreter = f"cp{major}{minor}"

    platforms = [platform]
    if platform.startswith("linux_"):
        arch = platform.removeprefix("linux_")
        # A manylinux wheel names the oldest glibc it runs against, so every
        # version at or below the target is acceptable.
        platforms += [f"manylinux_2_{glibc}_{arch}" for glibc in GLIBC_MINORS]
        platforms += [f"manylinux2014_{arch}"]

    tags = {Tag(interpreter, interpreter, plat) for plat in platforms}
    tags |= {Tag(interpreter, "abi3", plat) for plat in platforms}
    tags |= {Tag(f"py{major}", "none", plat) for plat in [*platforms, "any"]}
    tags |= {Tag(f"py{major}{minor}", "none", "any")}
    return tags


def wheel_rank(artifact: Artifact, accepted: frozenset[Tag]) -> int | None:
    """How well a wheel fits, lower being better, or ``None`` for no fit.

    A platform-specific wheel outranks a pure one, which is what pip does:
    `charset_normalizer` ships a compiled wheel and an `any` fallback, and the
    compiled one is the one to install.
    """
    _, _, _, wheel_tags = parse_wheel_filename(artifact.filename)
    if not any(tag in accepted for tag in wheel_tags):
        return None
    return 1 if any(tag.platform == "any" for tag in wheel_tags) else 0


def choose(
    artifacts: Iterable[Artifact],
    requirement: Requirement,
    target: Target,
) -> Artifact | None:
    """The one artefact that satisfies `requirement` on this target.

    A wheel wins over a source distribution, and among wheels the best tag
    wins. A source distribution is the answer only when no wheel fits, which
    is what makes the build path of this example run.

    **The name of a requirement is not the name of a package until PEP 503
    normalises it.** A project asks for `flit_core`, and the index calls it
    `flit-core`. The version range is PEP 440, and `packaging` answers both.
    """
    package = canonicalize_name(requirement.name)
    best_wheel: tuple[int, Artifact] | None = None
    sdist: Artifact | None = None

    for artifact in artifacts:
        if canonicalize_name(artifact.package) != package:
            continue
        if artifact.version not in requirement.specifier:
            print(
                f"plan: {artifact.filename}: {artifact.version} is outside {requirement}",
                file=sys.stderr,
            )
            continue
        if not target.accepts(artifact.marker):
            print(f"plan: {artifact.filename}: marker excludes this target", file=sys.stderr)
            continue
        if artifact.kind == "sdist":
            sdist = artifact
            continue
        score = wheel_rank(artifact, target.tags)
        if score is None:
            continue
        if best_wheel is None or score < best_wheel[0]:
            best_wheel = (score, artifact)

    if best_wheel is not None:
        return best_wheel[1]
    return sdist


def target_from_environ() -> Target:
    """The target that the Nix half named, read from the environment.

    Every planner of this example reads it the same way, because `default.nix`
    gives every planner the same three variables.
    """
    python_version = env("PYTHON_VERSION")
    platform = env("TARGET_PLATFORM")
    return Target(
        tags=frozenset(target_tags(python_version, platform)),
        markers={
            "sys_platform": env("TARGET_SYS_PLATFORM"),
            "platform_system": "Linux",
            "os_name": "posix",
            "python_version": python_version,
            "platform_machine": platform.rsplit("_", 1)[-1],
            "implementation_name": "cpython",
        },
    )


def build_system(pyproject: dict[str, Any]) -> tuple[str, list[Requirement]]:
    """The PEP 517 backend of a project, and what that backend needs.

    A source distribution and a local project declare this the same way, so
    both planners read it with this function. PEP 517 gives the fallback for a
    project that declares no backend.
    """
    table = pyproject.get("build-system", {})
    backend = table.get("build-backend", FALLBACK_BACKEND)
    return backend, [Requirement(text) for text in table.get("requires", [])]
