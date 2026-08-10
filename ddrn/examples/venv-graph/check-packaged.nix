# Run the environment of `projects/packaged-app`, inside the store that holds
# it.
#
# `./check-uv.nix` does the same for `projects/pure-app`, and `./check.nix`
# gives the reason that this runs as a derivation and not from a shell.
{
  pkgs ? import <nixpkgs> { },
  nanopynixEnv,
  project ? ./projects/packaged-app,
  # The tree that the editable install reads. `uv.nix` redirects the install at
  # `$DDRN_EDITABLE_ROOT`, which this derivation sets, so a different tree here
  # reads back differently **from the same environment**. `run-uv.sh` proves
  # that by building this twice.
  editableTree ? ./projects/packaged-app,
}:

let
  planner = import ./uv.nix { inherit pkgs nanopynixEnv project; };
  venv = builtins.outputOf planner.outPath "out";
in
derivation {
  name = "packaged-check";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  DDRN_EDITABLE_ROOT = "${editableTree}";

  args = [
    "-c"
    ''
      set -eu

      # The report is the output, so read it back to stderr when the check
      # refuses. `./check-uv.nix` gives the whole reason, and the two `return 1`
      # lines below with it.
      report() {
        "${venv}/bin/python" ${./scripts/check-packaged.py} || return 1

        # The console script that `[project.scripts]` names, which exists only
        # because `pypa/installer` wrote it from `entry_points.txt`.
        script="$("${venv}/bin/packaged-app")" || return 1
        echo "console script"
        printf '  %s\n' "$script"
      }

      if ! report > "$out" 2>&1; then
        while IFS= read -r line; do printf '%s\n' "$line" >&2; done < "$out"
        exit 1
      fi
    ''
  ];
}
