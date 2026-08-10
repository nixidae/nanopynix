# Run the environment that `uv.nix` built, inside the store that holds it.
#
# `./check.nix` does the same for the hand-written `lock.json`, and it gives
# the reason that this runs as a derivation and not from a shell: a store path
# is absolute, and a chroot store puts the store under a prefix.
#
# **The proof here is that the environment holds what `uv.lock` resolved, and
# nothing else.** `rich` is the one package that the project asked for, and
# `markdown-it-py`, `mdurl` and `pygments` reached the environment because the
# planner walked the edges of the lock file. `installer` is in the same lock
# file, in the `ddrn` dependency group, and it must **not** be here: the
# selection did not enable that group.
{
  pkgs ? import <nixpkgs> { },
  nanopynixEnv,
  project ? ./projects/pure-app,
}:

let
  planner = import ./uv.nix { inherit pkgs nanopynixEnv project; };
  venv = builtins.outputOf planner.outPath "out";
in
derivation {
  name = "uv-check";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  args = [
    "-c"
    ''
      set -eu

      # **The report is the output, so a failure would otherwise say nothing.**
      # Nix prints the log of a build that fails and deletes its output, and
      # this report goes to the output. So write it, and read it back to stderr
      # when the check refuses.
      # **Each command names its own failure.** `set -e` does not apply inside a
      # function that runs as the condition of an `if`, and neither does the
      # exit code of a command substitution that sits in the arguments of
      # `printf`. Both hid a failing check until this comment was written.
      report() {
        "${venv}/bin/python" ${./scripts/check-uv.py} || return 1

        greeting="$("${venv}/bin/python" ${project}/main.py)" || return 1
        echo "the project runs"
        printf '  %s\n' "$greeting"
      }

      if ! report > "$out" 2>&1; then
        while IFS= read -r line; do printf '%s\n' "$line" >&2; done < "$out"
        exit 1
      fi
    ''
  ];
}
