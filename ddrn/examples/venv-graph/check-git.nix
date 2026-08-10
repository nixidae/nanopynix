# Run the environment that `uv.nix` built out of a git checkout.
#
# `./check-uv.nix` does the same for `projects/pure-app`, and it gives the
# reason that this runs as a derivation and not from a shell: a store path is
# absolute, and a chroot store puts the store under a prefix.
#
# **This project is the one that reaches no index for its dependency.**
# `uv.lock` records a commit of `pypa/flit` and the subdirectory that holds
# `flit_core`, and it records no file at all. So the package exists in the
# environment only because `uv.nix` fetched the repository and the graph ran
# one PEP 517 `build_wheel` call over `flit_core/`.
#
# **The imports live here, and not in the script**, the way `./check-binary.nix`
# keeps `zmq` out of `scripts/check-binary.py`: only the graph installs this
# package, so no type checker of the repository can resolve it.
{
  pkgs ? import <nixpkgs> { },
  nanopynixEnv,
  project ? ./projects/git-app,
}:

let
  planner = import ./uv.nix { inherit pkgs nanopynixEnv project; };
  venv = builtins.outputOf planner.outPath "out";
in
derivation {
  name = "git-check";
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
        "${venv}/bin/python" ${./scripts/check-git.py} || return 1

        # **The install holds the code.** A git checkout is not a tree that the
        # user edits, so nothing redirects to it. An editable install of a
        # local project reports a path under `$DDRN_EDITABLE_ROOT` here, and
        # this one has to report a path inside the environment.
        module="$("${venv}/bin/python" -c 'import flit_core; print(flit_core.__file__)')" || return 1
        echo "loaded from"
        printf '  %s\n' "$module"
        case "$module" in
        "${venv}"/*) ;;
        *)
          echo "$module is outside ${venv}, so this install follows a tree" >&2
          return 1
          ;;
        esac

        # **The backend of the checkout is in the wheel.** `flit_core` builds
        # itself, with `backend-path = ["."]` of PEP 517, so this module is
        # both what the graph called to build the package and what the package
        # ships. A wheel that held the top module alone would import and would
        # answer none of these hooks.
        hooks="$("${venv}/bin/python" -c '
      from flit_core import buildapi

      print(" ".join(sorted(
          name
          for name in dir(buildapi)
          if name.startswith(("build_", "get_requires_", "prepare_"))
      )))
      ')" || return 1
        echo "buildapi"
        printf '  %s\n' "$hooks"
        case "$hooks" in
        *build_sdist*build_wheel*) ;;
        *)
          echo "flit_core.buildapi gives '$hooks', and PEP 517 names build_sdist and build_wheel" >&2
          return 1
          ;;
        esac
      }

      if ! report > "$out" 2>&1; then
        while IFS= read -r line; do printf '%s\n' "$line" >&2; done < "$out"
        exit 1
      fi
    ''
  ];
}
