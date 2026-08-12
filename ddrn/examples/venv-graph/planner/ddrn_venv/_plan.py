"""The plan document, and how a planner submits one.

**A plan document is the interface between the two halves of this example.**
A planner decides what the graph is and writes the document. `graph.nix` says
how a node of each kind is built, and it reads no lock file. So a reader of a
different lock format is a new producer of this document, and `graph.nix` does
not change when one arrives.

The document names one node as its `root`. The outer planner roots its
document at the virtual environment. The planner of a source distribution
roots its document at the build of that distribution.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nanopynix_bindings import expr as nix_expr, store as nix_store
from packaging.utils import canonicalize_name

# **`nanopynix.libstore` and not `nanopynix`.** A planner opens a store,
# initialises Nix and submits one output, and it needs no RPC engine. That
# module imports `nanopynix_bindings` and nothing else of nanopynix, so it
# costs 53 ms against 848 ms for the package. `ddrn/PACKAGING-FACTS.md`
# measures what a planner spends its run on.
from nanopynix.libstore import init_libstore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._resolve import Artifact


def nix_string(text: str) -> str:
    """`text` as a Nix string literal.

    A Nix string takes the escapes of C for the backslash and the quote, and
    it also takes `${` as the start of an interpolation. The JSON below holds
    store paths and file names, so `${` cannot appear, but an escape that
    depends on the input is not an escape.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${")
    return f'"{escaped}"'


@dataclass(frozen=True, slots=True)
class LocalNode:
    """What a node that builds one tree of the user needs, whichever form.

    **The three forms differ in one hook, and in nothing else.** `build_editable`,
    `build_sdist` and `build_wheel` all run in the same directory, with the same
    backend, on the same path. So the three methods below take this value, and
    only `editable_node` takes anything more.

    `subdirectory` names the project inside `source`. `uv.lock` writes it for a
    git source and for a URL source that hold the project below their root.
    """

    name: str
    source: str
    backend: str
    backend_path: list[str]
    subdirectory: str = ""

    def entry(self, kind: str, suffix: str) -> dict[str, Any]:
        """The node, under the name that `kind` gives it.

        **The name carries a suffix, because the project may also name the
        environment.** `uv init --package` makes a project that is both a member
        of its own environment and the name of it, and two nodes may not share a
        name.
        """
        return {
            "name": f"{self.name}-{suffix}",
            "kind": kind,
            "platform": False,
            "source": self.source,
            "backend": self.backend,
            "backendPath": self.backend_path,
            "subdirectory": self.subdirectory,
        }


def build_tools_from(inputs: dict[str, Any]) -> dict[str, list[str]]:
    """The store paths that each package needs to build, by PEP 503 name.

    `default.nix` takes the list at its `buildTools` argument, and it writes
    the store path of each entry. **The name is canonicalised here, and not in
    `graph.nix`.** A distribution name has more than one spelling, and
    `packaging` holds the rule that makes two spellings one name.
    """
    given: dict[str, list[str]] = inputs.get("buildTools", {})
    return {canonicalize_name(name): list(roots) for name, roots in given.items()}


@dataclass
class Plan:
    """The graph that a planner decided, in the shape `graph.nix` takes."""

    # A subscripted alias is callable and gives an empty container, and it also
    # tells the type checker what that container holds. A bare `list` here
    # leaves the element type unknown.
    nodes: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    members: list[str] = field(default_factory=list[str])
    installed: list[str] = field(default_factory=list[str])
    #: The node of each member, by PEP 503 name. `application_node` reads
    #: this to find the one member that the caller asked for.
    by_package: dict[str, str] = field(default_factory=dict[str, str])
    #: The name of the node that every install step reads.
    installer: str = ""
    #: The store paths that each package needs to build, by PEP 503 name.
    #: `build_tools_from` reads the value out of the inputs of the planner.
    build_tools: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    _seen: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])

    def tools_for(self, package: str) -> dict[str, Any]:
        """The `buildTools` key of the node that builds `package`, or nothing.

        **A package with no entry gets no key at all.** A node is the name of a
        store path, so an empty key would change the output path of every node
        of every graph that this repository has already built.
        """
        roots = self.build_tools.get(canonicalize_name(package))
        return {"buildTools": roots} if roots else {}

    def add(self, entry: dict[str, Any]) -> str:
        """Add one node, and return its name.

        **A repeated node adds nothing, and a repeated *name* is an error.**
        The same wheel answers more than one requirement, and the second call
        must reuse the first node rather than make a second one. Two different
        nodes under one name are a different thing: `graph.nix` keys a node by
        its name, so the second would replace the first and the graph would
        build something that no planner asked for.
        """
        name: str = entry["name"]
        existing = self._seen.get(name)
        if existing is not None:
            if existing != entry:
                raise ValueError(
                    f"two different nodes are both called '{name}': a node of kind "
                    f"'{existing['kind']}' and a node of kind '{entry['kind']}'. "
                    "A node name is the name of a store path, so it has to be unique."
                )
            return name
        self._seen[name] = entry
        self.nodes.append(entry)
        return name

    def artifact_node(
        self,
        artifact: Artifact,
        kind: str,
        *,
        backend: str = "",
        backend_path: list[str] | None = None,
    ) -> str:
        """Add the node that builds one artefact of the lock file.

        `kind` is what the node *does*, and it is not always the kind of the
        artefact. The installer is a wheel, and its node unpacks it rather
        than installs it, because nothing can install a wheel before that node
        exists.

        **That bootstrap node carries a name of its own.** The same installer
        is a member of the environment when a group or an extra asks for it,
        and the node that installs it is a different node with a different
        output. One name for both makes `add` refuse the graph, so the
        bootstrap takes the `-bootstrap` suffix.

        A node of kind `sdist-build` runs one PEP 517 hook, so it takes the
        backend and the path that the backend lives on. Both belong in this
        call, and not in an assignment to the node afterwards: `add` returns
        the existing name when a node repeats, and a later assignment would
        then reach the wrong node.
        """
        entry: dict[str, Any] = {
            "name": f"{artifact.node_name}-bootstrap" if kind == "unpack" else artifact.node_name,
            "kind": kind,
            "artifact": artifact.as_plan(),
            # `graph.nix` gives a node the loader and the libraries of this
            # system only when the tag of the wheel says that the node needs
            # them. The tag is what `packaging` already parsed.
            "platform": artifact.is_platform_wheel,
        }
        if kind == "sdist-build":
            if not backend:
                raise ValueError(f"{artifact.filename}: a node of kind '{kind}' needs a backend")
            entry["backend"] = backend
            entry["backendPath"] = backend_path or []
            # **Only a node that runs a build gets a tool.** A wheel is
            # already built, so the node that installs one needs no compiler
            # and takes no dependency on one.
            entry |= self.tools_for(artifact.package)
        return self.add(entry)

    def editable_node(self, node: LocalNode, *, root: str) -> str:
        """Add the node that builds one local project as an editable.

        `root` is what the PEP 660 redirect names, and it is the one thing that
        the other two forms do not have. Both of those hold the code, so
        nothing redirects to a tree of the user.
        """
        return self.add(node.entry("editable", "editable") | {"root": root} | self.tools_for(node.name))

    def local_wheel_node(self, node: LocalNode) -> str:
        """Add the node that builds a wheel out of a tree, and installs it.

        **This is what `pip install .` does, and what a git source needs.** A
        git checkout is a directory, so no archive stands between the tree and
        the wheel: one PEP 517 `build_wheel` call answers it. A `directory`
        source of `uv.lock` takes the same route, because `uv` installs one
        that way and not as an editable.
        """
        return self.add(node.entry("local-wheel", "from-tree") | self.tools_for(node.name))

    def local_sdist_node(self, node: LocalNode) -> str:
        """Add the node that builds the source distribution of a local project.

        **This node is what `uv build` writes.** It runs the PEP 517
        `build_sdist` hook over the tree of the project, and its output is the
        archive that an index would serve. It installs nothing;
        `local_build_node` reads the archive and builds the wheel.
        """
        return self.add(node.entry("local-sdist", "sdist") | self.tools_for(node.name))

    def local_build_node(self, *, name: str, archive: str, backend: str, backend_path: list[str]) -> str:
        """Add the node that builds a wheel out of a local source distribution.

        **The kind is the same one that an artefact of the index takes.** A
        distribution that a node of this graph built and a distribution that
        the index served are the same thing to the build that reads it, so the
        same node kind and the same script serve both. `archive` names the node
        that holds the archive, and an artefact of the index carries a fetch
        instead.
        """
        return self.add(
            {
                "name": f"{name}-from-sdist",
                "kind": "sdist-build",
                "platform": False,
                "from": archive,
                "backend": backend,
                "backendPath": backend_path,
            }
            | self.tools_for(name)
        )

    def nixpkgs_node(self, *, package: str, version: str, drv: str, out: str) -> str:
        """Add the node that takes one package from nixpkgs, already built.

        **This node builds nothing.** `default.nix` instantiated the package
        outside the sandbox and handed in the `.drv` and the output path with
        the dependency between them discarded, which is the tier 4 mechanism
        that `ddrn/PACKAGING-FACTS.md` describes. `graph.nix` puts the
        dependency back with `builtins.appendContext`, and the outer store then
        **substitutes** the output from a cache rather than building it.

        The name carries a suffix, because the package may also be the name of
        the environment, and two nodes may not share a name.
        """
        return self.add(
            {
                "name": f"{package}-{version}-nixpkgs",
                "kind": "nixpkgs",
                # The store path is whatever nixpkgs built, and this graph does
                # not patch it. nixpkgs already linked it against this system.
                "platform": False,
                "artifact": {"package": package, "version": version, "drv": drv, "out": out},
            }
        )

    def venv_node(self, name: str) -> str:
        """Add the node that composes the members into an environment."""
        return self.add(
            {
                "name": name,
                "kind": "venv",
                "platform": False,
                "members": list(self.members),
                "installed": list(self.installed),
            }
        )

    def application_node(self, name: str, *, venv: str, bases: Sequence[str]) -> str:
        """Add the node that links the programs of one member out of `venv`.

        `bases` names the node that installed each member, and those nodes are
        the template of the shape. `scripts/make-application.py` gives the two
        rules that decide which paths of a template reach the output.
        """
        return self.add(
            {
                "name": name,
                "kind": "application",
                "platform": False,
                "venv": venv,
                "bases": list(bases),
            }
        )

    def root(self, name: str, *, application: str | Sequence[str] | None = None) -> str:
        """The node that the planner submits, which is one of two shapes.

        **An environment holds every program of every member, and an
        application holds the programs of the members that the caller names.**
        `uv.nix` gives the second shape through its `application` argument, and
        `scripts/make-application.py` gives the whole reason to want it: a
        virtual environment carries `bin/python`, so a NixOS configuration that
        installs one shadows the interpreter of the system.

        `application` takes one name or a list of them. **A list is one output
        over one environment**, and not one output for each name: a project
        that ships three programs ships them together, and three separate
        applications would each hold a link into the same environment.

        The environment is a node either way. The application links out of it,
        so both nodes are in the graph and only the root differs. The
        environment then takes the `-env` suffix, because the application is
        what the caller builds and it carries the name of the project.
        """
        if application is None:
            return self.venv_node(name)

        wanted = [application] if isinstance(application, str) else list(application)
        if not wanted:
            raise ValueError(
                "application is an empty list. Name at least one member, or pass null for the environment itself."
            )

        bases = [self.by_package.get(canonicalize_name(package)) for package in wanted]
        missing = [package for package, base in zip(wanted, bases, strict=True) if base is None]
        if missing:
            held = ", ".join(sorted(self.by_package)) or "no member at all"
            names = ", ".join(f"'{package}'" for package in missing)
            raise ValueError(
                f"application names {names}, and the environment holds {held}. "
                "An application is one member of the environment, so the selection has to install it."
            )

        # `dict.fromkeys` keeps the order that the caller wrote and drops a
        # repeat. Two names of one member are one template.
        found = list(dict.fromkeys(base for base in bases if base is not None))
        return self.application_node(name, venv=self.venv_node(f"{name}-env"), bases=found)

    def member(self, name: str, label: str, *, package: str) -> None:
        """Mark a node as a member of the environment, and not only a build input.

        `label` is the line that the manifest of the environment carries.
        `package` is the distribution name, which `application_node` needs to
        find the node of one member again.
        """
        self.members.append(name)
        self.installed.append(label)
        self.by_package[canonicalize_name(package)] = name

    def document(self, *, root: str, system: str, python_version: str, inputs: dict[str, Any]) -> dict[str, Any]:
        """The whole document, ready for `graph.nix`."""
        return {
            "system": system,
            "pythonVersion": python_version,
            "tools": inputs["tools"],
            "scripts": inputs["scripts"],
            "nodes": self.nodes,
            "installer": self.installer,
            "root": root,
        }


