# Run the application of one project, inside the store that holds it.
#
# `./check-workspace.nix` runs the environment of the same project, and
# `./check.nix` gives the reason that this runs as a derivation and not from a
# shell.
#
# **An application is some programs, and an environment is every program.** The
# `application` argument of `./uv.nix` renders the tree of the named members
# over the environment, so the three things below are what this check exists
# for:
#
#   1. Each named program runs, and it gives the same answer as the one of the
#      environment.
#   2. `bin/python` is not there, and neither is the console script of any
#      member that the caller did not name. That is what makes the output safe
#      to put in `environment.systemPackages` of a NixOS configuration.
#   3. `environment` of the result is the environment of the same members, so
#      one set of arguments gives both shapes.
{
  pkgs ? import <nixpkgs> { },
  nanopynixEnv,
  project ? ./projects/workspace,
  editableTree ? ./projects/workspace,
  # The members to render. Each one installs a console script of the same name
  # here, so one list names both the members and the programs.
  application ? [ "ws-cli" ],
  # The name of the environment that the application links into. Two projects
  # build two applications, so each one needs its own name.
  venvName ? "workspace-app",
  # Each program of the environment that this application must **not** carry.
  # **The caller states this, and the graph does not.** An application that
  # carried the whole environment would otherwise build and report itself as
  # good.
  absent ? [
    "python"
    "python3"
    "activate"
    "pygmentize"
    "markdown-it"
  ],
}:

let
  planner = import ./uv.nix {
    inherit
      pkgs
      nanopynixEnv
      project
      application
      venvName
      ;
  };
  app = builtins.outputOf planner.outPath "out";

  # The same project with no application, which is the shape that a development
  # shell takes. `uv.nix` gives it as an attribute, so these arguments are
  # written once.
  venv = builtins.outputOf planner.environment.outPath "out";

  programs = if builtins.isList application then application else [ application ];
in
derivation {
  name = "${venvName}-check";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  DDRN_EDITABLE_ROOT = "${editableTree}";

  args = [
    "-c"
    ''
      set -eu

      report() {
        echo "prefix  ${app}"

        # Each program runs. It is a symbolic link into the environment, and
        # the shebang of the script in that environment names the `bin/python`
        # of the environment, so the interpreter that it finds is the one that
        # holds every member.
        echo "console scripts"
      ${pkgs.lib.concatMapStrings (name: ''
        printf '  %s\n' "$("${app}/bin/${name}")" || return 1
      '') programs}

        # **What the application does not carry.** Each name below is in the
        # environment, and none of them belongs to a member that the caller
        # named.
        echo "absent"
        for name in ${pkgs.lib.escapeShellArgs absent}; do
          if [ -e "${app}/bin/$name" ]; then
            echo "  this application carries bin/$name, and it names no member that ships it" >&2
            return 1
          fi
          printf '  bin/%s\n' "$name"
        done

        # The whole output, so that a reader sees how small it is.
        echo "holds"
        ${pkgs.findutils}/bin/find "${app}" -mindepth 1 -printf '  %P\n' | ${pkgs.coreutils}/bin/sort

        # **The environment of the same arguments**, which every link above
        # names. This proves the `environment` attribute reaches the store path
        # that the application was built over, and it is also the shape for a
        # development shell.
        echo "environment"
        printf '  %s\n' "${venv}"

        # **The attribute has to name the store path that the links name**, or
        # a caller who installs both gets two copies of one environment.
        # `uv.nix` gives the environment of an application the `-env` suffix
        # for this reason, and a node name is the name of a store path.
      ${pkgs.lib.concatMapStrings (name: ''
        target="$(${pkgs.coreutils}/bin/readlink "${app}/bin/${name}")"
        case "$target" in
        "${venv}"/*) ;;
        *)
          echo "  bin/${name} points at $target, and not into ${venv}" >&2
          return 1
          ;;
        esac
      '') programs}

        for name in ${pkgs.lib.escapeShellArgs absent}; do
          if [ ! -e "${venv}/bin/$name" ]; then
            echo "  the environment holds no bin/$name, so the absent list is wrong" >&2
            return 1
          fi
        done
        printf '  holds every program that the application left out\n'
      }

      if ! report > "$out" 2>&1; then
        while IFS= read -r line; do printf '%s\n' "$line" >&2; done < "$out"
        exit 1
      fi
    ''
  ];
}
