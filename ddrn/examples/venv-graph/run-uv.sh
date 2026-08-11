#!/usr/bin/env bash
# Build the real `uv` projects of `./projects` as graphs of derivations.
#
# `./run.sh` does the same for the hand-written `lock.json` of this example,
# and it explains why the build happens in a private chroot store. This script
# is that one, pointed at `./uv.nix`.
#
# **The lock file comes from `uv`, and nothing here rewrites it.**
# `projects/README.md` gives the command that wrote each project. `uv.nix`
# reads the lock file with `builtins.fromTOML`, makes one `fetchurl` for each
# artefact, and resolves nothing. The planner resolves, inside the sandbox,
# with `packaging`.
#
# Seven projects run, and each one exercises what the ones before it cannot:
#
#   pure-app      a virtual root, and every artefact a pure wheel
#   packaged-app  a PEP 660 editable of the project, built by `hatchling`,
#                 whose backend needs a dependency closure; and one package
#                 built from its source distribution because
#                 `tool.uv.no-binary-package` names it. The same project also
#                 builds a second time as a release: `build_sdist` writes the
#                 archive that an index would serve, and `build_wheel` builds
#                 the wheel out of that archive.
#   workspace     two members of one lock file, each an editable of its own
#                 directory, and one member that reads the other. Two
#                 selections of that one lock file build two environments, and
#                 one more build ships one member and links the other.
#   binary-app    a manylinux wheel that holds compiled code, which
#                 `auto-patchelf` has to reach. The same project builds a
#                 second time with `pyzmq` taken from nixpkgs instead.
#   git-app       a dependency that comes from a git repository, and from a
#                 subdirectory of it. `uv.lock` records a commit and no file,
#                 so nothing of that package can be downloaded: the graph
#                 fetches the checkout and builds a wheel out of the tree.
#   vendored-app  two dependencies that no index serves. `uv.lock` names each
#                 one by a path of the project, so no artefact of either has an
#                 address. One is a wheel, and the other is an archive that the
#                 graph builds.
#   local-index   a registry that is a directory of the project. The source
#                 names that directory and the row of the artefact names the
#                 file under it, so the location has three parts and not two.
#
# The graph downloads from PyPI, so this script needs a network. `git-app`
# needs one for a second reason: `uv.nix` clones the repository of a git source
# during the evaluation, because `uv.lock` records no hash of that tree.
#
# Usage: ddrn/examples/venv-graph/run-uv.sh [work-directory]
set -euo pipefail

work="${1:-${TMPDIR:-/tmp}/ddrn-venv-graph-uv}"
mkdir -p "$work"
store="$work/store"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"

features='nix-command flakes ca-derivations dynamic-derivations'

echo "==> building the patched Nix"
FLAKE_COMPATISH_DISABLE_OVERRIDES=1 nix build --impure \
  --expr "(import $repo { }).nanopynixMaster.nix-cli" \
  --out-link "$work/nix-patched" --print-build-logs

nix_patched="$(readlink -f "$work/nix-patched")"
echo "==> $("$nix_patched/bin/nix" --version)"

echo "==> building nanopynix against that same Nix"
FLAKE_COMPATISH_DISABLE_OVERRIDES=1 nix build --impure \
  --expr "(import $repo { }).nanopynixMaster.pythonSet.mkVirtualEnv \"ddrn-planner-env\" { nanopynix = [ ]; }" \
  --out-link "$work/nanopynix-env" --print-build-logs

nanopynix_env="$(readlink -f "$work/nanopynix-env")"
echo "==> nanopynix env at $nanopynix_env"

echo "==> seeding the private store at $store"
mkdir -p "$store"
# `--no-check-sigs` is needed and is safe here, for the reason that `./run.sh`
# gives: both inputs were built on this machine, and the store is a private
# directory that this script created.
"$nix_patched/bin/nix" copy --to "$store" --no-check-sigs "$nix_patched" "$nanopynix_env" \
  --extra-experimental-features 'nix-command flakes'

# Build one file of this directory in the private store, and print the output.
build() {
  "$nix_patched/bin/nix" build \
    --store "$store" \
    --file "$here/$1" \
    --argstr nanopynixEnv "$nanopynix_env" \
    "${@:2}" \
    --impure \
    --no-link --print-out-paths --print-build-logs \
    --extra-experimental-features "$features" \
    --system-features 'builder-rpc-v0 recursive-nix'
}