def open_restricted_store() -> nix_store.Store:
    """Open the restricted daemon socket that `builder-rpc-v0` gives a builder.

    A planner runs only inside such a build, so a different `NIX_REMOTE` is an
    error and not a fallback.
    """
    remote = os.environ.get("NIX_REMOTE", "")
    if not remote.startswith("unix://"):
        raise RuntimeError(
            f"NIX_REMOTE is {remote!r}. This script runs only inside a "
            "builder-rpc-v0 build, which sets it to the restricted socket."
        )
    # A `builder-rpc-v0` build gets no `$out`. The output arrives through
    # `submit_output` instead, so there is no path to write to.
    if "out" in os.environ:
        raise RuntimeError(f"unexpected: out is set to {os.environ['out']!r}")  # noqa: SIM112 -- Nix names this variable in lower case

    init_libstore()
    nix_expr.init_libexpr()
    store = nix_store.open_store(remote)
    print(f"==> restricted store: {store.get_uri()}", file=sys.stderr)
    return store


def submit(store: nix_store.Store, document: dict[str, Any], graph_nix: str) -> None:
    """Evaluate `graph.nix` over `document`, and submit the root it wrote.

    **The evaluator reads a fixed file, and the plan is its argument.** The
    decisions of a planner are data, and `graph.nix` is the code that turns
    them into derivations. No planner writes a Nix expression by hand.
    """
    expression = f"import {graph_nix} (builtins.fromJSON {nix_string(json.dumps(document))})"

    state = nix_expr.EvalState(store)
    root = state.eval_string(expression)

    # `derived_path` gives the store path of the `.drv` that the evaluator
    # wrote. It is not an output path: this build cannot realise anything.
    root_drv = root.derived_path()
    print(f"==> the evaluator wrote {root_drv}", file=sys.stderr)

    store.submit_output(root_drv, "out")
    print("==> submitted it as output 'out'", file=sys.stderr)
