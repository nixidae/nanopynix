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

import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.tags import Tag, compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

from ._env import env

if TYPE_CHECKING:
    from collections.abc import Container, Iterable, Sequence

    from packaging.utils import BuildTag

#: The backend that PEP 517 names when a project declares no `build-backend`.
FALLBACK_BACKEND = "setuptools.build_meta:__legacy__"

#: The oldest and the newest glibc minor version that a `manylinux_2_Y` tag
#: may name. PEP 600 defines the scheme, and 2.17 is the floor that
#: `manylinux2014` corresponds to.
GLIBC_MINORS = range(17, 40)

#: Every key that PEP 508 defines for a marker environment.
#:
#: **A key that a planner does not pass describes the machine that plans.**
#: `Marker.evaluate` of `packaging` merges what a caller gives over
#: `default_environment()`, and `default_environment()` reads the running
#: interpreter. A planner plans for another machine, so it passes all of them
#: and `target_from_environ` refuses a set that is short.
#: `ddrn/PACKAGING-FACTS.md` holds the measurement.
PEP508_KEYS = frozenset(
    {
        "implementation_name",
        "implementation_version",
        "os_name",
        "platform_machine",
        "platform_python_implementation",
        "platform_release",
        "platform_system",
        "platform_version",
        "python_full_version",
        "python_version",
        "sys_platform",
    }
)


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
    #: Where the project lives inside this archive, when it is not at the top
    #: of it. `uv.lock` writes this for a URL source, and one archive of the
    #: index may then hold more than one project.
    subdirectory: str = ""
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
            subdirectory=row.get("subdirectory", ""),
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
        if self.subdirectory:
            common |= {"subdirectory": self.subdirectory}
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

    #: The PEP 425 tags this environment accepts, best fit first. The order is
    #: what ranks a wheel, so this is a sequence and not a set.
    tags: tuple[Tag, ...]
    #: The PEP 508 marker environment. It holds every key of `PEP508_KEYS`.
    markers: dict[str, str]
    #: The major and the minor version, as `3.13`.
    python_version: str
    #: The platform tag of a wheel, as `linux_x86_64`.
    platform: str

    def accepts(self, marker: str | None) -> bool:
        """Whether a PEP 508 marker holds for this target."""
        if marker is None:
            return True
        return Marker(marker).evaluate(self.markers)


def target_platforms(platform: str) -> list[str]:
    """Every platform tag that a wheel may carry for this target, best first.

    **`packaging.tags.platform_tags` reads the glibc of the machine that calls
    it**, so this list is the one part that a planner has to state itself. PEP
    600 says a `manylinux_2_Y` wheel runs against every glibc at or above 2.Y,
    so each version at or below the target fits and the newest one is the
    closest fit.
    """
    if not platform.startswith("linux_"):
        return [platform]
    arch = platform.removeprefix("linux_")
    return [
        *[f"manylinux_2_{glibc}_{arch}" for glibc in reversed(GLIBC_MINORS)],
        # The legacy alias of `manylinux_2_17`, which PEP 600 keeps valid.
        f"manylinux2014_{arch}",
        platform,
    ]


def target_tags(python_version: str, platform: str) -> list[Tag]:
    """The tags this environment accepts, in the order that pip prefers them.

    **`packaging` computes this, and this function only states the target.**
    `packaging.tags.sys_tags` describes the machine that calls it, and a
    planner plans for a machine that it is not running on. `cpython_tags` and
    `compatible_tags` take the version and the platforms as arguments, which is
    the same computation against a stated target. That is the same trap, and
    the same escape, as `Marker.evaluate` with an explicit environment.

    PEP 384, PEP 425 and PEP 600 all live in there. A hand-written tag set
    missed the stable ABI: `pyzmq` 27.1.0 ships one `cp312-abi3` wheel for
    Linux and no `cp313` or `cp314` wheel, and a set that held `cp314-abi3`
    alone found no wheel and built the source distribution instead.
    """
    major, minor = (int(part) for part in python_version.split("."))
    platforms = target_platforms(platform)
    return [
        *cpython_tags(python_version=(major, minor), platforms=platforms),
        *compatible_tags(python_version=(major, minor), interpreter=f"cp{major}{minor}", platforms=platforms),
    ]


