# Run the environment that `uv.nix` built out of two files of the project.
#
# `./check-uv.nix` does the same for `projects/pure-app`, and it gives the
# reason that this runs as a derivation and not from a shell: a store path is
# absolute, and a chroot store puts the store under a prefix.
#
# **This project is the one whose dependencies have no address.** `uv.lock`
# names each one by a path relative to the project, so the menu that `uv.nix`
# builds holds no fetch for either. `projects/vendored-app/README.md` gives the
# two shapes that a `path` source takes.
#
# **The imports live here, and not in the script**, the way `./check-git.nix`
# keeps `flit_core` out of `scripts/check-git.py`: only the graph installs these
# packages, so no type checker of the repository can resolve them.
{
  pkgs ? import <nixpkgs> { },
  nanopynixEnv,
  project ? ./projects/vendored-app,
}:

let
  planner = import ./uv.nix { inherit pkgs nanopynixEnv project; };
  venv = builtins.outputOf planner.outPath "out";
in
derivation {
  name = "vendored-check";
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
        "${venv}/bin/python" ${./scripts/check-vendored.py} || return 1

        # **The wheel came out of a file, and the archive came out of a
        # build.** Both must import from inside the environment: a `path`
        # source is not a tree that the user edits, so nothing redirects to it.
        for name in vendored archived; do
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
