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
- one node for each local project, in the form that the caller asked for. A
  PEP 660 editable runs the backend of the project and then redirects the
  install at a tree outside the store. A source distribution runs `build_sdist`
  over that same tree, and a second node builds a wheel out of the archive;
- one node for the environment, which composes the members.

**The source distribution is the case that motivates the whole lab.** `idna`
is in the lock file as an sdist only. Its `pyproject.toml` asks for
`flit_core`, the planner resolves that name against the same lock file, and
the wheel of `flit_core` becomes another node of the same graph. The build of
`idna` then reads the output of that node. No node of that pair can exist
without the other, and neither can exist before the plan runs.

**The decisions live in `ddrn_venv`, and this file reads one document.**
`ddrn_venv` holds what every planner of this example shares. What remains here
is reading `DDRN_INPUTS`, which the Nix half builds from a lock file, and
turning it into the plan document.

**Two lock formats reach this planner, and `DDRN_INPUTS` is the same for
both.**
`lock.json` is the hand-written one of this example, and its `roots` name what
to install. `uv.lock` is the one that `uv` writes, and it names no root: this
planner resolves it here, with `ddrn_venv._uvlock`, and the members of that
resolution become roots. `./uv.nix` is the Nix half of the second one.
"""

from __future__ import annotations

import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ddrn_venv import (
    Artifact,
    LocalNode,
    Lock,
    LockError,
    Plan,
    Target,
    build_requirements,
    build_system,
    build_tools_from,
    check_freshness,
    choose,
    env,
    open_restricted_store,
    resolve_lock,
    submit,
    target_from_environ,
)
from ddrn_venv._workspace import members as members_of, selection
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ddrn_venv._workspace import Choice


@dataclass(frozen=True, slots=True)
class Request:
    """What to install, and the artefacts to install it from.

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
    #: Each package to build from its source distribution even when a wheel
    #: fits. This is `tool.uv.no-binary-package`, which uv keeps in
    #: `pyproject.toml` and writes into no lock file.
    no_binary: frozenset[str] = frozenset()
    #: Each package that nixpkgs supplies instead of the index, by PEP 503
    #: name. The value holds the `version`, the `drv` and the `out` of a
    #: package that Nix instantiated outside the sandbox and did not build.
    #:
    #: **The name of this field is not `overrides`, and that is deliberate.**
    #: uv gives `override-dependencies` to a table that forces a version, and
    #: uv2nix gives `overrides` to a build fixup. Neither is this. One word
    #: carries one meaning in this repository.
    from_nixpkgs: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    #: The store paths that each package needs to build, by PEP 503 name.
    #:
    #: **This is what one build gets, and not what every build gets.** A source
    #: distribution that holds C code needs a compiler, and `graph.nix` gives a
    #: node the tools of its own package alone. `default.nix` gives the reason
    #: at its `buildTools` argument.
    build_tools: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    @classmethod
    def from_json(cls, inputs: dict[str, Any], roots: list[str]) -> Request:
        rows: list[dict[str, Any]] = inputs["artifacts"]
        supplied: dict[str, dict[str, str]] = inputs.get("fromNixpkgs", {})
        return cls(
            artifacts=[Artifact.from_json(row) for row in rows],
            roots=roots,
            installer=inputs["installer"],
            no_binary=frozenset(inputs.get("noBinary", [])),
            from_nixpkgs={canonicalize_name(name): entry for name, entry in supplied.items()},
            build_tools=build_tools_from(inputs),
        )


