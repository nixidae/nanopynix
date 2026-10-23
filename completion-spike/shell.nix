# The development environment for this subproject.
#
# Run the suite with it, from anywhere in the repository:
#
#     nix develop --file completion-spike/shell.nix --command pytest completion-spike
#
# **The three shells are part of the environment, and so is the terminfo
# database.** The tests drive a real fish, bash and zsh on a pty, so a shell
# that is missing is a test that cannot run, and `TERM=xterm` with no terminfo
# is a fish that draws no candidate list at all.
#
# `cyclopts`, `pexpect` and `pyte` are not in the repository's own dev shell
# venv --
# this subproject is a nixpkgs `buildPythonApplication` rather than one of the
# pyproject.nix builders projects, so its dependencies are resolved here and
# in nix/completion-spike.nix, and nowhere else.
let
  sources = import ../nix/sources.nix;
  repo = import ../. { inherit sources; };
  inherit (repo) pkgs;
  inherit (import sources.nixvenv { inherit pkgs; }) nixvenv;

  # `src` and `test-support/src` are editable, and there is no install step:
  # an edit is live, which is what a spike needs. The Nix build tests the
  # installed program instead, and tests/conftest.py takes whichever of the
  # two it finds.
  #
  # `test-support` is here as well, because the pty driver lives there now.
  # Issue #213 moved it out of `completion_spike._pty`, so that
  # `pynix/completions/tests/` could drive the same three shells.
  python = nixvenv {
    python = pkgs.python3;
    packages = ps: [
      ps.cyclopts
      ps.pexpect
      ps.pyte
      ps.pytest
    ];
    executables = [
      "python"
      "pytest"
    ];
    editable = [
      ./src
      ../test-support/src
    ];
  };
in
pkgs.mkShellNoCC {
  name = "completion-spike-shell";

  packages = [
    python
    pkgs.fish
    pkgs.bashInteractive
    pkgs.zsh
    pkgs.ncurses
  ];

  # A derivation attribute rather than a `shellHook` line, so that it applies
  # to `nix develop --command` and not only to an interactive shell.
  TERMINFO_DIRS = "${pkgs.ncurses}/share/terminfo";
}