show() {
  "$nix_patched/bin/nix" --store "$store" \
    --extra-experimental-features "$features" "$@"
}

# Each entry is a directory of `./projects`, and the name that the environment
# of it takes. The two differ for the workspace, whose directory is `workspace`
# and whose root project is `workspace-root`.
for entry in pure-app:pure-app packaged-app:packaged-app workspace:workspace-root binary-app:binary-app git-app:git-app vendored-app:vendored-app local-index:local-index; do
  project="${entry%%:*}"
  expected="${entry##*:}"
  echo
  echo "############ $project ############"

  echo "==> resolving the environment from uv.lock"
  drv="$(build uv.nix --arg project "$here/projects/$project")"

  echo "==> the planner submitted $drv"
  case "$drv" in
  *-"$expected".drv) ;;
  *)
    echo "expected an output named '$expected.drv', got '$drv'" >&2
    exit 1
    ;;
  esac

  echo "==> the root of the graph reads:"
  show derivation show "$drv" | grep -o '"[^"]*\.drv"' | sort -u
done

# **The check runs as a derivation, and not from this shell**, because a chroot
# store puts the store under a prefix and every symlink of the environment is
# absolute. Each check asserts that the environment holds exactly what
# `uv.lock` resolves, and that it does not hold the tools that built it.
echo
echo "==> building and running pure-app"
show store cat "$(build check-uv.nix)"

echo
echo "==> building and running packaged-app"
show store cat "$(build check-packaged.nix)"

# **The editable proof.** The environment installed `packaged-app` from its own
# tree, and the `.pth` that `hatchling` wrote expands `$DDRN_EDITABLE_ROOT`
# when the interpreter starts. So a second tree reads back differently from the
# **same** environment, and nothing about the environment is rebuilt.
echo
echo "==> the same environment, a different tree"
other="$work/other-tree"
rm -rf "$other"
cp -R "$here/projects/packaged-app" "$other"
chmod -R u+w "$other"
sed -i 's|^WHICH_TREE = .*|WHICH_TREE = "a tree that this run wrote a minute ago"|' \
  "$other/src/packaged_app/__init__.py"

check_other="$(build check-packaged.nix --arg editableTree "$other")"
show store cat "$check_other"

# The environment is one store path, and both checks read it. Prove that,
# rather than say it: each report names the prefix that it ran under, and the
# two have to agree.
#
# The `prefix` line is what the report gives, and a reference of the check
# derivation is not: the tree that the editable reads is a second store path
# whose name also ends in `packaged-app`.
echo
echo "==> both checks ran under the same environment:"
for path in "$(build check-packaged.nix)" "$check_other"; do
  show store cat "$path" | sed -n 's/^prefix  *//p'
done

# **The same project, shipped rather than linked.** `uv build` writes a source
# distribution, an index serves it, and a user who installs from it runs the
# PEP 517 `build_wheel` hook over what the archive holds. The graph performs
# both steps, as two nodes, and the archive between them is a store path.
#
# The report differs from the editable one in the line that says where the
# module came from. This environment holds the code, so the tree that
# `$DDRN_EDITABLE_ROOT` names answers for nothing.
echo
echo "==> the same project, installed from its own source distribution"
show store cat "$(build check-sdist.nix)"

echo
echo "==> and it ignores the tree that the editable install follows"
show store cat "$(build check-sdist.nix --arg editableTree "$other")"

# **One lock file, and two environments.** `uv.lock` records no selection, so
# the caller names the members to install. The first selection takes every
# member, which is what `uv sync --all-packages` takes, and the second takes
# one member of the two. The `ws-cli` line of the first report reads the other
# member, so it also proves the edge between two editables of one workspace.
every_member='["ws-cli" "ws-core" "rich" "markdown-it-py" "mdurl" "pygments"]'

echo
echo "==> the whole workspace"
show store cat "$(build check-workspace.nix \
  --argstr venvName workspace-all --arg expect "$every_member")"

