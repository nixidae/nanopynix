{
  mkShell,
  pyright,
  ruff,
  nixfmt,
  clang-tools,
  taplo,
  treefmt,
  actionlint,
  shellcheck,
  editablePythonSet,
  cachix,
  statix,
  tofuCoreSchemaTool,
  storeExecTool,
  nix-cli,
  gdb,
  graphviz,
  graph-easy,
  uv,
}:
let
  # Reuses dev-env.nix's editable venv rather than assembling a second one.
  # The docs toolchain goes *into* that venv, via pynix's `docs` extra, rather
  # than onto PATH beside it: `sphinx-build` has to import what it documents,
  # and a second Python environment would have its own `sys.path` without it.
  pythonEnv = import ./dev-env.nix {
    inherit editablePythonSet;
    extraSpec = {
      pynix = [
        "test"
        "ekn"
        "docs"
      ];
    };
  };
in
mkShell {
  shellHook = ''
    unset PYTHONPATH
  '';

  packages = [
    # **The `nix` of this band, and not the `nix` of the machine.** This shell
    # is defined inside each Nix-version scope, so `nix-cli` is the command
    # that the same source builds as the library that nanopynix links.
    #
    # A test that compares pynix with `nix ... --json` needs this. Without it,
    # `PATH` gives whatever the machine runs: measured on 2026-08-12, the shell
    # of the master band linked 2.36.0pre-ddnix and found 2.34.8, and the two
    # printed two flake fingerprints over one flake.
    # `tests/support/nix_oracle.py` refuses such a comparison, and
    # `ddrn/NIX-FACTS.md` records the two strings.
    #
    # **The daemon of the machine stays the daemon of the machine.** A command
    # here that speaks to `/nix/var/nix/daemon-socket` is a newer client than
    # that daemon. Give a store of your own, such as
    # `--store 'local://?root=/tmp/somewhere'`, for work that the protocol of
    # the machine cannot carry.
    nix-cli
    pythonEnv
    pyright
    ruff
    nixfmt
    clang-tools
    taplo
    treefmt
    actionlint
    # `scripts/` holds shell, and `writeShellApplication` only checks the
    # scripts that it generates. Without this the dev shell cannot run the
    # `check-shell` gate that nix/checks.nix now builds.
    shellcheck
    cachix
    statix
    gdb
    # pynix._lsp._tofu_core_schema invokes this at LSP-server runtime (see
    # its module docstring) rather than baking a static snapshot -- on PATH
    # here so the editable dev shell resolves it exactly like the real,
    # non-editable build's makeWrapperArgs does (pynix/package.nix).
    tofuCoreSchemaTool
    storeExecTool
    # `ddrn/architecture.dot` is the source of the diagram in
    # `ddrn/ARCHITECTURE.md`. `dot` checks that the file parses and draws it
    # in a terminal with `-Tvt`, and `graph-easy` writes the ASCII art that
    # the document holds. `scripts/render-architecture.sh` runs both.
    #
    # **`graph-easy` is here because `dot -Tascii` is not a diagram.** That
    # device came in graphviz 13.0.0, and nixpkgs builds graphviz with
    # autotools, which leaves the plugin out. A CMake build gives the device,
    # and the device then draws the picture with AA-lib: it renders the pixels
    # of the image as characters, and the graph of this lab came out as 366
    # lines of noise. `graph-easy` draws boxes and arrows instead.
    graphviz
    graph-easy
    # Each project under `ddrn/examples/venv-graph/projects` comes from `uv`,
    # and `projects/README.md` gives the command that wrote each one. The
    # lock file of this repository pins the version, so every reader of that
    # file runs the same `uv` and gets the same output.
    uv
  ];
}
