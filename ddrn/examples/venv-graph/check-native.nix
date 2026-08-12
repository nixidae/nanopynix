# Run the environment of `projects/native-app`, inside the store that holds it.
#
# **This is the `buildTools` argument against two packages of PyPI.**
# `tests/sealed.nix` covers the same argument over a fixture of this
# repository, and it needs no network. This file needs one, and it adds what a
# fixture cannot: the backend is the `setuptools` that each project declares,
# and the source is the archive that the index serves.
#
# `./check-uv.nix` gives the reason that this runs as a derivation and not from
# a shell: a store path is absolute, and a chroot store puts the store under a
# prefix.
{
  pkgs ? import <nixpkgs> { },
  nanopynixEnv,
  project ? ./projects/native-app,
  venvName ? "native-app",
  # **This file names no build tool, and the project names both.**
  # `projects/native-app/pyproject.toml` holds `[tool.ddrn.build-tools]`, and
  # `uv.nix` reads it. So this argument is empty, and a caller who overrides
  # one package still can: `uv.nix` merges the two, and the argument wins for
  # the package that it names.
  #
  # `psutil` holds C code and links nothing outside the interpreter. `cffi`
  # reads `ffi.h` and links `libffi`, so it needs the two outputs of that
  # package as well: a node runs no `stdenv`, so no setup hook follows `.dev`.
  #
  # `pkg-config` is in that table because `cffi` asks it first. The build
  # succeeds with no `pkg-config` at all, because `cffi` falls back to
  # `CPPFLAGS` and `LDFLAGS`, and `ddrn/PACKAGING-FACTS.md` records that
  # measurement.
  buildTools ? { },
}:

let
  planner = import ./uv.nix {
    inherit
      pkgs
      nanopynixEnv
      project
      venvName
      buildTools
      ;
  };
  venv = builtins.outputOf planner.outPath "out";
in
derivation {
  name = "${venvName}-check";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  args = [
    "-c"
    ''
      set -eu

      # The report is the output, so read it back to stderr when the check
      # refuses. `./check-uv.nix` gives the whole reason, and the `return 1`
      # line below with it.
      report() {
        "${venv}/bin/python" ${./scripts/check-native.py} || return 1
      }

      if ! report > "$out" 2>&1; then
        while IFS= read -r line; do printf '%s\n' "$line" >&2; done < "$out"
        exit 1
      fi
    ''
  ];
}
