#!/usr/bin/env bash
# Build a wheel from an sdist with PEP 517, and then install it.
#
# **This node is the reason the graph has to be a graph.** The backend that
# builds this source is another node of the same graph, and the planner chose
# it from the same lock file. A planner that emits one derivation cannot
# express that: the backend would have to exist before the plan runs.
#
# The environment gives:
#   sdist          the source archive, or a directory that holds one. A fetch
#                  of the graph gives the file, and a node of kind
#                  `local-sdist` gives the directory, because the name that
#                  PEP 625 puts on the archive is not the name of the project.
#   backend        the PEP 517 backend, as `module.submodule`
#   backendPath    PYTHONPATH for the backend, from the nodes that built it
#   installerPath  PYTHONPATH for `installer`, from the node that unpacked it
#   out            the output path
# SC2154: graph.nix passes each variable below as an attribute of the
# derivation, so the build environment holds it.
# shellcheck disable=SC2154
set -eu

root="$PWD"
mkdir -p "$root/src" "$root/wheel"

# **A local project arrives as a directory, and an artefact of the index
# arrives as a file.** The backend of the local one names its own archive, and
# PEP 625 normalises that name, so the node that built it cannot be asked for
# the name in advance. One archive per directory is the rule, and this is where
# a second one becomes an error rather than a silent choice.
archive="$sdist"
if [ -d "$archive" ]; then
  found=("$archive"/*.tar.gz)
  if [ "${#found[@]}" -ne 1 ]; then
    echo "expected one .tar.gz in $archive, and found ${#found[@]}" >&2
    exit 1
  fi
  archive="${found[0]}"
  echo "sdist: $(basename "$archive"), built by a node of this graph"
fi

tar -xzf "$archive" -C "$root/src" --strip-components=1

# **The project is not always at the top of the archive.** `uv.lock` writes
# `source = { url = "…", subdirectory = "backend" }` for an archive that holds
# more than one project, and `hatchling` inside `pypa/hatch` is that case. An
# archive of the index leaves this empty.
#
# **A missing `pyproject.toml` is not an error, and a missing subdirectory
# is.** PEP 517 gives a fallback backend for a distribution that declares
# none, and the planner applied it. A subdirectory that the archive does not
# hold is a different thing: the lock file named it, so the archive is not the
# one that the lock file described.
project="$root/src${subdirectory:+/$subdirectory}"
if [ ! -d "$project" ]; then
  echo "$archive holds no '$subdirectory', and uv.lock says that the project is there" >&2
  exit 1
fi

cd "$project"
# The backend runs with the built backend nodes on its path, and with nothing
# else. There is no network here, and no index.
PYTHONPATH="$backendPath" python3 -c '
import importlib
import sys
import tomllib
from pathlib import Path

backend_name, wheel_directory = sys.argv[1], sys.argv[2]

# **A project may carry its own backend, and PEP 517 calls that backend-path.**
# The requires list is then often empty, because nothing has to be resolved at
# all. hatchling is that shape, and the archive of pypa/hatch is what a URL
# source of uv.lock names. The paths are relative to this directory, and they
# go first, so an in-tree backend wins over a package of the same name.
#
# A shell comment may not hold a backtick here. This script runs the block
# below in single quotes, and shellcheck reads a backtick there as a command
# that this script did not intend to run.
declaration = Path("pyproject.toml")
table = tomllib.loads(declaration.read_text()) if declaration.exists() else {}
for entry in reversed(table.get("build-system", {}).get("backend-path", [])):
    path = (Path.cwd() / entry).resolve()
    if not path.is_relative_to(Path.cwd()):
        raise RuntimeError(f"backend-path names {entry!r}, which leaves the project")
    sys.path.insert(0, str(path))

module, _, attribute = backend_name.partition(":")
backend = importlib.import_module(module)
if attribute:
    backend = getattr(backend, attribute)
print(backend.build_wheel(wheel_directory))
' "$backend" "$root/wheel"
cd "$root"

# The wheel that this node built goes through the same installer as one that
# the lock file named, so both give the same shape of output.
for built in "$root"/wheel/*.whl; do
  PYTHONPATH="$installerPath" python3 -m installer --prefix "$out" "$built"
done
