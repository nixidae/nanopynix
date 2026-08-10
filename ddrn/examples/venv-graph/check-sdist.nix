# Run the environment of `projects/packaged-app`, with the project installed
# from its own source distribution.
#
# `./check-packaged.nix` builds the same project as a PEP 660 editable, and
# `./check.nix` gives the reason that this runs as a derivation and not from a
# shell. One argument separates the two: `fromSdist` names the member to
# install from the archive that `build_sdist` writes.
{
  pkgs ? import <nixpkgs> { },
  nanopynixEnv,
  project ? ./projects/packaged-app,
  venvName ? "packaged-sdist",
  # **A tree that this environment must ignore.** An editable install of this
  # same project reads whatever this names. A release holds its own code, so
  # `scripts/check-sdist.py` refuses when the module comes from here.
  editableTree ? ./projects/packaged-app,
}:

let
  planner = import ./uv.nix {
    inherit
      pkgs
      nanopynixEnv
      project
      venvName
      ;
    fromSdist = [ "packaged-app" ];
  };
  venv = builtins.outputOf planner.outPath "out";
in
derivation {
  name = "sdist-check";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  DDRN_EDITABLE_ROOT = "${editableTree}";

  args = [
    "-c"
    ''
      set -eu

      # The report is the output, so read it back to stderr when the check
      # refuses. `./check-uv.nix` gives the whole reason.
      report() {
        "${venv}/bin/python" ${./scripts/check-sdist.py} || return 1

        # The console script that `[project.scripts]` names. A wheel that came
        # out of an archive goes through the same installer as one that the
        # index served, so this file exists for the same reason.
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
