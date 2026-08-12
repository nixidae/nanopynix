#!/usr/bin/env bash
# Show a real `uv` project become a graph of derivations, one step at a time.
#
# **This script is the demonstration, and `tests/test_projects.py` is the
# gate.** That module builds all seven projects of `./projects` and reads each
# report back, and it fails when one of them changes. This script builds two of
# them and prints what each step produced, so that a reader sees the two-step
# build rather than a pass mark.
#
# `./run.sh` does the same for the hand-written `lock.json` of this example, and
# it explains why the build happens in a private chroot store.
#
# **The lock file comes from `uv`, and nothing here rewrites it.**
# `projects/README.md` gives the command that wrote each project. `uv.nix` reads
# the lock file with `builtins.fromTOML`, makes one `fetchurl` for each
# artefact, and resolves nothing. The planner resolves, inside the sandbox, with
# `packaging`.
#
# The graph downloads from PyPI, so this script needs a network.
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

# **Step one: the planner runs, and it submits a derivation.** The output of
# `uv.nix` is not an environment. It is the root `.drv` of the graph that the
# planner registered from inside its own sandbox, and the name of that root is
# the name of the environment.
echo
echo "############ step one: the planner ############"
drv="$(build uv.nix --arg project "$here/projects/packaged-app")"
echo "==> the planner submitted $drv"

echo "==> the root of the graph names these derivations:"
show derivation show "$drv" | grep -o '"[^"]*\.drv"' | sort -u

# **Step two: the graph realises.** `builtins.outputOf` reads the output of the
# planner as a derivation, and Nix builds the graph that the planner wrote.
#
# **The check runs as a derivation, and not from this shell**, because a chroot
# store puts the store under a prefix and every symlink of the environment is
# absolute. The check asserts that the environment holds exactly what `uv.lock`
# resolves, and that it does not hold the tools that built it.
echo
echo "############ step two: the graph ############"
show store cat "$(build check-packaged.nix)"

# **`env` is those two steps as one command.** The result of `uv.nix` is the
# planner, so the build above gave a `.drv` and a second build gave the graph.
# `env` is a derivation whose whole content is one symbolic link to the
# environment, so one build reaches it. `application.nix` gives the reason a
# wrapper is necessary: `builtins.outputOf` gives a string, and `nix build`
# takes a derivation.
echo
echo "############ the same environment, in one command ############"
wrapper="$(build uv.nix env --arg project "$here/projects/packaged-app")"
echo "==> one build gave $wrapper, which holds one link:"
show store ls --long "$wrapper"

# **The editable proof.** The environment installed `packaged-app` from its own
# tree, and the `.pth` that `hatchling` wrote expands `$DDRN_EDITABLE_ROOT` when
# the interpreter starts. So a second tree reads back differently from the
# **same** environment, and nothing about the environment is rebuilt.
#
# Each report names the prefix that it ran under, and the two prefixes have to
# agree. A reference of the check derivation would not prove this: the tree that
# the editable reads is a second store path whose name also ends in
# `packaged-app`.
echo
echo "############ one environment, two trees ############"
other="$work/other-tree"
rm -rf "$other"
cp -R "$here/projects/packaged-app" "$other"
chmod -R u+w "$other"
sed -i 's|^WHICH_TREE = .*|WHICH_TREE = "a tree that this run wrote a minute ago"|' \
  "$other/src/packaged_app/__init__.py"

show store cat "$(build check-packaged.nix --arg editableTree "$other")"

echo
echo "==> both reports ran under the same environment:"
for path in "$(build check-packaged.nix)" "$(build check-packaged.nix --arg editableTree "$other")"; do
  show store cat "$path" | sed -n 's/^prefix  *//p'
done

# **One member as a program, and not as an environment.** `uv sync` gives a
# virtual environment, and a virtual environment carries `bin/python`,
# `bin/activate` and the console script of every member. A NixOS configuration
# that installs one therefore shadows the interpreter of the system.
#
# The `application` argument renders the tree of one member over that same
# environment, so the output holds `bin/ws-cli` and nothing else.
# `pyproject.nix` calls this `util.mkApplication`.
#
# **The closure does not shrink**, and the report says so: each entry of the
# application is a link into the environment, so the environment and the
# interpreter stay. What changes is what reaches the PATH of a user.
echo
echo "############ one member of a workspace, as an application ############"
show store cat "$(build check-application.nix)"

# **The same environment as a shell, and the editable tree that it reads.**
# `shell` sets `DDRN_EDITABLE_ROOT` to the project, so an editable member
# imports the tree that the user edits. This script prints the command rather
# than run it, because a shell needs a terminal.
echo
echo "==> the same environment as a development shell:"
cat <<COMMAND
==>   $nix_patched/bin/nix develop --store $store \\
==>     --file $here/uv.nix shell \\
==>     --argstr nanopynixEnv $nanopynix_env \\
==>     --arg project $here/projects/packaged-app \\
==>     --impure \\
==>     --extra-experimental-features '$features' \\
==>     --system-features 'builder-rpc-v0 recursive-nix'
COMMAND

echo
echo '==> tests/test_projects.py builds the other five projects, and every'
echo "==> check of this directory. Run it with:"
echo "==>   direnv exec . pytest ddrn/examples/venv-graph/tests"
