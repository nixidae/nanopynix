let
  lock = builtins.fromJSON (builtins.readFile ../flake.lock);

  # **The override evaluates from the file system, and this must not change
  # that.** `resolveOverride` of flake-compatish answers a path with
  # `toString`, so `self` below is the working directory and no copy happens.
  # That is the whole purpose of the override, and a filtered `source` would
  # defeat it if it applied in that case: `sourceString` is forced to find
  # `flake.lock`, so a store path there is copied whether or not anything
  # reads it.
  #
  # So the filter applies only when a copy is going to happen anyway.
  # `FLAKE_COMPATISH_DISABLE_OVERRIDES` turns the override off, and the root
  # node of the lockfile then reaches `fetchTree` with `type = "path"`, which
  # copies the whole tree with no filter of its own. Every CI workflow and
  # `ddrn/examples/venv-graph/tests` set that variable, so this is the copy
  # that a gate makes.
  #
  # That copy took the working directory and not the repository.
  # `.pytest-agent` alone reached 45023 files against 1488 tracked ones, and
  # each evaluation copied all of them. On btrfs that exhausts the metadata
  # long before `df` reports a full disk. `./source-filter.nix` states the
  # rule, and it takes no `lib`, because none exists at this point.
  #
  # **The context has to go, and the value is a string either way.**
  # `builtins.path` gives a string that refers to a store path, and
  # flake-compatish hands a string `source` to `builtins.parseFlakeRef`, which
  # refuses such a string. The path exists by the time this line runs, because
  # `builtins.path` realises it, so nothing needs the context to keep it.
  #
  # This reads the variable that flake-compatish reads, with the same rule
  # (`default.nix:153`). `getEnv` gives an empty string in a pure evaluation,
  # and `flake.nix` is what a pure evaluation reads, not this file.
  disableOverrides = builtins.getEnv "FLAKE_COMPATISH_DISABLE_OVERRIDES";
  overridesDisabled = disableOverrides != "" && disableOverrides != "0";

  source =
    if overridesDisabled then
      builtins.unsafeDiscardStringContext (builtins.path {
        name = "nanopynix-source";
        path = ../.;
        filter = (import ./source-filter.nix).filter;
      })
    else
      ../.;

  # Follow the root node's input mapping, and do not name the node directly.
  #
  # **Two nodes of this lockfile are called flake-compatish.** `easykubenix`
  # depends on it as well, so Nix gives one of them the plain name and the
  # other `flake-compatish_2`, and which one gets which is not ours to choose.
  # `nodes.flake-compatish` was easykubenix's copy, so every `--file .`
  # evaluation used the revision that *that* flake pins rather than the one
  # `flake.nix` declares. The two differed by four months, and the newer one
  # carries `FLAKE_COMPATISH_DISABLE_OVERRIDES`, so the flag read as absent
  # here while the lockfile said it was present.
  flake-compatish = import (
    fetchTree lock.nodes.${lock.nodes.${lock.root}.inputs.flake-compatish}.locked
  );
in
flake-compatish {
  inherit source;
  overrides = {
    # **A path here copies nothing.** `resolveOverride` of flake-compatish
    # answers a path with `toString`, so `self` is the working directory
    # itself and no store copy happens. That is what makes a local evaluation
    # fast, and it is why the filter above matters only when
    # `FLAKE_COMPATISH_DISABLE_OVERRIDES` removes this line.
    self = ../.;
    # nixpkgs = <nixpkgs>;
  };
  nixpkgsArgs = system: {
    inherit system;
    config.allowUnfree = true;
  };
}