echo
echo "==> one member of that same lock file"
show store cat "$(build check-workspace.nix \
  --argstr venvName workspace-core \
  --arg select '{ "ws-core" = []; }' --arg expect '["ws-core"]')"

# **A member redirects at its own directory, and not at the workspace.** Each
# member lives under the tree that `$DDRN_EDITABLE_ROOT` names, so the
# directory of the member travels into the `.pth` with it. A second copy of the
# whole workspace proves that: one edit to `ws-core` reaches both reports,
# because `ws-cli` reads `ws-core`.
echo
echo "==> the same environment, a different workspace tree"
other_ws="$work/other-workspace"
rm -rf "$other_ws"
cp -R "$here/projects/workspace" "$other_ws"
chmod -R u+w "$other_ws"
sed -i 's|^WHICH_TREE = .*|WHICH_TREE = "a tree that this run wrote a minute ago"|' \
  "$other_ws/packages/ws-core/src/ws_core/__init__.py"

show store cat "$(build check-workspace.nix \
  --argstr venvName workspace-all --arg expect "$every_member" \
  --arg editableTree "$other_ws")"

# **One member ships, and the other one still links.** `ws-core` installs from
# the archive that its own backend wrote, so it holds its own code and stops
# following the tree. `ws-cli` stays a PEP 660 editable, and it imports
# `ws_core`, so this build also proves that the two forms compose.
#
# The tree is the edited copy again, and the two members now disagree about
# what they read: `ws-cli` reports the edit, and `ws-core` reports the store.
echo
echo "==> one member from its own sdist, and the other one editable"
show store cat "$(build check-workspace.nix \
  --argstr venvName workspace-core-sdist --arg expect "$every_member" \
  --arg fromSdist '["ws-core"]' --arg editableTree "$other_ws")"

# **A wheel that holds compiled code, and the same package from nixpkgs.** The
# first build takes the manylinux wheel that `uv.lock` pins, and
# `auto-patchelf` rewrites its `RPATH` so the loader of this system can resolve
# it. The second takes `pkgs.python3Packages.pyzmq` instead, through the tier 4
# menu that `default.nix` documents, and the graph builds no wheel at all.
#
# Both reports run the same code, so the difference is only where the code came
# from: the wheel carries its own copy of `libzmq` inside `pyzmq.libs`, and the
# package of nixpkgs links against the `zeromq` of the store.
nixpkgs_pyzmq='{ pyzmq = (import <nixpkgs> {}).python3Packages.pyzmq; }'

echo
echo "==> a manylinux wheel, patched by auto-patchelf"
show store cat "$(build check-binary.nix)"

echo
echo "==> the same project, with pyzmq from nixpkgs"
show store cat "$(build check-binary.nix \
  --argstr venvName binary-nixpkgs --arg fromNixpkgs "$nixpkgs_pyzmq")"

# **A dependency that no index serves.** `uv.lock` records a commit of
# `pypa/flit` and the subdirectory that holds `flit_core`, and it records no
# `wheels` table and no `sdist` table. So the menu holds nothing to download
# for that package, and the environment can only hold it because `uv.nix`
# fetched the repository and the graph ran one PEP 517 `build_wheel` call over
# `flit_core/` of the checkout.
#
# `flit_core` builds itself, with the `backend-path` of PEP 517, so this build
# also proves that a project may carry a backend that no menu holds.
echo
echo "==> a dependency from a git checkout, below a subdirectory"
show store cat "$(build check-git.nix)"

# **The last project has no address for anything that it installs.** `uv.lock`
# names each of its two dependencies by a path relative to the project, so the
# menu of `uv.nix` holds no fetch for either. One of the two is a wheel, which
# the store takes whole. The other is an archive, so the graph plans it, builds
# it with the backend that the lock file pins, and installs the wheel that came
# out.
echo
echo "==> two dependencies that no index serves"
show store cat "$(build check-vendored.nix)"

# **The last project keeps the directory of the registry on the source, and the
# name of the file on the row.** So the location has three parts: the project,
# the registry and the row. uv joins the same three in `to_registry_wheel`, and
# a reader that joined the row to the project alone would name a file that does
# not exist.
echo
echo "==> a registry that is a directory of the project"
show store cat "$(build check-index.nix)"
