"""Build one local project, in the form that the plan asked for.

`graph.nix` runs this as the builder of a node of kind `editable`, of kind
`local-sdist` and of kind `local-wheel`. `mode` says which one, and the three
forms are the three ways that a project reaches a user:

    editable   PEP 660 `build_editable`, which `uv sync` performs. The wheel
               carries a redirect and no code.
    sdist      PEP 517 `build_sdist`, which `uv build` performs. The output is
               the archive that an index serves, and a second node builds a
               wheel out of it.
    wheel      PEP 517 `build_wheel` over the tree itself, which `pip install .`
               performs. A git source of `uv.lock` takes this route: a
               checkout is a directory, so no archive stands between the tree
               and the wheel.

**Neither form calls `get_requires_for_build_*`.** PEP 517 lets a backend name
more requirements at build time, through `get_requires_for_build_sdist`,
`get_requires_for_build_wheel` and `get_requires_for_build_editable`. A
frontend calls the hook, resolves what comes back, and adds it to the build
environment. This lab cannot: the node runs in a sandbox with no index and no
store, and the path of the backend is what the plan already decided. So the
`ddrn` dependency group of the project supplies the same packages instead, and
`projects/README.md` records the one case that this costs.

**An editable install is a redirection, and the redirection has to survive the
store.** A backend that follows PEP 660 writes a wheel that carries no code.
It carries a path instead, and that path is the directory the backend ran in.
Here that directory is a copy of the project inside the store, which is read
only and which the user does not edit. So this script rewrites the path that
the wheel recorded into the path that `graph.nix` was told to use.

`pyproject.nix` does the same in two steps, `build-editable` and
`patch-editable` (`build/hooks/editable_hook`), and it wraps the replacement in
`os.path.expandvars`. This script keeps that wrapper, because it is what makes
an environment variable a valid answer:

    editableRoot = "/home/me/Code/myapp"   ->  one fixed tree
    editableRoot = "$MYAPP_ROOT"           ->  the tree that the variable names

The second form matters for a store path. A literal root is part of what this
node builds, so a different root gives a different environment. A variable is
the same bytes for every tree, so **one environment reads whichever tree the
variable names, and nothing rebuilds.**

A source distribution takes no redirect at all. It holds the code, so the
install that comes out of it reads the store and not a tree of the user.

The environment gives:
    mode           `editable`, `sdist` or `wheel`
    source         the project, copied into the store
    subdirectory   where the project lives inside `source`, or empty
    editableRoot   the path to redirect to, in `editable` mode only
    backend        the PEP 517 backend, as `module` or `module:attribute`
    backendPath    PYTHONPATH for the backend, from the nodes that built it
    installerPath  PYTHONPATH for `installer`, in every mode but `sdist`
    out            the output path
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Protocol, cast


class Pep660Backend(Protocol):
    """The one hook of PEP 660 that an editable node calls.

    A backend is a module, and a module carries no type. The protocol says
    what the node needs from it, and the name of the method is the name that
    PEP 660 gives.
    """

    def build_editable(
        self,
        wheel_directory: str,
        config_settings: dict[str, str] | None = None,
        metadata_directory: str | None = None,
    ) -> str: ...


class Pep517SdistBackend(Protocol):
    """The one hook of PEP 517 that a `local-sdist` node calls.

    PEP 517 makes `build_sdist` mandatory, so every backend has it. It writes
    one archive into the directory that the caller names, and it returns the
    base name of that archive.
    """

    def build_sdist(
        self,
        sdist_directory: str,
        config_settings: dict[str, str] | None = None,
    ) -> str: ...


class Pep517WheelBackend(Protocol):
    """The one hook of PEP 517 that a `local-wheel` node calls."""

    def build_wheel(
        self,
        wheel_directory: str,
        config_settings: dict[str, str] | None = None,
        metadata_directory: str | None = None,
    ) -> str: ...


#: A `.pth` line that is a bare path becomes this, with the path substituted.
#: `site` runs a line that starts with `import`, and appends any other line to
#: `sys.path` verbatim. A verbatim line cannot expand a variable, so the line
#: has to become code.
PTH_LINE = "import sys; import os.path; sys.path.append(os.path.expandvars({path!r}))"

#: The hook that each mode calls. A mode that is not a key here is an error,
#: because `graph.nix` writes this value and no user does.
HOOKS = {"editable": "build_editable", "sdist": "build_sdist", "wheel": "build_wheel"}


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is empty: graph.nix must set it")
    return value


def in_tree_path(project: Path) -> list[str]:
    """The directories that `backend-path` of this project names.

    **A backend does not always come from the menu.** PEP 517 lets a project
    carry its own, with `backend-path` beside `build-backend`, and then
    `requires` is often empty because nothing has to be resolved at all:

        [build-system]
        requires = []
        build-backend = 'hatchling.ouroboros'
        backend-path = ['src']

    That is `hatchling` itself, which `tests/fixtures/locks/*-subdirectory.lock`
    both name. The paths are relative to the directory that holds
    `pyproject.toml`, and PEP 517 says they must stay inside it, so a path that
    leaves the project is an error here.
    """
    with (project / "pyproject.toml").open("rb") as handle:
        table = tomllib.load(handle)
    entries: list[str] = table.get("build-system", {}).get("backend-path", [])

    root = project.resolve()
    paths: list[str] = []
    for entry in entries:
        path = (root / entry).resolve()
        if not path.is_relative_to(root):
            raise RuntimeError(f"backend-path names '{entry}', which leaves {root}. PEP 517 forbids that.")
        paths.append(str(path))
    if paths:
        print(f"backend: in-tree, from {entries}", file=sys.stderr)
    return paths


def load_backend(name: str, hook: str) -> object:
    """Import the PEP 517 backend that the project asks for."""
    module, _, attribute = name.partition(":")
    backend: object = importlib.import_module(module)
    if attribute:
        backend = getattr(backend, attribute)
    if not hasattr(backend, hook):
        raise RuntimeError(f"the backend '{name}' has no {hook}, so it cannot build this form")
    return backend


def patch_pth(text: str, build_dir: str, root: str) -> tuple[str, bool]:
    """Redirect each bare path of a `.pth` file that the build directory owns."""
    lines: list[str] = []
    patched = False
    for line in text.splitlines():
        if line.startswith(build_dir):
            lines.append(PTH_LINE.format(path=root + line[len(build_dir) :]))
            patched = True
        else:
            lines.append(line)
    return "\n".join(lines) + "\n", patched


def redirect(out: Path, build_dir: str, root: str) -> int:
    """Rewrite every recorded build path under `out`. Return how many changed.

    **A `.py` file that names the build directory is not handled here.** The
    backends that write one, setuptools among them, put the path inside a
    string literal of a generated import finder. `pyproject.nix` rewrites that
    literal as a concrete syntax tree, with `libcst`, so that it changes the
    literal and nothing else. This example uses `flit_core`, which writes the
    `.pth` form only, so it raises rather than pretend to handle the other one.
    """
    changed = 0
    for path in sorted(out.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix == ".pth":
            text = path.read_text(encoding="utf-8")
            new_text, patched = patch_pth(text, build_dir, root)
            if patched:
                path.write_text(new_text, encoding="utf-8")
                changed += 1
                print(f"editable: redirected {path.name}", file=sys.stderr)
        elif path.suffix == ".py" and build_dir in path.read_text(encoding="utf-8", errors="ignore"):
            raise RuntimeError(
                f"{path} names the build directory. This backend writes an import "
                "finder, and that form needs the syntax tree rewrite that "
                "pyproject.nix does with libcst."
            )
    return changed


def writable_copy(source: Path) -> Path:
    """A copy of the project that the backend may write beside.

    **The backend records where it ran, and it is allowed to write there.** The
    store copy is read only, so neither is possible in it.
    """
    build_dir = Path.cwd() / "project"
    shutil.copytree(source, build_dir)
    for path in build_dir.rglob("*"):
        path.chmod(path.stat().st_mode | 0o200)
    return build_dir


def install(wheel: Path, out: Path, installer_path: str) -> None:
    """Install one wheel with `pypa/installer`, which is what a wheel node uses.

    The same installer, and the same command, that a node of the graph runs
    for a wheel of the index. An editable wheel is a wheel, and PEP 376 does
    not treat it apart.
    """
    subprocess.run(  # noqa: S603 -- every argument comes from the plan, and the plan is this build
        [sys.executable, "-m", "installer", "--prefix", str(out), str(wheel)],
        check=True,
        env={**os.environ, "PYTHONPATH": installer_path},
    )


def build_editable(project: Path, out: Path, backend_name: str, installer_path: str, root: str) -> None:
    """Build the PEP 660 wheel, install it, and redirect what it recorded."""
    dist = Path.cwd() / "dist"
    dist.mkdir()

    os.chdir(project)
    backend = cast("Pep660Backend", load_backend(backend_name, "build_editable"))
    wheel_name = backend.build_editable(str(dist))
    print(f"editable: {backend_name} wrote {wheel_name}", file=sys.stderr)
    os.chdir(dist.parent)

    install(dist / wheel_name, out, installer_path)

    # **The redirect names the tree that ran, and not the tree that was
    # copied.** A project below a subdirectory records that subdirectory, so
    # the path to rewrite is the one the backend ran in.
    changed = redirect(out, str(project), root)
    if changed == 0:
        raise RuntimeError(
            f"nothing under {out} named the build directory {project}. "
            "The install is not editable, so the environment would read the store copy."
        )

    print(f"editable: {root} is the tree that this install reads", file=sys.stderr)


def build_wheel(project: Path, out: Path, backend_name: str, installer_path: str) -> None:
    """Build a wheel out of the tree, and install it.

    **No archive stands between the tree and the wheel.** `pip install .` does
    this, and a git source of `uv.lock` needs it: a checkout is a directory,
    and the `build_sdist` step that a release takes would only add a file that
    nothing reads.

    The install holds the code, so it follows no tree of the user, the same
    way a release does.
    """
    dist = Path.cwd() / "dist"
    dist.mkdir()

    os.chdir(project)
    backend = cast("Pep517WheelBackend", load_backend(backend_name, "build_wheel"))
    wheel_name = backend.build_wheel(str(dist))
    print(f"wheel: {backend_name} wrote {wheel_name}", file=sys.stderr)
    os.chdir(dist.parent)

    install(dist / wheel_name, out, installer_path)


def build_sdist(project: Path, out: Path, backend_name: str) -> None:
    """Build the source distribution, and leave it in the output.

    **The output of this node is one archive, under the name that the backend
    gave it.** That name follows PEP 625, so it is not the name of the project:
    `packaged-app` writes `packaged_app-0.1.0.tar.gz`. `graph.nix` gives this
    output to a node of kind `sdist-build`, which finds the archive by its
    extension rather than predict that name.
    """
    out.mkdir(parents=True, exist_ok=True)

    os.chdir(project)
    backend = cast("Pep517SdistBackend", load_backend(backend_name, "build_sdist"))
    archive = backend.build_sdist(str(out))
    os.chdir(out.parent)

    size = (out / archive).stat().st_size
    print(f"sdist: {backend_name} wrote {archive}, {size} bytes", file=sys.stderr)


def main() -> int:
    mode = env("mode")
    if mode not in HOOKS:
        raise RuntimeError(f"mode is {mode!r}, and this script builds one of {sorted(HOOKS)}")

    out = Path(env("out"))
    backend_name = env("backend")
    backend_path = os.environ.get("backendPath", "")  # noqa: SIM112 -- Nix names a derivation attribute in lower case
    subdirectory = os.environ.get("subdirectory", "")  # noqa: SIM112 -- same

    build_dir = writable_copy(Path(env("source")))
    # **The backend runs where `pyproject.toml` is, and that is not always the
    # top of the tree.** A git source of `uv.lock` may name a subdirectory,
    # and `hatchling` inside `pypa/hatch` is the case that this lab reads.
    project = build_dir / subdirectory if subdirectory else build_dir
    if not (project / "pyproject.toml").exists():
        raise RuntimeError(f"{project} holds no pyproject.toml, so no backend declaration reaches this build")

    # **The in-tree paths go first, and PEP 517 says so.** A project that
    # carries its own backend means that one, and not a package of the same
    # name that the plan happened to install.
    sys.path[:0] = in_tree_path(project) + [entry for entry in backend_path.split(":") if entry]

    if mode == "editable":
        build_editable(project, out, backend_name, env("installerPath"), env("editableRoot"))
    elif mode == "wheel":
        build_wheel(project, out, backend_name, env("installerPath"))
    else:
        build_sdist(project, out, backend_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