@dataclass(frozen=True, slots=True)
class UvProject:
    """A `uv.lock`, and the two decisions that the lock file does not record.

    **uv already chose every version, and this reader chooses none.** A lock
    file is a union over every environment that the project supports, so it
    says which versions are possible and not which packages this target
    installs. Three things stay open:

    1. Which packages apply. An edge carries a PEP 508 marker, a package of a
       forked resolution carries `resolution-markers`, and `requires-python`
       gates the whole file. `resolve_lock` answers this.
    2. Which extras and which dependency groups the caller wants. `uv sync`
       takes that decision on its command line, and `select` is that decision.
    3. Which artefact of a package to install. `choose` answers that, with the
       PEP 425 tags of the target.

    So every member comes back as an exact pin, and `choose` never sees the
    range that the project wrote.
    """

    lock: Lock
    #: Each root of the resolution, with the extras and the groups to enable.
    #: `None` asks this class to build it, from `extras`, from `groups` and
    #: from the `pyproject.toml` of each member.
    select: dict[str, list[str]] | None
    #: The tree of each local package, keyed by its normalised name. `uv.nix`
    #: copies each one into the store, and the backend of the project reads it.
    editables: dict[str, dict[str, str]]
    #: The `pyproject.toml` of each member that lives in the workspace, keyed by
    #: its normalised name, as a store path. `check_freshness` compares each one
    #: with the `[package.metadata]` that the lock file recorded. An empty
    #: mapping turns that comparison off.
    declarations: dict[str, str] = field(default_factory=dict[str, str])
    #: The `pyproject.toml` of every directory under the workspace, keyed by
    #: the path of that directory. `uv.nix` walks the tree to build it, because
    #: a member that the lock file never saw reaches no comparison of
    #: `declarations`. `ddrn_venv._workspace` reads the globs out of it. An
    #: empty mapping turns that comparison off.
    workspace: dict[str, str] = field(default_factory=dict[str, str])
    #: Which extras of each member to enable, when `select` says nothing.
    #: `None` is `uv sync` with no flag, `"all"` is `--all-extras`, and a list
    #: names them. `ddrn_venv._workspace.extras_of` gives the rule of uv.
    extras: Choice = None
    #: Which dependency groups, when `select` says nothing. `None` is
    #: `tool.uv.default-groups` of each member, which is `["dev"]` when that
    #: table says nothing, and `"all"` is `--all-groups`.
    groups: Choice = None

    @classmethod
    def from_json(cls, spec: dict[str, Any]) -> UvProject:
        return cls(
            lock=Lock.read(spec["lock"]),
            select=spec.get("select"),
            editables=spec.get("editables", {}),
            declarations=spec.get("declarations", {}),
            workspace=spec.get("workspace", {}),
            extras=spec.get("extras"),
            groups=spec.get("groups"),
        )

    def roots(self) -> dict[str, list[str]]:
        """Which members to install, and what to enable in each one.

        **`uv.lock` records none of this**, so `select` is the decision when a
        caller states it. A caller that states nothing gets what `uv sync`
        gives: every member of the workspace, no extra, and the groups that
        `tool.uv.default-groups` names.
        """
        if self.select is not None:
            return self.select
        found = members_of(self.workspace)
        if found is None:
            raise LockError(
                "no selection was given, and the workspace could not be read to build one. Pass `select` to `uv.nix`."
            )
        return selection(found, self.extras, self.groups)

    def selection(self, target: Target) -> tuple[list[str], list[LocalProject]]:
        """What this target installs: the pinned artefacts, and the local trees.

        **A local package of the lock file installs from its own tree, and uv
        says so.** `uv init --package` writes `source = { editable = "." }` for
        the project, and `uv init --no-package` writes
        `source = { virtual = "." }` for one that ships nothing.
        `Resolution.members` drops the virtual kind, and every other local kind
        installs from its own tree.

        **The lock file does not say which *form* to install.** `uv sync`
        installs a member as a PEP 660 editable, and `uv build` writes the
        source distribution that an index serves. Both are the same project,
        and `mode` is that decision. `uv.nix` takes it as an argument.

        **The freshness check runs first, and it runs before the resolution.**
        A lock file that the project has grown past resolves the old
        declaration, and every answer after that point is an answer about the
        wrong project.
        """
        check_freshness(self.lock, self.declarations, self.workspace)

        resolution = resolve_lock(self.lock, target.markers, self.roots())

        roots: list[str] = []
        projects: list[LocalProject] = []
        for package in sorted(resolution.members, key=lambda member: member.name):
            # **A git checkout is a tree, and so it takes the tree route.** It
            # carries no `wheels` and no `sdist`, so a root of the form
            # `name==version` would find no artefact and this planner would
            # report that the target fits nothing. A URL source is the other
            # way round: `uv.nix` joins its location to its hash, and what
            # arrives here is an ordinary source distribution.
            if not (package.is_local or package.is_git):
                roots.append(f"{package.raw_name}=={package.version}")
                continue
            spec = self.editables.get(package.name)
            if spec is None:
                where = "a git checkout" if package.is_git else "a local directory"
                raise RuntimeError(
                    f"uv.lock resolves '{package.label}' from {where}, and the inputs "
                    f"carry no tree for it. uv.nix names one for each such package of the lock."
                )
            projects.append(
                LocalProject(
                    name=package.raw_name,
                    source=spec["source"],
                    root=spec["root"],
                    mode=spec.get("mode", "editable"),
                    subdirectory=spec.get("subdirectory", ""),
                )
            )

        print(
            f"plan: uv.lock resolves {len(roots)} packages and {len(projects)} local projects",
            file=sys.stderr,
        )
        return roots, projects

    def backend(self, requirements: Iterable[Requirement], target: Target, subject: str) -> list[Requirement]:
        """The closure of a `build-system.requires`, as one pin for each name.

        `ddrn_venv.build_requirements` says why the closure and not the list,
        and what to do when the lock file does not hold the backend.
        """
        try:
            return build_requirements(self.lock, target.markers, requirements)
        except LockError as error:
            raise RuntimeError(f"{subject}: {error}") from error


