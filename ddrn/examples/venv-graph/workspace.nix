# Every directory of a tree that holds a `pyproject.toml`.
#
# **A lock file records the members that it resolved, and a workspace can grow
# a member past it.** `ddrn_venv._freshness` compares the declaration of each
# member with the `[package.metadata]` that `uv.lock` holds. A directory that
# the lock file never saw reaches no such comparison, because the lock file is
# the list of what to compare. Only a walk of the tree finds that one, and only
# the evaluator can walk a tree that is not in the store.
#
# **So this file walks, and it decides nothing.** It reads no glob, it opens no
# `pyproject.toml`, and it knows no rule of uv. `ddrn_venv._workspace` takes
# this listing and answers which directories are members, with `fnmatch` for
# the globs. That is the argument of the whole lab, applied once more: a
# packaging rule belongs in Python, and the Nix half fetches and lists.
{
  lib,
  # The workspace root, as a path. This is the directory that holds `uv.lock`.
  root,
}:

let
  # **A name here is never a directory of a uv workspace, and each one is
  # expensive to walk.** uv descends into all of them, because the `glob` crate
  # takes `require_literal_leading_dot: false`. A member under one of these
  # names is not found here, and `ddrn_venv._workspace` records that boundary.
  #
  # `.git` alone is most of the cost: this repository holds 1902 directories,
  # and a walk of all of them costs 0.3 s of evaluation.
  skipped = [
    ".git"
    ".jj"
    ".hg"
    ".svn"
    ".direnv"
    ".venv"
    ".tox"
    ".eggs"
    ".mypy_cache"
    ".pytest_cache"
    ".ruff_cache"
    "__pycache__"
    "node_modules"
  ];

  # **A symbolic link is not followed.** `builtins.readDir` gives `symlink` for
  # one, and this walk descends into a `directory` only. uv follows a link, so
  # a member behind one is not found here either.
  walk =
    relative:
    let
      directory = if relative == "" then root else root + "/${relative}";
      entries = builtins.readDir directory;
      under = name: if relative == "" then name else "${relative}/${name}";
      children = lib.filter (name: entries.${name} == "directory" && !(lib.elem name skipped)) (
        lib.attrNames entries
      );
      here = lib.optional (entries ? "pyproject.toml") {
        name = relative;
        value = "${builtins.path {
          path = directory + "/pyproject.toml";
          name = "pyproject.toml";
        }}";
      };
    in
    here ++ lib.concatMap (name: walk (under name)) children;

in
# The `pyproject.toml` of each directory, keyed by the path of that directory
# relative to the root. The root itself is the empty key.
#
# **Each file goes into the store whole, and this reader interprets none of
# it.** The planner needs `[project].name`, `tool.uv.managed` and
# `[tool.uv.workspace]`, and a TOML reader in the Nix language would be a
# second implementation of rules that `ddrn_venv._workspace` already holds.
lib.listToAttrs (walk "")
