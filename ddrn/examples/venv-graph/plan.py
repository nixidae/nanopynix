"""Resolve a Python environment, and register it as a graph of derivations.

This script is the builder of `ddrn/examples/venv-graph/default.nix`. It runs
inside the sandbox of a `builder-rpc-v0` build, and it needs the three changes
to Nix that `ddrn/UPSTREAM.md` gives.

**Compare it with `ddrn/examples/venv/plan.py`.** That planner makes the same
packaging decisions and then emits **one** derivation, because a planner that
writes to `$out` can emit exactly one. So it installs every wheel in a single
build step, and it cannot build a source distribution at all: the backend that
builds the source would have to exist before the plan runs.

This planner emits a graph:

- one node for each wheel, which installs it;
- one node for each source distribution, which runs its PEP 517 backend;
- one node for each local project, which runs the PEP 660 backend of that
  project and then redirects the install at a tree outside the store;
- one node for the environment, which composes the members.

**The source distribution is the case that motivates the whole lab.** `idna`
is in the lock file as an sdist only. Its `pyproject.toml` asks for
`flit_core`, the planner resolves that name against the same lock file, and
the wheel of `flit_core` becomes another node of the same graph. The build of
`idna` then reads the output of that node. No node of that pair can exist
without the other, and neither can exist before the plan runs.

**The decisions live in `ddrn_venv`, and this file is the reader of one menu.**
`ddrn_venv` holds what every planner of this example shares. What remains here
is reading `DDRN_MENU`, which the Nix half builds from `lock.json`, and turning
it into the plan document.
"""

from __future__ import annotations

import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ddrn_venv import (
    Artifact,
    Plan,
    Target,
    build_system,
    choose,
    env,
    open_restricted_store,
    submit,
    target_from_environ,
)
from packaging.requirements import Requirement

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True, slots=True)
class Lock:
    """What the Nix half read out of the lock file, as one value.

    This is the whole input of `resolve`, and it is what a reader of a
    different lock format has to produce.

    **It records no build system.** Neither `uv.lock` nor `poetry.lock` does,
    and `ddrn/PACKAGING-FACTS.md` gives the measurement. A source distribution
    therefore becomes a node of kind `sdist`, which is a planner and not a
    build, and that planner reads the declaration out of the archive.
    """

    artifacts: list[Artifact]
    roots: list[str]
    installer: str

    @classmethod
    def from_menu(cls, menu: dict[str, Any]) -> Lock:
        rows: list[dict[str, Any]] = menu["artifacts"]
        return cls(
            artifacts=[Artifact.from_json(row) for row in rows],
            roots=menu["roots"],
            installer=menu["installer"],
        )


@dataclass(frozen=True, slots=True)
class Editable:
    """One local project, and the tree that its install must read.

    `source` is a copy of the project inside the store, which the backend
    reads. `root` is what the install redirects to, and it is either a literal
    path or a reference to a variable that the environment reads at run time.
    """

    name: str
    source: str
    root: str

    @property
    def pyproject(self) -> dict[str, Any]:
        """The `pyproject.toml` of the project, read from the store copy."""
        with Path(self.source, "pyproject.toml").open("rb") as handle:
            return tomllib.load(handle)


def resolve_backend(
    plan: Plan,
    artifacts: list[Artifact],
    requirements: Iterable[Requirement],
    target: Target,
    subject: str,
) -> list[str]:
    """Give each backend of `requirements` a node, and name those nodes.

    **This is the step that one derivation cannot express.** The backend comes
    from the same lock file as the thing it builds, so neither the backend node
    nor the node that reads it can exist before the plan runs.
    """
    names: list[str] = []
    for requirement in requirements:
        dependency = choose(artifacts, requirement, target)
        if dependency is None:
            raise RuntimeError(f"{subject}: no artefact of the lock file satisfies '{requirement}'")
        names.append(plan.artifact_node(dependency, dependency.kind))
        print(f"plan:   backend {dependency.filename} for {requirement}", file=sys.stderr)
    return names


def resolve(lock: Lock, editables: list[Editable], target: Target) -> Plan:
    """Turn the lock file into the graph that builds the environment."""
    plan = Plan()
    artifacts = lock.artifacts

    # **The installer bootstraps, and it does so by unpacking.** Every other
    # node installs its wheel with `pypa/installer`, which writes `RECORD`, the
    # `.dist-info` and each console script. Nothing can install the installer
    # itself, so its node unzips the wheel and puts it on a path. This is the
    # same bootstrap that `pip` performs, and it is one node of the graph.
    bootstrap = choose(artifacts, Requirement(lock.installer), target)
    if bootstrap is None:
        raise RuntimeError(f"the installer '{lock.installer}' is not in the lock file")
    plan.installer = plan.artifact_node(bootstrap, "unpack")
    print(f"plan:   installer {bootstrap.filename} (unpacked to bootstrap)", file=sys.stderr)

    for package in lock.roots:
        chosen = choose(artifacts, Requirement(package), target)
        if chosen is None:
            print(f"plan: {package}: no artefact fits this target", file=sys.stderr)
            continue

        # **A source distribution becomes a planner, and not a build.** The
        # backend it needs is declared inside the archive, which no evaluation
        # has downloaded. `graph.nix` turns a node of kind `sdist` into a
        # derivation that runs `scripts/plan-sdist.py`, and that planner
        # submits the graph which builds this distribution.
        name = plan.artifact_node(chosen, chosen.kind)
        plan.member(name, f"{chosen.package}=={chosen.version}")
        binary = " (binary)" if chosen.is_platform_wheel else ""
        print(f"plan: {package}: {chosen.filename} ({chosen.kind}){binary}", file=sys.stderr)

    for editable in editables:
        # **A local project declares a build backend the same way an sdist
        # does.** The planner reads that declaration from the project itself,
        # and resolves it against the same lock file.
        backend, requirements = build_system(editable.pyproject)
        backend_path = resolve_backend(plan, artifacts, requirements, target, f"{editable.name} (editable)")
        name = plan.editable_node(
            name=editable.name,
            source=editable.source,
            root=editable.root,
            backend=backend,
            backend_path=backend_path,
        )
        plan.member(name, f"{editable.name} (editable, from {editable.root})")
        print(f"plan: {editable.name}: editable through {backend}, reading {editable.root}", file=sys.stderr)

    return plan


def main() -> int:
    store = open_restricted_store()

    menu = json.loads(env("DDRN_MENU"))
    lock = Lock.from_menu(menu)

    python_version = env("PYTHON_VERSION")
    target = target_from_environ()

    editables = [
        Editable(name=name, source=spec["source"], root=spec["root"])
        for name, spec in sorted(menu.get("editables", {}).items())
    ]

    plan = resolve(lock, editables, target)
    root = plan.venv_node(env("DDRN_VENV_NAME"))

    print(
        f"==> {len(plan.nodes)} nodes, {len(plan.members)} of them in the "
        f"environment, from {len(lock.artifacts)} artefacts",
        file=sys.stderr,
    )

    submit(
        store,
        plan.document(root=root, system=env("DDRN_SYSTEM"), python_version=python_version, menu=menu),
        env("DDRN_GRAPH_NIX"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
