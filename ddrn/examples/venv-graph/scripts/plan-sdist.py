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
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

from ddrn_venv import (
    Artifact,
    Plan,
    build_system,
    choose,
    env,
    open_restricted_store,
    submit,
    target_from_environ,
)
from packaging.requirements import Requirement

#: The name of the file that PEP 517 reads, inside the one top directory that
#: a source distribution must have.
PYPROJECT = "pyproject.toml"


def read_pyproject(archive: str) -> dict[str, Any]:
    """The `pyproject.toml` of a source distribution, read from the archive.

    A distribution that carries no such file is not an error. PEP 517 gives
    the fallback backend for exactly that case, and `build_system` applies it.
    """
    path = Path(archive)
    if path.name.endswith(".zip"):
        with zipfile.ZipFile(path) as archive_file:
            for name in archive_file.namelist():
                if name.count("/") == 1 and name.endswith(f"/{PYPROJECT}"):
                    return tomllib.loads(archive_file.read(name).decode())
        return {}

    with tarfile.open(path) as archive_file:
        for member in archive_file.getmembers():
            if member.name.count("/") == 1 and member.name.endswith(f"/{PYPROJECT}"):
                handle = archive_file.extractfile(member)
                if handle is None:
                    raise RuntimeError(f"{archive}: '{member.name}' is not a regular file")
                with handle:
                    return tomllib.loads(handle.read().decode())
    return {}


def main() -> int:
    store = open_restricted_store()

    menu = json.loads(env("DDRN_MENU"))
    artifacts = [Artifact.from_json(row) for row in menu["artifacts"]]
    target = target_from_environ()

    # The archive itself. It is an input of this derivation, so it is present,
    # and the node that builds it needs no fetch.
    archive = env("DDRN_SDIST")
    subject: dict[str, str] = menu["sdist"]
    sdist = Artifact(
        package=subject["package"],
        version=subject["version"],
        filename=subject["filename"],
        kind="sdist",
        path=archive,
    )

    backend, requirements = build_system(read_pyproject(archive))
    print(f"plan-sdist: {sdist.filename} builds through {backend}", file=sys.stderr)

    plan = Plan()

    # The installer, on the same terms as in the outer graph. The node is the
    # same wheel unpacked by the same script, so its content is the same and
    # content addressing gives it the same output path in both graphs.
    bootstrap = choose(artifacts, Requirement(menu["installer"]), target)
    if bootstrap is None:
        raise RuntimeError(f"the installer '{menu['installer']}' is not in the menu")
    plan.installer = plan.artifact_node(bootstrap, "unpack")

    backend_path: list[str] = []
    for requirement in requirements:
        dependency = choose(artifacts, requirement, target)
        if dependency is None:
            raise RuntimeError(f"{sdist.filename}: no artefact of the menu satisfies '{requirement}'")
        # **A backend that is itself a source distribution needs no special
        # case.** Its node is of kind `sdist`, and `graph.nix` turns that into
        # another planner, which is one more level of the same chain.
        backend_path.append(plan.artifact_node(dependency, dependency.kind))
        print(f"plan-sdist:   backend {dependency.filename} for {requirement}", file=sys.stderr)

    root = plan.artifact_node(sdist, "sdist-build", backend_path=backend_path)
    plan.nodes[-1]["backend"] = backend

    print(f"==> {len(plan.nodes)} nodes for {sdist.filename}", file=sys.stderr)

    submit(
        store,
        plan.document(
            root=root,
            system=env("DDRN_SYSTEM"),
            python_version=env("PYTHON_VERSION"),
            menu=menu,
        ),
        env("DDRN_GRAPH_NIX"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
