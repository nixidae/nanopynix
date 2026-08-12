# Run the environment of one selection of `projects/workspace`, inside the
# store that holds it.
#
# `./check-uv.nix` does the same for `projects/pure-app`, and `./check.nix`
# gives the reason that this runs as a derivation and not from a shell.
{
  pkgs ? import <nixpkgs> { },
  nanopynixEnv,
  project ? ./projects/workspace,
  # The tree that each editable install reads. `uv.nix` redirects the install
  # at `$DDRN_EDITABLE_ROOT`, which this derivation sets, so a different tree
  # here reads back differently **from the same environment**. `run-uv.sh`
  # proves that by building this twice.
  editableTree ? ./projects/workspace,
  # Which members to install. `null` takes every member of the workspace, which
  # is what `uv sync --all-packages` takes.
  select ? null,
  # The distributions that this selection must resolve to, as PEP 503 names.
  # **The caller states this, and the graph does not.** A selection that
  # resolved to the wrong set would otherwise build and report itself as good.
  expect,
  # The name of the environment. Two selections of one workspace build two
  # different environments, so each one needs its own name.
  venvName,
  # Each member to install from its own source distribution. `uv.nix` gives the
  # whole reason, and the short one is this: a member that ships as an archive
  # holds its own code, so it stops following the tree that the others read.
  #
  # `null` is the default of `uv.nix`, which reads `[tool.ddrn] from-sdist` of
  # the project. A list here replaces that table.
  fromSdist ? null,
}:

let
  planner = import ./uv.nix {
    inherit
      pkgs
      nanopynixEnv
      project
      select
      venvName
      fromSdist
      ;
  };
  venv = builtins.outputOf planner.outPath "out";

  # One probe for each member that this selection installs. A member that the
  # selection leaves out is absent from the environment, so its probe must not
  # run at all.
  installs = name: builtins.elem name expect;
in
derivation {
  name = "${venvName}-check";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  DDRN_EDITABLE_ROOT = "${editableTree}";
  DDRN_EXPECT = builtins.concatStringsSep " " expect;

  args = [
    "-c"
    ''
      set -eu

      # The report is the output, so read it back to stderr when the check
      # refuses. `./check-uv.nix` gives the whole reason, and the `return 1`
      # lines below with it.
      report() {
        "${venv}/bin/python" ${./scripts/check-workspace.py} || return 1

        # **Every member reports the tree that it was read from.** The `.pth`
        # that hatchling wrote expands `$DDRN_EDITABLE_ROOT` when the
        # interpreter starts, so these lines report whichever tree this
        # derivation named.
        echo "members"
      ${pkgs.lib.optionalString (installs "ws-core") ''
        core="$("${venv}/bin/python" -c 'import ws_core; print(ws_core.banner())')" || return 1
        printf '  %s\n' "$core"
      ''}
      ${pkgs.lib.optionalString (installs "ws-cli") ''
        # This call reaches the other member, so it proves the edge between
        # two editable members of one workspace.
        cli="$("${venv}/bin/python" -c 'import ws_cli; print(ws_cli.greet())')" || return 1
        printf '  %s\n' "$cli"

        # The console script that `[project.scripts]` names, which exists only
        # because `pypa/installer` wrote it from `entry_points.txt`.
        script="$("${venv}/bin/ws-cli")" || return 1
        echo "console script"
        printf '  %s\n' "$script"
      ''}
      }

      if ! report > "$out" 2>&1; then
        while IFS= read -r line; do printf '%s\n' "$line" >&2; done < "$out"
        exit 1
      fi
    ''
  ];
}
