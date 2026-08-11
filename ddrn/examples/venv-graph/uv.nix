# A real `uv` project, built as a graph of derivations.
#
# **This file makes no packaging decision.** It reads `uv.lock` with
# `builtins.fromTOML`, makes one `pkgs.fetchurl` for every artefact of every
# package, and gives that list to `./default.nix` through the `artifacts` seam.
# An artefact of a `path` source has no address, so `builtins.path` copies that
# file into the store and the row names the copy.
# It chooses no wheel, it evaluates no marker, and it walks no edge.
# `ddrn_venv._uvlock` does all of that, inside the planner, with `packaging`.
#
# `uv2nix` makes the opposite choice. It re-implements PEP 440 and PEP 508 in
# the Nix language (`pyproject.nix/lib/pep440.nix`, `lib/pep508.nix`) and
# resolves the lock file during evaluation. `ddrn/PACKAGING-FACTS.md` gives the
# measurement that decided this lab against that: a menu of every artefact of a
# 1911-package lock file costs 632 KB and 1.2 s, so a filter buys nothing.
#
# **One source kind breaks that rule, and `uv.lock` is the reason.** A git
# source records a commit and no hash of the tree, so there is no fixed-output
# derivation to instantiate and `builtins.fetchGit` runs during evaluation.
# Every other input stays lazy. `gitTree` below gives the whole cost.
#
# The projects under `./projects` come from `uv` itself. `projects/README.md`
# gives the command that wrote each one.
#
# Usage: `ddrn/examples/venv-graph/run-uv.sh`.
{
  pkgs ? import <nixpkgs> { },
  # A Python environment that has nanopynix, built against the patched Nix.
  # `./default.nix` says why a derivation here is pure and a string is not.
  nanopynixEnv,
  # The uv project to build. It holds `pyproject.toml` and `uv.lock`.
  project ? ./projects/pure-app,
  # Which packages of the lock file to install, and which extras and which
  # dependency groups of each one to enable.
  #
  # **`uv.lock` records none of these three decisions.** `uv sync` takes them
  # on its command line, and this argument is the same decision. `null` means
  # every workspace member, with no extra and no group.
  select ? null,
  # The wheel installer that every graph bootstraps with. It comes out of the
  # same lock file as everything else, so one file pins the whole build.
  # `projects/README.md` gives the `uv add` command that puts it there.
  installer ? "installer",
  # What a PEP 660 editable install of a local project redirects to. A literal
  # path names one tree forever. A reference to a variable, which is the
  # default, keeps the environment the same bytes for every tree.
  # `./default.nix` gives the whole reason at its `editables` argument.
  editableRoot ? "$DDRN_EDITABLE_ROOT",
  # Each member to install from its own source distribution, by package name.
  # Every other `editable` source installs as a PEP 660 editable.
  #
  # **These are the two forms that uv itself writes.** `uv sync` installs a
  # member as an editable, so the environment follows the tree. `uv build`
  # writes the archive that an index serves, and a user who installs from that
  # archive gets whatever the backend put in it. `projects/README.md` gives the
  # measurement of what that is, and it is not the whole tree.
  #
  # A `directory` source and a git source take neither form. uv installs both
  # of those as a wheel, so this reader does too.
  fromSdist ? [ ],
  venvName ? null,
  # The interpreter that the environment runs. `./default.nix` gives the whole
  # reason at its `python` argument, and the short one is this: the interpreter
  # is the whole runtime closure, and a smaller one costs a build of CPython
  # and adds a second interpreter to every store that holds `pkgs.python3`.
  python ? pkgs.python3,
  markers ? { },
  # Each package that nixpkgs supplies instead of the index, as
  # `{ pyzmq = pkgs.python3Packages.pyzmq; }`. `./default.nix` gives the whole
  # reason at its `fromNixpkgs` argument, and the short one is this: a manylinux
  # wheel carries a copy of every library that it links against, and a package
  # of nixpkgs links against the store.
  #
  # **The lock file still resolves the package.** The override decides where
  # the code comes from, and not whether the package is in the environment, so
  # the edges of `uv.lock` still name it and its dependencies still resolve.
  fromNixpkgs ? { },
}:

