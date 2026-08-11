# Run the environment of `projects/binary-app`, inside the store that holds it.
#
# `./check-uv.nix` does the same for `projects/pure-app`, and `./check.nix`
# gives the reason that this runs as a derivation and not from a shell.
{
  pkgs ? import <nixpkgs> { },
  nanopynixEnv,
  project ? ./projects/binary-app,
  # The distributions that this environment must hold, as PEP 503 names.
  expect ? [
    "pyzmq"
  ],
  venvName ? "binary-app",
  # Each package that nixpkgs supplies instead of the index. `run-uv.sh` builds
  # this file twice, once with none and once with `pyzmq`, and the two reports
  # differ in what the extension links against.
  fromNixpkgs ? { },
}:

let
  planner = import ./uv.nix {
    inherit
      pkgs
      nanopynixEnv
      project
      venvName
      fromNixpkgs
      ;
  };
  venv = builtins.outputOf planner.outPath "out";
in
derivation {
  name = "${venvName}-check";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";

  DDRN_EXPECT = builtins.concatStringsSep " " expect;

  # `patchelf` reads back what `auto-patchelf` wrote, and `ldd` resolves each
  # entry against it. `ldd` lives in the `bin` output of glibc, and not in the
  # bintools wrapper.
  PATH = "${pkgs.coreutils}/bin:${pkgs.patchelf}/bin:${pkgs.glibc.bin}/bin";

  args = [
    "-c"
    ''
            set -eu

            # The report is the output, so read it back to stderr when the check
            # refuses. `./check-uv.nix` gives the whole reason, and the `return 1`
            # lines below with it.
            report() {
              "${venv}/bin/python" ${./scripts/check-binary.py} || return 1

              # **The import is the proof.** A manylinux wheel names a loader and
              # libraries that NixOS does not have, so an extension that
              # `auto-patchelf` did not reach raises `ImportError` here.
              version="$("${venv}/bin/python" -c 'import zmq; print(zmq.zmq_version())')" || return 1
              echo "libzmq       $version"

              # Run the compiled code, and do not only import it. An in-process
              # round trip goes through the extension and through the bundled
              # `libzmq` that travels inside the same wheel.
              trip="$("${venv}/bin/python" -c '
      import zmq
      context = zmq.Context()
      here, there = context.socket(zmq.PAIR), context.socket(zmq.PAIR)
      here.bind("inproc://ddrn")
      there.connect("inproc://ddrn")
      here.send(b"the graph built this")
      print(there.recv().decode())
      here.close(); there.close(); context.term()
      ')" || return 1
              echo "round trip   $trip"

              # **What `auto-patchelf` wrote, read back.** Every `DT_NEEDED` entry of
              # the extension resolves through an `RPATH` that names a store path. A
              # wheel straight from the index names no store path at all, so a `not
              # found` line here means the node skipped the patch.
              so="$("${venv}/bin/python" -c 'import zmq.backend.cython._zmq as m; print(m.__file__)')" || return 1
              echo "extension    ''${so##*/}"

              # **Each command runs on its own line, and not inside an `if`
              # condition.** `set -e` does not apply to a command that an `if` tests,
              # so an absent tool there exits 127 and reads as "nothing was found".
              # That is how the first form of this check reported success while
              # every probe of it failed.
              rpath="$(patchelf --print-rpath "$so")" || return 1
              echo "rpath"
              printf '  %s\n' "$rpath"

              # A wheel straight from the index carries no `RPATH` that names the
              # store, so an unresolved entry here means the node installed the
              # wheel and skipped the patch.
              linked="$(ldd "$so")" || return 1
              case "$linked" in
              *"not found"*)
                echo "an entry of the extension resolves to nothing, so auto-patchelf did not run"
                printf '%s\n' "$linked"
                return 1
                ;;
              esac
              echo "every DT_NEEDED entry resolves"
            }

            if ! report > "$out" 2>&1; then
              while IFS= read -r line; do printf '%s\n' "$line" >&2; done < "$out"
              exit 1
            fi
    ''
  ];
}
