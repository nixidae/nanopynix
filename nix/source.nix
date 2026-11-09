/*
  The repository tree, for a derivation that needs the whole thing: an
  allow-list of the tracked kinds of file, under the directories that hold
  them.

  A deny-list of run directories missed each new one: `.pytest-agent` once
  made the `nanopynix` source 36 MiB, against 8.8 MiB of `src` and `tests`,
  and changed it on every local run. Under a directory named here, a cache
  holds no file of a kind named here (`.pyc`, JSON reports, SQLite).

  `.github` and `.assets` stay: the test runner reads the rendered workflows
  (`tests/meta/test_ci_step_policy.py`).

  Prefer a project source over this one. A derivation that reads the whole
  repository rebuilds when any file of any project changes, which is what issue
  #130 set out to stop.
*/
{ lib }:
let
  root = ../.;
  kinds = [
    "c"
    "go"
    "ini"
    "lock"
    "md"
    "mod"
    "nix"
    "patch"
    "png"
    "py"
    "pyi"
    "roff"
    "sh"
    "sum"
    "toml"
    "typed"
    "yml"
  ];
  tracked = lib.fileset.fileFilter (file: lib.any file.hasExt kinds || file.name == "LICENSE");
  # Hidden directories hold the caches: `.pytest_cache` writes a README.md.
  hiddenDirs = [
    ".assets"
    ".claude"
    ".github"
    # `pynix-lsp/tests/test_lsp`: the editor configuration the suite reads.
    ".helix"
  ];
  noCaches = lib.fileset.fromSource (
    lib.sources.cleanSourceWith {
      src = root;
      filter =
        path: type:
        let
          base = baseNameOf path;
        in
        type != "directory" || !lib.hasPrefix "." base || lib.elem base hiddenDirs;
    }
  );
  dirs = [
    ".assets"
    ".claude"
    ".github"
    "ci"
    "completion-spike"
    "docs"
    "libpynix"
    "nanopynix-testing"
    "nix"
    "pynix"
    "pynix-lsp"
    "pytest-agent"
    "scripts"
    "test-support"
    "tests"
    "tmp"
    "tools"
  ];
in
lib.fileset.toSource {
  inherit root;
  fileset = lib.fileset.unions (
    map (dir: lib.fileset.intersection noCaches (tracked (root + "/${dir}"))) dirs
    ++ [
      ../.envrc
      ../.gitignore
      ../LICENSE
      ../README.md
      ../REASONIX.md
      ../ROADMAP.md
      ../TODO.md
      ../default.nix
      ../flake.lock
      ../flake.nix
      ../mypy.ini
      ../pyproject.toml
      ../pytest.ini
      ../ruff-strict.toml
      ../ruff.toml
      ../shell.nix
      ../statix.toml
      ../treefmt.toml
    ]
  );
}
