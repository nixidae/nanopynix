# A PEP 751 lock file, read into the same menu that `./uv.nix` builds.
#
# **This file is a spike, and not a second supported reader.** It answers one
# question: how much of the Nix half survives a move from `uv.lock` to the
# standard format. `tests/test_pylock.py` compares what this file gives with
# what `./uv.nix` gives over the same project, and `ddrn/PACKAGING-FACTS.md`
# records the answer.
#
# PEP 751 is the lock format that more than one tool writes. uv exports it:
#
#     uv export --format pylock.toml --frozen --all-packages --all-groups \
#       --python <interpreter> -o pylock.toml
#
# **`parse-toml-timestamps` is necessary.** PEP 751 writes `upload-time` as a
# TOML datetime, and `builtins.fromTOML` refuses one without that feature.
# `ddrn/PACKAGING-FACTS.md` gives the measurement and the line of Nix.
#
# **What this file does not do is the point of it.** `./uv.nix` reads the
# source of each package and decides which of five shapes it has. PEP 751
# gives each shape a key of its own, so this reader asks which key is present
# and reads it. There is no url to parse, and no registry to join a path
# against.
{
  pkgs ? import <nixpkgs> { },
  # The directory that holds `pylock.toml`. Every relative path of the lock
  # file resolves against it, which is what PEP 751 says.
  project,
  # The name of the lock file. PEP 751 permits `pylock.toml` and
  # `pylock.<name>.toml`, and uv refuses to write any other name.
  lockName ? "pylock.toml",
}:

let
  inherit (pkgs) lib;

  lockFile = project + "/${lockName}";
  document = builtins.fromTOML (builtins.readFile lockFile);

  # **PEP 751 pins the version of the format, and this reader accepts one.**
  # `packaging.pylock` raises `PylockUnsupportedVersionError` for the same
  # reason, so the two halves agree on what they read.
  lockVersion = document.lock-version or null;

  packages = document.packages or [ ];

  # **A file of this machine, or a file of the network.** PEP 751 writes one
  # of `url` and `path` on every artefact, and it says that a `path` resolves
  # against the directory of the lock file. uv writes an absolute `file://`
  # url for a registry that is a directory, which is the third case.
  fileScheme = "file://";
  isFileUrl = url: lib.hasPrefix fileScheme url;
  pathOfUrl = url: lib.removePrefix fileScheme url;

  # The name that an artefact carries. PEP 751 gives `name` on a wheel and on
  # a source distribution, and it is the file name that a reader needs.
  # `installer` reads the name of a wheel, and a store path carries a hash in
  # front of that name.
  fileName =
    entry:
    if entry ? name then
      entry.name
    else if entry ? path then
      baseNameOf entry.path
    else
      lib.last (lib.splitString "/" entry.url);

  # **uv writes `sha256:<hex>` and PEP 751 writes a table.** So this is the
  # one translation that the format costs, and `./uv.nix` needs none.
  hashOf =
    package: entry:
    let
      hashes = entry.hashes or { };
      names = builtins.attrNames hashes;
    in
    if hashes ? sha256 then
      "sha256:${hashes.sha256}"
    else if names == [ ] then
      null
    else
      "${builtins.head names}:${hashes.${builtins.head names}}";

  # Where a file of this machine lives. `builtins.path` names the one file, so
  # a second file of the same directory is no input of the planner.
  localFile =
    package: entry:
    let
      relative = if entry ? path then entry.path else pathOfUrl entry.url;
    in
    builtins.path {
      path = if lib.hasPrefix "/" relative then /. + relative else project + "/${relative}";
      name = fileName entry;
    };

  isLocal = entry: entry ? path || isFileUrl (entry.url or "");

  # One entry of the menu, in the shape that `./default.nix` takes.
  artifactOf =
    package: kind: entry:
    {
      inherit kind;
      package = package.name;
      version = package.version or (throw "pylock.nix: '${package.name}' has an artefact and no version");
      filename = fileName entry;
    }
    // (
      if isLocal entry then
        { path = "${localFile package entry}"; }
      else
        {
          drv = pkgs.fetchurl {
            inherit (entry) url;
            name = fileName entry;
            hash = hashOf package entry;
          };
        }
    )
    // lib.optionalAttrs (entry ? subdirectory) { inherit (entry) subdirectory; };

  # **An archive is a source distribution that no index served.** PEP 751 gives
  # it a key of its own, because it carries a url or a path and no index. The
  # planner builds it exactly as it builds an `sdist`.
  artifactsOf =
    package:
    map (artifactOf package "wheel") (package.wheels or [ ])
    ++ map (artifactOf package "sdist") (lib.optional (package ? sdist) package.sdist)
    ++ map (artifactOf package "sdist") (lib.optional (package ? archive) package.archive);

  # **A git source arrives as three fields, and not as one url to parse.**
  # `./uv-git.nix` exists because uv writes
  # `git+https://host/repo?subdirectory=x#<rev>`, and PEP 751 writes the url,
  # the commit and the subdirectory apart. So this reader parses nothing.
  #
  # The fetch still happens during evaluation, and it still reaches the
  # network: a commit names no hash of the tree that it gives, whichever format
  # records it.
  gitTree =
    package:
    builtins.fetchGit {
      inherit (package.vcs) url;
      rev = package.vcs.commit-id;
      # PEP 751 records a commit, and not a branch. Without this the fetch
      # looks in the default branch alone, and a commit of a tag is not in it.
      allRefs = true;
    };

  isTree = package: package ? directory || package ? vcs;

  # One entry for each package that the planner builds from a directory.
  editables = lib.listToAttrs (
    map (package: {
      inherit (package) name;
      value =
        if package ? vcs then
          {
            source = "${gitTree package}";
            # A checkout is not a tree that the user edits, so nothing
            # redirects to it and the install holds the code.
            root = "";
            mode = "wheel";
            subdirectory = package.vcs.subdirectory or "";
          }
        else
          {
            source = "${builtins.path {
              path = project + "/${package.directory.path}";
              name = "${package.name}-source";
            }}";
            root = "$DDRN_EDITABLE_ROOT/${package.directory.path}";
            # **PEP 751 records the decision that uv records.** `editable` is
            # a boolean of the directory, so a reader follows it rather than
            # invent a rule of its own.
            mode = if package.directory.editable or false then "editable" else "wheel";
            subdirectory = package.directory.subdirectory or "";
          };
    }) (lib.filter isTree packages)
  );
in

lib.throwIfNot (lockVersion == "1.0")
  "pylock.nix: ${lockFile} says lock-version ${builtins.toJSON lockVersion}, and this reader accepts \"1.0\""
  {
    inherit document editables;
    artifacts = lib.concatMap artifactsOf packages;
    # Every package of the file, by name. PEP 751 is already resolved, so this
    # is the whole selection and no walk gives a smaller one.
    names = map (package: package.name) packages;
    inherit (document) requires-python;
  }
