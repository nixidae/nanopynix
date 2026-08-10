/*
  The repository tree, filtered, for a derivation that needs the whole thing.

  **A bare `../.` copies the working directory and not the repository.** Under
  a flake evaluation the source is git-filtered and the difference does not
  show. Under `--file .`, which is how CI now builds every step, the copy took
  everything: 230 MiB against 5.7 MiB of tracked content. `.pytest-agent` was
  92 MiB of that, and it changes on every local pytest run, so each build
  minted a new 230 MiB store path. That is a full disk in a day.

  `./source-filter.nix` holds the rule, because `./compat.nix` needs the same
  one and has no `lib` when it runs. `lib.cleanSource` stays on top of it for
  the editor droppings that the bare rule does not name: a backup file, an
  object file, and a `.so`. No tracked file matches those.

  Removing `.git` also corrects a local failure: libgit2 refuses a root-owned
  repository, so `tests/pynix/test_real_store_scenario.py` failed whenever a
  colocated `.git` travelled into the store copy.
*/
{ lib }:
let
  rule = import ./source-filter.nix;
in
lib.cleanSourceWith {
  name = "nanopynix-source";
  src = lib.cleanSource ../.;
  filter = rule.filter;
}