def build_tag(artifact: Artifact) -> BuildTag:
    """The PEP 427 build tag of a wheel, which breaks a tie between two of them.

    **Two wheels of one distribution may carry the same tags.** The build tag
    is what separates them, and the binary distribution format says how to read
    it: "Sort as an empty tuple if unspecified, else sort as a two-item tuple
    with the first item being the initial digits as an int". `packaging` returns
    exactly that, and a higher tuple wins. uv breaks the same tie with the same
    value, in `Package::find_best_wheel`.
    """
    _, _, tag, _ = parse_wheel_filename(artifact.filename)
    return tag


def wheel_rank(artifact: Artifact, accepted: Sequence[Tag]) -> int | None:
    """How well a wheel fits, lower being better, or ``None`` for no fit.

    The rank is the position of the best tag in the list that `target_tags`
    returns, so the preference is the one that `packaging` states and not one
    that this file invents. A wheel built for exactly this interpreter
    therefore beats a stable-ABI wheel, which beats a pure one.
    `charset_normalizer` is the case that the last step answers: it ships a
    compiled wheel and an `any` fallback, and the compiled one is the one to
    install.
    """
    _, _, _, wheel_tags = parse_wheel_filename(artifact.filename)
    ranks = [index for index, tag in enumerate(accepted) if tag in wheel_tags]
    return min(ranks) if ranks else None


def choose(
    artifacts: Iterable[Artifact],
    requirement: Requirement,
    target: Target,
    no_binary: Container[str] = frozenset(),
) -> Artifact | None:
    """The one artefact that satisfies `requirement` on this target.

    A wheel wins over a source distribution, and among wheels the best tag
    wins. Two wheels of one tag are separated by the build tag, and the higher
    one wins. `Package::to_dist` and `Package::find_best_wheel` of uv take the
    same two steps in the same order. A source distribution is the answer only
    when no wheel fits, which is what makes the build path of this example run.

    `no_binary` reverses that for the packages it names, and it is the same
    decision that `tool.uv.no-binary-package` makes. **uv keeps that setting in
    `pyproject.toml` and writes it into no lock file**, so `uv.nix` reads it
    where uv put it and passes it here.

    **The name of a requirement is not the name of a package until PEP 503
    normalises it.** A project asks for `flit_core`, and the index calls it
    `flit-core`. The version range is PEP 440, and `packaging` answers both.
    """
    package = canonicalize_name(requirement.name)
    best_wheel: tuple[int, BuildTag, Artifact] | None = None
    sdist: Artifact | None = None
    binary = package not in no_binary

    for artifact in artifacts:
        if canonicalize_name(artifact.package) != package:
            continue
        if artifact.kind == "wheel" and not binary:
            print(f"plan: {artifact.filename}: no-binary-package names {package}", file=sys.stderr)
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
        # A lower rank is a closer tag, and a higher build tag wins a tie.
        tag = build_tag(artifact)
        if best_wheel is None or score < best_wheel[0] or (score == best_wheel[0] and tag > best_wheel[1]):
            best_wheel = (score, tag, artifact)

    if best_wheel is not None:
        return best_wheel[2]
    return sdist


def target_from_environ() -> Target:
    """The target that the Nix half named, read from the environment.

    Every planner of this example reads it the same way, because `default.nix`
    gives every planner the same variable. That variable is one JSON document,
    and not one variable for each field, because the marker environment is
    eleven fields and a planner must pass all of them.
    """
    document = json.loads(env("DDRN_TARGET"))
    markers: dict[str, str] = document["markers"]

    missing = PEP508_KEYS - set(markers)
    if missing:
        raise RuntimeError(
            f"DDRN_TARGET gives no value for {sorted(missing)}. `packaging` would then read "
            "the interpreter that runs this planner, and a planner plans for another machine."
        )

    python_version = document["pythonVersion"]
    platform = document["platform"]
    return Target(
        tags=tuple(target_tags(python_version, platform)),
        # `extra` is not a field of the environment. It is the name that a
        # marker of an optional dependency compares against, and an empty
        # value makes each such marker false, which is what an edge that the
        # lock file already resolved needs.
        markers={**markers, "extra": ""},
        python_version=python_version,
        platform=platform,
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