let
  inherit (pkgs) lib;

  # **Each of the two is a path, and not a string that holds a path.** A string
  # interpolation of `project` copies the whole directory into the store, so
  # `main.py` would then be an input of the planner and an edit of it would
  # change the plan. Path arithmetic names the one file.
  pyproject = builtins.fromTOML (builtins.readFile (project + "/pyproject.toml"));
  lockFile = project + "/uv.lock";
  document = builtins.fromTOML (builtins.readFile lockFile);

  # The name of the project, which names the environment that comes out of the
  # resolution.
  projectName = pyproject.project.name;

  # **The roots of a lock file are its workspace members, and `[manifest]`
  # names them.** uv writes that table for a workspace only, so a lock file of
  # one project holds none, and that one project is the one member. The names
  # here are package names, and not the directories that
  # `[tool.uv.workspace] members` holds.
  members = document.manifest.members or [ projectName ];

  # **`tool.uv` is a setting of the project, and uv copies none of it into the
  # lock file.** So this reads the setting where uv keeps it, rather than
  # invent an argument for the same decision. `no-binary-package` names each
  # package to build from its source distribution even when a wheel fits.
  noBinary = pyproject.tool.uv."no-binary-package" or [ ];

  # The three source kinds that mean "a directory of this workspace, and not an
  # artefact of an index". `ddrn_venv._uvlock.LOCAL_SOURCES` lists the same
  # three, and the value of the key is the directory, relative to this project.
  # A git source is a directory too, and `isGit` below reads it, but uv gives
  # it a URL rather than a path.
  localKinds = [
    "editable"
    "directory"
    "virtual"
  ];
  localKind = package: lib.findFirst (kind: package.source ? ${kind}) null localKinds;

  # **A git source is a tree too, and uv writes no artefact for it.** The
  # package carries no `wheels` table and no `sdist` table, so nothing of it
  # reaches the menu. The checkout is the whole input, and one PEP 517
  # `build_wheel` call over it gives the install.
  isGit = package: package.source ? git;

  # A package that this reader builds from a directory. A `virtual` package
  # installs nothing, so it gets no tree, and `Resolution.members` never asks
  # for one.
  isTree = package: (localKind package != null || isGit package) && !(package.source ? virtual);

  # Where a local package lives, relative to the project that the lock file
  # belongs to. A single project says `.` here, and a member of a workspace
  # says the directory that holds it.
  localPath = package: package.source.${localKind package};

  # **The tree of a local package reaches the store whole, and that is the
  # point.** The backend of the project reads it, so `builtins.path` copies the
  # directory rather than one file of it.
  localTree =
    package:
    let
      relative = localPath package;
    in
    builtins.path {
      path = if relative == "." then project else project + "/${relative}";
      name = "${package.name}-source";
    };

  # **The redirect names the directory of the member, and not the directory of
  # the workspace.** `editableRoot` names one tree, and every member of a
  # workspace lives under it, so the relative directory of the lock file has to
  # travel into the redirect as well. `scripts/build-local.py` rewrites the
  # build directory to this value, and the build directory is the tree of the
  # member. A single project says `.`, so its redirect is `editableRoot`
  # itself, and that is the case that `projects/packaged-app` proves.
  localRoot =
    package:
    let
      relative = localPath package;
    in
    if relative == "." then editableRoot else "${editableRoot}/${relative}";

  # The URL, the commit and the subdirectory of a git source. `./uv-git.nix`
  # holds the parse, and `tests/test_uv_git.py` runs that file over the
  # reference of each fixture lock file.
  gitParts = package: import ./uv-git.nix package.name package.source.git;

  # **The checkout happens during evaluation, and it reaches the network.**
  # `uv.lock` records the commit of a git source and no hash of the tree that
  # the commit gives, so there is no fixed-output derivation to instantiate and
  # nothing to defer. Every other input of this lab is lazy: `pkgs.fetchurl`
  # instantiates for nothing and downloads only what the planner chooses. A
  # lock file that holds a git source pays a fetch for it whether or not the
  # resolution installs the package. `ddrn/PACKAGING-FACTS.md` records the
  # measurement, and it records what uv would have to write to remove the cost.
  gitTree =
    package:
    let
      parts = gitParts package;
    in
    builtins.fetchGit {
      inherit (parts) url rev;
      # uv names a commit, and not a branch. Without this, the fetch looks in
      # the default branch alone, and a commit of a tag is not in it.
      allRefs = true;
    };

  # One entry for each package that this reader builds from a tree.
  uvEditables = lib.listToAttrs (
    map (package: {
      name = package.name;
      value =
        if isGit package then
          {
            source = "${gitTree package}";
            # **A git checkout is not a tree that the user edits**, so nothing
            # redirects to it and the install holds the code.
            root = "";
            mode = "wheel";
            inherit (gitParts package) subdirectory;
          }
        else
          {
            source = "${localTree package}";
            root = localRoot package;
            # **uv installs an `editable` source as an editable, and a
            # `directory` source as a wheel.** The two source kinds are the two
            # decisions that uv already made, so this reader follows them
            # rather than install every directory the same way.
            mode =
              if lib.elem package.name fromSdist then
                "sdist"
              else if package.source ? directory then
                "wheel"
              else
                "editable";
            subdirectory = "";
          };
    }) (lib.filter isTree document.package)
  );

  # **A name that no local package answers is a mistake, and not a no-op.**
  # `fromSdist` names a member of this workspace, so a name that the lock file
  # does not hold as a local package would change nothing and say nothing.
  localNames = map (package: package.name) (
    lib.filter (package: localKind package != null) document.package
  );

  # The `pyproject.toml` of one member, in the store.
  #
  # **This file is what tells a stale lock file from a current one.** `uv.lock`
  # records the declaration of each member in `[package.metadata]`, so a
  # reader can compare the two and refuse a lock file that the project has
  # grown past. uv does the same in `Lock::satisfies`, and
  # `ddrn_venv._freshness` gives the rules that this reader takes from it.
  #
  # **The file goes in whole, and this reader interprets none of it here.** A
  # comparison of two dependency lists needs a PEP 508 parser, and the whole
  # argument of this lab is that such a parser belongs in `packaging` and not
  # in the Nix language. So the planner reads the TOML.
  declarationOf =
    package:
    let
      relative = localPath package;
    in
    builtins.path {
      path = (if relative == "." then project else project + "/${relative}") + "/pyproject.toml";
      name = "${package.name}-pyproject.toml";
    };

  # One entry for each member that lives in a directory of this workspace. A
  # git source is a tree as well, and it is not a member: uv resolved it, and
  # this project declares no dependency of it.
  declarations = lib.listToAttrs (
    map (package: {
      inherit (package) name;
      value = "${declarationOf package}";
    }) (lib.filter (package: localKind package != null) document.package)
  );

  # **The address of an artefact is on the row, or on the source.** A package
  # of an index gives a `url` on each row of `wheels` and on `sdist`. A URL
  # source gives the address once, on the source, and leaves the `sdist` table
  # with the hash alone:
  #
  #   source = { url = "https://.../hatchling-v1.27.0.tar.gz", subdirectory = "backend" }
  #   sdist = { hash = "sha256:c5d576a0..." }
  artifactUrl =
    package: file:
    if file ? url then
      file.url
    else if package.source ? url then
      package.source.url
    else
      throw (
        "uv.nix: '${package.name}' names an artefact with no url and no path, and its source "
        + "gives neither. Every artefact of uv.lock names one of the two."
      );

  # **A file of this machine reaches the menu without a fetch.** uv writes the
  # location of such a file in one of two places, and `WheelWireSource` of uv
  # gives both:
  #
  #   source = { path = "vendor/vendored-1.0.0-py3-none-any.whl" }   the file is the package
  #   source = { registry = "index" }                                a local registry, and
  #   wheels = [ { path = "stocked-1.0.0-py3-none-any.whl" } ]       the row is under it
  #
  # The first is `Source::Path`, and the row of the artefact then carries the
  # hash and the file name alone. The second is a registry that is a directory,
  # which `--find-links` and `format = "flat"` both give.
  #
  # **The path of a row has three components, and not two.** uv joins the root
  # of the workspace, the path of the registry and the path of the row, in
  # `to_registry_wheel` and in `to_source_dist`
  # (`crates/uv-resolver/src/lock/mod.rs`). A row that names its file relative
  # to the project would name the wrong file, or no file at all.
  #
  # Each of the last two parts may be absolute, because uv makes a row relative
  # only when the user gave the index as a relative path
  # (`try_relative_to_if`). `joinPath` keeps an absolute part as it is.
  isAbsolute = text: lib.hasPrefix "/" text;
  joinPath = base: part: if isAbsolute part then part else "${base}/${part}";

  # Whether the value of `registry` names a place on the network. uv asks the
  # same question with `split_scheme`, and it reads a value with no scheme as a
  # directory (`RegistrySourceWire`). A value with an unknown scheme is a
  # directory to uv and a URL here, and no lock file writes one.
  hasScheme = text: builtins.match "[a-zA-Z][a-zA-Z0-9+.-]*:.*" text != null;

  # The directory of a registry that this machine holds, or `null`.
  registryDir =
    package:
    if package.source ? registry && !hasScheme package.source.registry then
      package.source.registry
    else
      null;

  localRelative =
    package: file:
    if file ? path then
      let
        directory = registryDir package;
      in
      if directory == null then
        throw (
          "uv.nix: '${package.name}' names an artefact by a path, and its source is not a "
          + "registry of this machine. uv writes such a row for a local registry only, and it "
          + "refuses the same file with MissingUrl."
        )
      else
        joinPath directory file.path
    else if package.source ? path then
      package.source.path
    else
      null;
  isLocal = package: file: localRelative package file != null;

  # **`builtins.path` names the one file, and not the directory that holds
  # it.** A second file of the index is therefore not an input of the planner,
  # and an edit of that second file does not change the plan.
  localArtifact =
    package: file:
    let
      relative = localRelative package file;
    in
    builtins.path {
      path = if isAbsolute relative then /. + relative else project + "/${relative}";
      name = baseNameOf relative;
    };

  # The file name that the index, or the project, gave an artefact. `installer`
  # reads the name of a wheel, and the store path carries a hash in front of
  # that name, so the name travels beside the path.
  # `ddrn_venv._uvlock.File.name` reads the same keys.
  fileName =
    package: file:
    if file ? filename then
      file.filename
    else if isLocal package file then
      baseNameOf (localRelative package file)
    else
      lib.last (lib.splitString "/" (artifactUrl package file));

  # One entry of the menu. **No filter and no preference apply here.** Every
  # wheel of every package becomes an entry, and the planner picks among them
  # with the PEP 425 tags of the target.
  artifactOf =
    package: kind: file:
    {
      inherit kind;
      package = package.name;
      version = package.version or (throw "uv.nix: '${package.name}' has an artefact and no version");
      filename = fileName package file;
    }
    // (
      if isLocal package file then
        # A file that this machine holds. `default.nix` passes such an entry
        # through, because there is no fetch to keep lazy.
        { path = "${localArtifact package file}"; }
      else
        {
          # **uv writes `sha256:<hex>`, and `fetchurl` reads that spelling.**
          # So no translation sits between the lock file and the fetch, and a
          # mismatch is a mismatch of the lock file itself.
          drv = pkgs.fetchurl {
            url = artifactUrl package file;
            inherit (file) hash;
            name = fileName package file;
          };
        }
    )
    # **One archive of a URL source may hold more than one project.** The
    # subdirectory travels with the artefact, so the planner of this
    # distribution reads the declaration in the right place, and the build runs
    # there. `pypa/hatch` holds `hatchling` in `backend`, and it is the case
    # that `tests/fixtures/locks/url-subdirectory.lock` records.
    // lib.optionalAttrs (package.source ? subdirectory) {
      inherit (package.source) subdirectory;
    };

  # A local package carries no `wheels` and no `sdist`, so it contributes no
  # entry. The planner still resolves it, because the lock file still holds its
  # edges.
  artifactsOf =
    package:
    map (artifactOf package "wheel") (package.wheels or [ ])
    ++ map (artifactOf package "sdist") (lib.optional (package ? sdist) package.sdist);

  # The one `version` that this reader accepts. `ddrn_venv._uvlock` asserts the
  # same value inside the planner, and so does `uv2nix`. This assertion is the
  # earlier of the two, and it names the file.
  lockVersion = document.version or null;

  planner = import ./. {
    inherit
      pkgs
      nanopynixEnv
      installer
      markers
      noBinary
      fromNixpkgs
      python
      uvEditables
      ;

    artifacts = lib.concatMap artifactsOf document.package;

    # `uv.lock` travels into the sandbox as a store path, and the planner
    # reads it there. The Nix half never resolves it.
    uvLock = lockFile;
    # The default takes every member, with no extra and no group, which is
    # what `uv sync --all-packages` takes. A caller that wants one member of a
    # workspace names that member alone.
    uvSelect = if select != null then select else lib.genAttrs members (_: [ ]);

    # The `pyproject.toml` of each member, so that the planner can refuse a
    # lock file that the project has grown past.
    uvDeclarations = declarations;

    # **The roots of the environment come out of the resolution**, so this list
    # adds nothing to them. A caller that wants one more package than the lock
    # file resolves puts a PEP 508 requirement here.
    roots = [ ];

    venvName = if venvName != null then venvName else projectName;

    # **A local project of a uv workspace comes through `uvEditables`, and not
    # through here.** The resolution decides which of them this target
    # installs, and only the planner resolves. This argument stays for a tree
    # that no lock file mentions.
    editables = { };
  };
in

lib.throwIfNot (lockVersion == 1)
  "uv.nix: ${lockFile} says version ${builtins.toJSON lockVersion}, and this reader accepts 1"
  (
    lib.throwIf (lib.subtractLists localNames fromSdist != [ ]) (
      "uv.nix: fromSdist names ${builtins.toJSON (lib.subtractLists localNames fromSdist)}, "
      + "and ${lockFile} holds no local package of that name. It holds ${builtins.toJSON localNames}."
    ) planner
  )
