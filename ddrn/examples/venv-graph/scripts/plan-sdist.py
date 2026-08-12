"""Plan the build of one source distribution, from inside a build.

**This is the step that no evaluation can take.** A source distribution
declares its build system inside the archive, and the archive is a file that
some derivation has to download first. So the backend is not known when the
outer plan runs, and `uv.lock` and `poetry.lock` do not record it either.
`ddrn/PACKAGING-FACTS.md` gives the measurement behind that statement.

The outer planner therefore registers **this planner** as a node of its graph,
rather than a build. The archive is an input of this derivation, so Nix
downloads it before this script runs. This script reads `pyproject.toml` out of
the archive, resolves the backend against the same menu of artefacts that the
outer planner used, and submits a graph that builds the distribution.

`ddrn/examples/submitted-graph` submits a graph from a build.
`tests/functional/dyn-drv/eval-submit.sh` of the patched Nix runs three such
levels. This script is the second level of that chain, and the graph that it
submits may hold a third: a backend that is itself a source distribution
becomes another node of kind `sdist`, which is another planner.
"""

from __future__ import annotations

import json
import sys

from ddrn_venv import (
    Artifact,
    Lock,
    LockError,
    Plan,
    build_requirements,
    build_system,
    build_tools_from,
    choose,
    env,
    open_restricted_store,
    read_pyproject,
    submit,
    target_from_environ,
)
from packaging.requirements import Requirement


def main() -> int:
    store = open_restricted_store()

    inputs = json.loads(env("DDRN_INPUTS"))
    artifacts = [Artifact.from_json(row) for row in inputs["artifacts"]]
    target = target_from_environ()

    # The archive itself. It is an input of this derivation, so it is present,
    # and the node that builds it needs no fetch.
    archive = env("DDRN_SDIST")
    # **The row of this archive travels apart from the menu, and that is a
    # measurement and not a taste.** Every planner of one level reads the same
    # menu, so `default.nix` serialises that menu one time for the level. A row
    # inside the menu made each document different, and Nix then wrote a
    # quarter of a megabyte of JSON for each of the planners.
    subject: dict[str, str] = json.loads(env("DDRN_SDIST_ROW"))
    # **The project is not always at the top of the archive.** A URL source of
    # `uv.lock` names a subdirectory, and the archive of `pypa/hatch` holds more
    # than one project. The declaration to read, and the directory that the
    # backend runs in, are both below that subdirectory.
    subdirectory = subject.get("subdirectory", "")
    sdist = Artifact(
        package=subject["package"],
        version=subject["version"],
        filename=subject["filename"],
        kind="sdist",
        path=archive,
        subdirectory=subdirectory,
    )

    backend, requirements = build_system(read_pyproject(archive, subdirectory))
    print(f"plan-sdist: {sdist.filename} builds through {backend}", file=sys.stderr)

    # **A backend needs its own dependencies, and `requires` names only the top
    # of that closure.** `flit_core` needs nothing, and `hatchling` needs four
    # packages to import at all. A `uv.lock` holds the edges of every package
    # that it lists, so it expands the list when there is one.
    if (spec := inputs.get("uv")) is not None:
        try:
            requirements = build_requirements(Lock.read(spec["lock"]), target.markers, requirements)
        except LockError as error:
            raise RuntimeError(f"{sdist.filename}: {error}") from error

    # **The tools travel with the inputs, so a nested planner has them too.**
    # A source distribution that holds C code needs a compiler wherever its
    # build node is, and this planner is what makes that node.
    plan = Plan(build_tools=build_tools_from(inputs))
    no_binary = frozenset(inputs.get("noBinary", []))

    # The installer, on the same terms as in the outer graph. The node is the
    # same wheel unpacked by the same script, so its content is the same and
    # content addressing gives it the same output path in both graphs.
    bootstrap = choose(artifacts, Requirement(inputs["installer"]), target)
    if bootstrap is None:
        raise RuntimeError(f"the installer '{inputs['installer']}' is not in the menu")
    plan.installer = plan.artifact_node(bootstrap, "unpack")

    backend_path: list[str] = []
    for requirement in requirements:
        dependency = choose(artifacts, requirement, target, no_binary)
        if dependency is None:
            raise RuntimeError(f"{sdist.filename}: no artefact of the menu satisfies '{requirement}'")
        # **A backend that is itself a source distribution needs no special
        # case.** Its node is of kind `sdist`, and `graph.nix` turns that into
        # another planner, which is one more level of the same chain.
        backend_path.append(plan.artifact_node(dependency, dependency.kind))
        print(f"plan-sdist:   backend {dependency.filename} for {requirement}", file=sys.stderr)

    root = plan.artifact_node(sdist, "sdist-build", backend=backend, backend_path=backend_path)

    print(f"==> {len(plan.nodes)} nodes for {sdist.filename}", file=sys.stderr)

    submit(
        store,
        plan.document(
            root=root,
            system=env("DDRN_SYSTEM"),
            python_version=target.python_version,
            inputs=inputs,
        ),
        env("DDRN_GRAPH_NIX"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
