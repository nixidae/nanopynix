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

import nanopynix

if TYPE_CHECKING:
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


@dataclass
class Plan:
    """The graph that a planner decided, in the shape `graph.nix` takes."""

    # A subscripted alias is callable and gives an empty container, and it also
    # tells the type checker what that container holds. A bare `list` here
    # leaves the element type unknown.
    nodes: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    members: list[str] = field(default_factory=list[str])
    installed: list[str] = field(default_factory=list[str])
    #: The name of the node that every install step reads.
    installer: str = ""
    _seen: set[str] = field(default_factory=set[str])

    def add(self, entry: dict[str, Any]) -> str:
        """Add one node, and return its name. A repeated name adds no node."""
        name: str = entry["name"]
        if name in self._seen:
            return name
        self._seen.add(name)
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

        A node of kind `sdist-build` runs one PEP 517 hook, so it takes the
        backend and the path that the backend lives on. Both belong in this
        call, and not in an assignment to the node afterwards: `add` returns
        the existing name when a node repeats, and a later assignment would
        then reach the wrong node.
        """
        entry: dict[str, Any] = {
            "name": artifact.node_name,
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
        return self.add(entry)

    def editable_node(self, *, name: str, source: str, root: str, backend: str, backend_path: list[str]) -> str:
        """Add the node that builds one local project as an editable."""
        return self.add(
            {
                "name": name,
                "kind": "editable",
                "platform": False,
                "source": source,
                "root": root,
                "backend": backend,
                "backendPath": backend_path,
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

    def member(self, name: str, label: str) -> None:
        """Mark a node as a member of the environment, and not only a build input.

        `label` is the line that the manifest of the environment carries.
        """
        self.members.append(name)
        self.installed.append(label)

    def document(self, *, root: str, system: str, python_version: str, menu: dict[str, Any]) -> dict[str, Any]:
        """The whole document, ready for `graph.nix`."""
        return {
            "system": system,
            "pythonVersion": python_version,
            "tools": menu["tools"],
            "scripts": menu["scripts"],
            "nodes": self.nodes,
            "installer": self.installer,
            "root": root,
        }


def open_restricted_store() -> Any:
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

    nanopynix.init_libstore()
    nix_expr.init_libexpr()
    store = nix_store.open_store(remote)
    print(f"==> restricted store: {store.get_uri()}", file=sys.stderr)
    return store


def submit(store: Any, document: dict[str, Any], graph_nix: str) -> None:
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
