/*
  Which entries of the working directory no evaluation and no build needs.

  **This module takes no `lib`, and that is the whole reason it exists.**
  `./source.nix` filters the tree for a derivation that needs it, and it has
  `lib` by then. `./compat.nix` has to filter the *flake source* itself, which
  it resolves before any nixpkgs is available, so the rule has to be one that
  `builtins` alone can state.

  A denylist, and not a list of the directories to keep: `.github` must
  survive, because the test runner reads the rendered workflows
  (`tests/meta/test_ci_step_policy.py`), and so must `.assets`. Those are the
  only two tracked directories whose name starts with a period, so a rule over
  the name alone cannot separate them from the caches.

  **This is not `_SKIP_DIRS` from `tests/support/suppressions.py`.** That set
  answers "which directory holds no first-party source", and it lists `tmp`.
  This one answers "which directory does no derivation need", and `tmp` is
  tracked (`tmp/fs_closure.py`). Keep the two lists apart.
*/
let
  hasPrefix = prefix: text: builtins.substring 0 (builtins.stringLength prefix) text == prefix;
in
rec {
  #: Every directory to leave out, by base name. `.git` and `.jj` are here as
  #: well as in `lib.cleanSource`, because this rule has to stand on its own
  #: where no `lib` exists.
  skipDirs = [
    ".bench-dumps"
    ".direnv"
    ".git"
    ".hypothesis"
    ".jj"
    ".pytest-agent"
    ".pytest_cache"
    ".ruff_cache"
    ".terraform"
    ".test-runs"
    "__pycache__"
    "node_modules"
  ];

  filter =
    path: type:
    let
      base = baseNameOf path;
    in
    !(
      (type == "directory" && builtins.elem base skipDirs)
      # `.coverage` is a file, and a local run writes a new one every time.
      || base == ".coverage"
      || hasPrefix ".coverage." base
      # A build writes `result`, and `nix build --out-link` writes
      # `result-<name>`. Both are symlinks into the store.
      || (type == "symlink" && (base == "result" || hasPrefix "result-" base))
    );
}
