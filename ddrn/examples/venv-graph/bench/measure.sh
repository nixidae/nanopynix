#!/usr/bin/env bash
# Measure what the menu of one real `uv.lock` costs to evaluate.
#
# **This measurement needs no network and builds nothing.** It instantiates
# `../uv.nix` over `./uv.lock` and reads the result. Instantiation writes a
# `.drv` for every artefact of the lock file and downloads none of them, which
# is the property that the whole design rests on. `ddrn/PACKAGING-FACTS.md`
# holds the numbers that this script produced, and it names this script.
#
# The environment of the planner is a constant here, and not the subject. The
# menu is the subject, and `uv.nix` builds the same menu whatever the planner
# is. So this script takes any store path, and it takes `pkgs.hello` by
# default, because a real nanopynix environment costs a 20 minute build and
# changes no number below.
#
# **A cold run needs an empty store, and not merely a first run.** A first run
# against the store of this machine finds every `.drv` already written, and it
# reports the warm number under the cold name. `--cold` therefore copies the
# environment into a store that this script makes, and instantiates there.
#
# Usage: ddrn/examples/venv-graph/bench/measure.sh [--cold] [env-store-path]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../../../.." && pwd)"

features='nix-command flakes ca-derivations dynamic-derivations'

cold=0
if [ "${1:-}" = "--cold" ]; then
  cold=1
  shift
fi

env_path="${1:-}"
if [ -z "$env_path" ]; then
  echo "==> building the placeholder environment"
  env_path="$(nix build --file "$repo" pkgs.hello --no-link --print-out-paths)"
fi
echo "==> planner environment: $env_path"

packages="$(grep -c '^\[\[package\]\]' "$here/uv.lock")"
echo "==> uv.lock holds $packages packages, $(wc -c <"$here/uv.lock") bytes"

work="$(mktemp -d)"

# **A store path is read only, so `rm -rf` alone leaves the directory.** The
# cold run makes a store under this directory, and every path in it comes out
# with no write bit. `chmod` gives the bits back first.
cleanup() {
  chmod -R u+w "$work" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

# The store that each instantiation writes to. A warm run uses the store of
# this machine, and `--cold` uses an empty one that holds the environment and
# nothing else.
store_args=()
if [ "$cold" = 1 ]; then
  echo "==> seeding an empty store at $work/store"
  nix copy --to "$work/store" --no-check-sigs "$env_path" \
    --extra-experimental-features 'nix-command flakes'
  store_args=(--store "$work/store")
fi

instantiate() {
  nix-instantiate "${store_args[@]}" "$here/../uv.nix" \
    --argstr nanopynixEnv "$env_path" \
    --arg project "$here" \
    --add-root "$1" 2>/dev/null
}

# `date +%s%N` gives nanoseconds as an integer, and bash divides integers. A
# shell needs no `bc` for a number of seconds with two decimal places.
elapsed() {
  local delta=$(($(date +%s%N) - $1))
  printf '    %d.%02d s\n' "$((delta / 1000000000))" "$((delta % 1000000000 / 10000000))"
}

if [ "$cold" = 1 ]; then
  echo "==> instantiation into the empty store, which writes every .drv"
else
  echo "==> instantiation, with every .drv of an earlier run already present"
fi
start="$(date +%s%N)"
drv="$(instantiate "$work/first")"
elapsed "$start"

echo "==> three more, with every .drv now in that store"
for i in 1 2 3; do
  start="$(date +%s%N)"
  instantiate "$work/warm-$i" >/dev/null
  elapsed "$start"
done

echo "==> the menu that the planner reads"
nix "${store_args[@]}" derivation show "$drv" --extra-experimental-features "$features" |
  python3 "$here/report.py"

echo "==> the closure of that .drv"
paths="$(nix "${store_args[@]}" path-info --recursive --extra-experimental-features "$features" "$drv")"
printf '    %s paths\n' "$(echo "$paths" | wc -l)"
printf '    %s of them .drv\n' "$(echo "$paths" | grep -c '\.drv$')"
nix "${store_args[@]}" path-info --closure-size --extra-experimental-features "$features" "$drv" |
  while read -r _ bytes; do printf '    %s bytes\n' "$bytes"; done

# **The menus are most of that.** Each planner of a source distribution carries
# its own copy of the whole menu, at every level of the recursion.
echo "==> how much of the derivation text is a copy of the menu"
prefix=""
if [ "$cold" = 1 ]; then prefix="$work/store"; fi
echo "$paths" | grep '\.drv$' | python3 "$here/menus.py" "$prefix"