@dataclass(frozen=True, slots=True)
class LocalProject:
    """One local project, and the form to install it in.

    `source` is a copy of the project inside the store, which the backend
    reads. `root` is what an editable install redirects to, and it is either a
    literal path or a reference to a variable that the environment reads at run
    time. A source distribution install ignores `root`, because the code is in
    the archive and nothing outside the store answers for it.

    `mode` is one of:

        editable  PEP 660 `build_editable`, which is what `uv sync` performs.
        sdist     PEP 517 `build_sdist`, and then `build_wheel` over the
                  archive that comes out. This is what `uv build` writes and
                  what an index serves, so it is the form that reaches a user
                  who does not have the tree.
        wheel     PEP 517 `build_wheel` over the tree itself, which is what
                  `pip install .` does. A git checkout takes this route, and
                  so does a `directory` source of `uv.lock`.

    `subdirectory` names the project inside `source`, and it is empty for a
    tree whose project is at the top. A git source of `uv.lock` carries one
    when the repository holds the project below its root.
    """

    name: str
    source: str
    root: str
    mode: str = "editable"
    subdirectory: str = ""

    @property
    def tree(self) -> Path:
        """The directory that holds `pyproject.toml`, which the backend reads."""
        return Path(self.source, self.subdirectory) if self.subdirectory else Path(self.source)

    @property
    def pyproject(self) -> dict[str, Any]:
        """The `pyproject.toml` of the project, read from the store copy."""
        with (self.tree / "pyproject.toml").open("rb") as handle:
            return tomllib.load(handle)


