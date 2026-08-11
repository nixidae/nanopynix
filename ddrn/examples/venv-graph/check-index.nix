# Run the environment that `uv.nix` built out of a registry of this machine.
#
# `./check-uv.nix` does the same for `projects/pure-app`, and it gives the
# reason that this runs as a derivation and not from a shell: a store path is
# absolute, and a chroot store puts the store under a prefix.
#
# **This project is the one whose location has three parts.** `uv.lock` puts
# the directory of the registry on the source and the name of the file on the
# row of the artefact, so a reader that joined the row to the project would
# name a file that does not exist. `projects/local-index/README.md` gives the
# whole shape, and `ddrn/PACKAGING-FACTS.md` names the line of uv that joins
# the same three parts.
#
# **The imports live here, and not in the script**, the way `./check-git.nix`
# keeps `flit_core` out of `scripts/check-git.py`: only the graph installs these
# packages, so no type checker of the repository can resolve them.
{
  pkgs ? import <nixpkgs> { },
  nanopynixEnv,
  project ? ./projects/local-index,
}:

let
  planner = import ./uv.nix { inherit pkgs nanopynixEnv project; };
  venv = builtins.outputOf planner.outPath "out";
in
derivation {
  name = "index-check";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  args = [
    "-c"
    ''
      set -eu

      # The report is the output, so a failure would otherwise say nothing.
      # `./check-uv.nix` gives the whole reason, and the reason that each
      # command below names its own failure.
      report() {
        "${venv}/bin/python" ${./scripts/check-index.py} || return 1

        # **The wheel came out of the index, and the archive was built out of
        # it.** Both must import from inside the environment: a registry of
        # this machine is not a tree that the user edits, so nothing
        # redirects to it.
        for name in stocked crated; do
          module="$("${venv}/bin/python" -c "import $name; print($name.__file__)")" || return 1
          echo "$name"
          printf '  %s\n' "$module"
          case "$module" in
          "${venv}"/*) ;;
          *)
            echo "$module is outside ${venv}, so this install follows a tree" >&2
            return 1
            ;;
          esac
        done
      }

      if ! report > "$out" 2>&1; then
        while IFS= read -r line; do printf '%s\n' "$line" >&2; done < "$out"
        exit 1
      fi
    ''
  ];
}
