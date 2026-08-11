# Run the application of `projects/workspace`, inside the store that holds it.
#
# `./check-workspace.nix` runs the environment of the same project, and
# `./check.nix` gives the reason that this runs as a derivation and not from a
# shell.
#
# **An application is one program, and an environment is every program.** The
# `application` argument of `./uv.nix` renders the tree of one member over the
# environment, so the two things below are what this check exists for:
#
#   1. `bin/ws-cli` runs, and it gives the same answer as the one of the
#      environment.
#   2. `bin/python` is not there, and neither is the console script of any
#      other member. That is what makes the output safe to put in
#      `environment.systemPackages` of a NixOS configuration.
{
  pkgs ? import <nixpkgs> { },
  nanopynixEnv,
  project ? ./projects/workspace,
  editableTree ? ./projects/workspace,
  # The member to render, and the program that it installs. `ws-cli` names both
  # here, and `projects/workspace/packages/ws-cli/pyproject.toml` is why.
  application ? "ws-cli",
}:

let
  planner = import ./uv.nix {
    inherit
      pkgs
      nanopynixEnv
      project
      application
      ;
    venvName = "workspace-app";
  };
  app = builtins.outputOf planner.outPath "out";
in
derivation {
  name = "workspace-app-check";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  DDRN_EDITABLE_ROOT = "${editableTree}";

  args = [
    "-c"
    ''
      set -eu

      report() {
        echo "prefix  ${app}"

        # The program runs. It is a symbolic link into the environment, and the
        # shebang of the script in that environment names the `bin/python` of
        # the environment, so the interpreter that it finds is the one that
        # holds every member.
        echo "console script"
        printf '  %s\n' "$("${app}/bin/${application}")" || return 1

        # **What the application does not carry.** Each name below is in the
        # environment, and none of them belongs to this member.
        echo "absent"
        for name in python python3 activate pygmentize markdown-it; do
          if [ -e "${app}/bin/$name" ]; then
            echo "  ${application} carries bin/$name, and an application holds one program" >&2
            return 1
          fi
          printf '  bin/%s\n' "$name"
        done

        # The whole output, so that a reader sees how small it is.
        echo "holds"
        ${pkgs.findutils}/bin/find "${app}" -mindepth 1 -printf '  %P\n' | ${pkgs.coreutils}/bin/sort
      }

      if ! report > "$out" 2>&1; then
        while IFS= read -r line; do printf '%s\n' "$line" >&2; done < "$out"
        exit 1
      fi
    ''
  ];
}