def resolve_backend(
    plan: Plan,
    request: Request,
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
        dependency = choose(request.artifacts, requirement, target, request.no_binary)
        if dependency is None:
            raise RuntimeError(f"{subject}: no artefact of the lock file satisfies '{requirement}'")
        names.append(plan.artifact_node(dependency, dependency.kind))
        print(f"plan:   backend {dependency.filename} for {requirement}", file=sys.stderr)
    return names


def resolve(
    request: Request,
    projects: list[LocalProject],
    target: Target,
    uv: UvProject | None = None,
) -> Plan:
    """Turn the lock file into the graph that builds the environment."""
    plan = Plan(build_tools=request.build_tools)
    artifacts = request.artifacts

    # **The installer bootstraps, and it does so by unpacking.** Every other
    # node installs its wheel with `pypa/installer`, which writes `RECORD`, the
    # `.dist-info` and each console script. Nothing can install the installer
    # itself, so its node unzips the wheel and puts it on a path. This is the
    # same bootstrap that `pip` performs, and it is one node of the graph.
    bootstrap = choose(artifacts, Requirement(request.installer), target)
    if bootstrap is None:
        raise RuntimeError(f"the installer '{request.installer}' is not in the lock file")
    plan.installer = plan.artifact_node(bootstrap, "unpack")
    print(f"plan:   installer {bootstrap.filename} (unpacked to bootstrap)", file=sys.stderr)

    for package in request.roots:
        requirement = Requirement(package)

        # **nixpkgs answers first, and the index is not consulted at all.** The
        # entry names a package that Nix already built for this system, so
        # no wheel is chosen, no artefact is fetched and `auto-patchelf` never
        # runs. `ddrn/PACKAGING-FACTS.md` gives the reason to want this: a
        # manylinux wheel carries its own copy of every library it links
        # against, and nixpkgs links against the store.
        supplied = request.from_nixpkgs.get(canonicalize_name(requirement.name))
        if supplied is not None:
            name = plan.nixpkgs_node(
                package=requirement.name,
                version=supplied["version"],
                drv=supplied["drv"],
                out=supplied["out"],
            )
            plan.member(name, f"{requirement.name}=={supplied['version']} (nixpkgs)", package=requirement.name)
            # **The version of nixpkgs is not the version of the lock file**,
            # and this line is the only place that says so. A reader who wants
            # the pinned version has to drop the override.
            print(
                f"plan: {package}: nixpkgs supplies {requirement.name}=={supplied['version']}, "
                f"and the lock file asked for {requirement.specifier or 'any version'}",
                file=sys.stderr,
            )
            continue

        chosen = choose(artifacts, requirement, target, request.no_binary)
        if chosen is None:
            print(f"plan: {package}: no artefact fits this target", file=sys.stderr)
            continue

        # **A source distribution becomes a planner, and not a build.** The
        # backend it needs is declared inside the archive, which no evaluation
        # has downloaded. `graph.nix` turns a node of kind `sdist` into a
        # derivation that runs `scripts/plan-sdist.py`, and that planner
        # submits the graph which builds this distribution.
        name = plan.artifact_node(chosen, chosen.kind)
        plan.member(name, f"{chosen.package}=={chosen.version}", package=chosen.package)
        binary = " (binary)" if chosen.is_platform_wheel else ""
        print(f"plan: {package}: {chosen.filename} ({chosen.kind}){binary}", file=sys.stderr)

    for project in projects:
        # **A local project declares a build backend the same way an sdist
        # does.** The planner reads that declaration from the project itself,
        # and resolves it against the same lock file.
        subject = f"{project.name} ({project.mode})"
        backend, requirements = build_system(project.pyproject)
        # **A backend needs its own dependencies, and `requires` names only the
        # top of that closure.** `uv.lock` holds the edges of every package it
        # lists, so the lock file expands the list when there is one.
        if uv is not None:
            requirements = uv.backend(requirements, target, subject)
        # **One path serves both hooks, and PEP 517 says that it may.** The
        # `requires` of the project is the environment that a backend builds
        # in, whichever hook the frontend calls. What this lab does not do is
        # call `get_requires_for_build_sdist` afterwards, and
        # `scripts/build-local.py` gives the reason.
        backend_path = resolve_backend(plan, request, requirements, target, subject)

        # **The three forms differ in one PEP 517 hook, and in nothing else.**
        # The same tree, the same backend and the same path serve all three, so
        # one value carries them and `mode` picks the node.
        node = LocalNode(
            name=project.name,
            source=project.source,
            backend=backend,
            backend_path=backend_path,
            subdirectory=project.subdirectory,
        )
        where = f", under {project.subdirectory}" if project.subdirectory else ""

        if project.mode == "wheel":
            # **One hook, and no archive between the tree and the wheel.** A
            # git checkout is a directory, so this is the whole build.
            name = plan.local_wheel_node(node)
            plan.member(name, f"{project.name} (built from a tree)", package=project.name)
            print(f"plan: {project.name}: build_wheel through {backend}{where}", file=sys.stderr)
            continue

        if project.mode == "sdist":
            # **Two nodes, because a release is two steps.** `uv build` writes
            # the archive, an index serves it, and the consumer builds a wheel
            # out of it. The graph performs both, and the archive between them
            # is a store path that a reader can open.
            archive = plan.local_sdist_node(node)
            name = plan.local_build_node(
                name=project.name,
                archive=archive,
                backend=backend,
                backend_path=backend_path,
            )
            plan.member(name, f"{project.name} (from its own source distribution)", package=project.name)
            print(f"plan: {project.name}: build_sdist through {backend}{where}, then build_wheel", file=sys.stderr)
            continue

        name = plan.editable_node(node, root=project.root)
        plan.member(name, f"{project.name} (editable, from {project.root})", package=project.name)
        print(f"plan: {project.name}: editable through {backend}, reading {project.root}", file=sys.stderr)

    return plan


def main() -> int:
    store = open_restricted_store()

    inputs = json.loads(env("DDRN_INPUTS"))
    target = target_from_environ()

    projects = [
        LocalProject(
            name=name,
            source=spec["source"],
            root=spec["root"],
            mode=spec.get("mode", "editable"),
            subdirectory=spec.get("subdirectory", ""),
        )
        for name, spec in sorted(inputs.get("editables", {}).items())
    ]

    # `roots` of the inputs is the whole selection for `lock.json`. A `uv.lock`
    # names no root, so its resolution adds them here, and it also names the
    # local projects that install from their own tree.
    roots: list[str] = list(inputs["roots"])
    uv: UvProject | None = None
    if (spec := inputs.get("uv")) is not None:
        uv = UvProject.from_json(spec)
        resolved, uv_projects = uv.selection(target)
        roots += resolved
        projects += uv_projects

    request = Request.from_json(inputs, roots)

    plan = resolve(request, projects, target, uv)
    application: str | None = inputs.get("application")
    root = plan.root(env("DDRN_VENV_NAME"), application=application)
    if application is not None:
        print(f"plan: application {application}, over the environment of the same members", file=sys.stderr)

    print(
        f"==> {len(plan.nodes)} nodes, {len(plan.members)} of them in the "
        f"environment, from {len(request.artifacts)} artefacts",
        file=sys.stderr,
    )

    submit(
        store,
        plan.document(root=root, system=env("DDRN_SYSTEM"), python_version=target.python_version, inputs=inputs),
        env("DDRN_GRAPH_NIX"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
