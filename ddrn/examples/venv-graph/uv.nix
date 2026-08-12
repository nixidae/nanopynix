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
  #
  # **The default builds it out of this repository, so a caller passes no
  # `--argstr nanopynixEnv`.** `run-uv.sh` and `tests/toolchain.py` still pass
  # the value, because each of them builds the environment once and reuses it,
  # and Nix never evaluates a default that a caller replaces.
  #
  # It costs 2.8 s of evaluation, measured on 2026-08-12 with `nix eval` over
  # the `drvPath` of the same expression. That is the whole of this repository,
  # so a copy of this file outside the checkout has to pass the argument.
  #
  # **An edit of this directory does not change the value.** The source filter
  # of `nanopynixMaster` leaves `ddrn/` out, measured on 2026-08-12: an added
  # line in `./README.md` gave the same `ddrn-planner-env.drv`. So an edit here
  # rebuilds no planner environment.
  nanopynixEnv ? (import ../../.. { }).nanopynixMaster.pythonSet.mkVirtualEnv "ddrn-planner-env" {
    nanopynix = [ ];
  },
  # The uv project to build. It holds `pyproject.toml` and `uv.lock`.
  project ? ./projects/pure-app,
  # Which packages of the lock file to install, and which extras and which
  # dependency groups of each one to enable.
  #
  # **`uv.lock` records none of these three decisions.** `uv sync` takes them
  # on its command line, and this argument is the same decision. `null` means
  # every workspace member, with `extras` and `groups` below deciding the rest.
  select ? null,
  # Which extras of each member to enable, when `select` says nothing.
  #
  # **uv keeps no default for an extra in `pyproject.toml`**, and it reads
  # `DefaultExtras::default()` in `sync.rs` with the project left unread. So
  # `null` here is `uv sync` with no flag, which takes no extra.
  #
  # `"all"` is `uv sync --all-extras`, and it reads the keys of
  # `[project.optional-dependencies]` of each member.
  # `[ "docs" "test" ]` names them, for every member, the way `--extra` does.
  extras ? null,
  # Which dependency groups of each member to enable, when `select` says
  # nothing.
  #
  # **uv does keep a default for a group.** `default_dependency_groups` of
  # `crates/uv/src/commands/project/mod.rs` reads `tool.uv.default-groups` of
  # the project, and it gives `[ "dev" ]` when that table says nothing. So
  # `null` here is `uv sync` with no flag, and it is **not** "no group at all".
  # That table also takes the literal `"all"`.
  #
  # `"all"` is `uv sync --all-groups`. `[ "docs" ]` names them.
  groups ? null,
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
  #
  # **`null` reads `[tool.ddrn] from-sdist` of the project**, the way `select`,
  # `extras` and `groups` read the project for their own decision. A list
  # replaces that table, and `[ ]` is therefore "no member ships an archive".
  fromSdist ? null,
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
  #
  # **The project declares the same thing in `[tool.ddrn.from-nixpkgs]`**, with
  # the attribute name as a string, and this argument replaces the declaration
  # of each package that it names.
  fromNixpkgs ? { },
  # What one package needs to build, by package name, as
  # `{ psutil = [ pkgs.stdenv.cc ]; }`. `./default.nix` gives the whole reason
  # at its `buildTools` argument, and the short one is this: a source
  # distribution that holds C code needs a compiler, and no other node does.
  #
  # **The project declares the same thing in `pyproject.toml`**, under
  # `[tool.ddrn.build-tools]`, and this argument replaces the declaration of
  # each package that it names. `declaredBuildTools` below gives the table and
  # the reason to prefer it.
  buildTools ? { },
  # Whether uv itself checks the lock file, in a derivation of its own, before
  # the planner runs.
  #
  # **The planner is the check, and this is the backstop.** `_freshness`
  # compares the lock file with the `pyproject.toml` of each member, and with
  # the directories of the workspace. `Lock::satisfies` of uv holds 21 ways for
  # a lock file to be stale, and those two comparisons are the ones a build
  # reaches. This argument runs `uv lock --check` over the project and gives
  # the other nineteen.
  #
  # It costs two things, so it is off by default:
  #
  # - **The whole project tree enters the store.** The rest of this file reads
  #   two files of the project, so an edit of any other file changes nothing.
  #   With this argument on, every edit of every file makes a new store path
  #   and a new check.
  # - **uv builds a package to read a dynamic version.** A project with
  #   `dynamic = ["version"]` needs its backend, and the check has no network,
  #   so uv fails for a reason that is not a stale lock file.
  #
  # The check itself costs 27 ms, measured over `projects/workspace` with a
  # cold cache.
  uvCheck ? false,
  # Which members to render as an application, by package name. One name, a
  # list of them, or `null` for the environment itself.
  #
  # **A virtual environment is not a program.** It carries `bin/python`,
  # `bin/activate` and the console script of every member, so a NixOS
  # configuration that puts one in `environment.systemPackages` shadows the
  # interpreter of the system with the one of the environment. An application
  # is the tree of the named members, with each entry a link into that same
  # environment:
  #
  #     application = "ws-cli";              ->  bin/ws-cli, and nothing else
  #     application = [ "ws-cli" "ws-web" ]; ->  the programs of both members
  #
  # **A list is one output, and not one for each name.** A project that ships
  # three programs ships them together, and three separate applications would
  # each hold a link into the same environment.
  #
  # **The closure does not shrink**, because every link names a path inside the
  # environment. What changes is what reaches the PATH of a user. The
  # `environment` attribute of the result gives that environment, for a
  # development shell of the same project.
  #
  # `pyproject.nix` calls this `util.mkApplication`, and it takes the same two
  # inputs: the environment for the content, and the `$out` of the one package
  # for the shape.
  application ? null,
  # The program that `meta.mainProgram` of the `app` attribute names.
  #
  # **`builtins.outputOf` gives a string, and a string carries no `meta`.** So
  # the `app` attribute of the result is a derivation whose whole content is one
  # symbolic link to the application, and `meta.mainProgram` of that derivation
  # is what this argument names. `ddrn/NIX-FACTS.md` gives the measurement,
  # under "A dynamic output carries no `meta`".
  #
  # `null` takes the sole name of `application`, which is right whenever the
  # console script of a member carries the name of that member. **The wrapper
  # verifies the name rather than trust it**, so a package whose entry point is
  # called something else fails the build and names what the application holds.
  #
  # An `application` of several members has no sole name, so `app` then carries
  # no `mainProgram` unless this argument gives one.
  mainProgram ? null,
}@arguments:

let
  inherit (pkgs) lib;

  # **A directory that is not a uv project must say that.** Nix names the file
  # that is absent, and it says nothing about what that file is for. So a
  # reader who gave `project` the wrong directory, and a reader whose project
  # never ran `uv lock`, got the same message about a path.
  #
  # `builtins.pathExists` asks the question before `builtins.readFile` answers
  # it. The check is outermost, because `document` below forces the read.
  absent = lib.filter (name: !builtins.pathExists (project + "/${name}")) [
    "pyproject.toml"
    "uv.lock"
  ];

  checkProject =
    value:
    lib.throwIf (absent != [ ]) (
      "uv.nix: ${toString project} holds no ${lib.concatStringsSep " and no " absent}. "
      + "`project` names the directory of a uv project, which holds pyproject.toml and uv.lock. "
      + "Run `uv lock` in that directory when the lock file is the one that is absent."
    ) value;

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

  # The name that the caller asked for, or the name of the project.
  environmentName = if venvName != null then venvName else projectName;

  # **The roots of a lock file are its workspace members, and `[manifest]`
  # names them.** uv writes that table for a workspace only, so a lock file of
  # one project holds none, and that one project is the one member. The names
  # here are package names, and not the directories that
  # `[tool.uv.workspace] members` holds.
  members = document.manifest.members or [ projectName ];

  # **A member that the lock file never saw reaches no comparison of
  # `[package.metadata]`**, because `declarations` below is built from the lock
  # file. `./workspace.nix` walks the tree instead, and it gives the
  # `pyproject.toml` of every directory that holds one. `ddrn_venv._workspace`
  # reads `[tool.uv.workspace] members` out of that listing and answers which
  # directories are members. **The walk decides nothing**, so no glob and no
  # rule of uv is in the Nix language.
  workspace = import ./workspace.nix {
    inherit lib;
    root = project;
  };

  # **uv reads the lock file, in a derivation that the planner waits for.**
  # This is the `uvCheck` argument, and the whole project tree is the input.
  # Every other read of this file names one file, so the tree is here and
  # nowhere else.
  #
  # `--offline` is what makes the check honest. A sandbox has no network, so uv
  # would fail on a connection either way, and the flag makes uv say so at once
  # rather than wait for a timeout. **A refusal offline is still a refusal**: uv
  # reaches the network only when `Lock::satisfies` gave a reason to resolve
  # again, so a lock file that satisfies the project never downloads.
  #
  # `--python` names the interpreter of the environment, because uv finds none
  # in a sandbox and offers to download one.
  #
  # **`--check` is the only mode of `uv lock` that a derivation can read**,
  # because the verdict of a derivation is the exit code of its builder.
  # `LockMode` (`crates/uv/src/commands/project/lock.rs`) holds four modes, and
  # each of the other three exits 0 over a lock file that is stale:
  #
  # | the mode | what it does | over a stale project |
  # | --- | --- | --- |
  # | `uv lock` | resolves, and writes `uv.lock` | writes a new file, exits 0 |
  # | `--dry-run` | resolves, and writes nothing | prints the difference, exits 0 |
  # | `--frozen` | reads the lock file, and resolves nothing | exits 2 for a missing member, and 0 for a marker |
  # | `--check` | asserts the file would not change | exits 1 |
  #
  # `uv lock` is the worst of the three here: it would rewrite the copy of the
  # lock file in the sandbox, and the build would then plan from the file that
  # the graph was built from and never learn of the difference.
  lockCheck =
    pkgs.runCommand "uv-lock-check-${projectName}"
      {
        nativeBuildInputs = [ pkgs.uv ];
        source = builtins.path {
          path = project;
          name = "${projectName}-source";
        };
      }
      ''
        cp -r "$source" project
        chmod -R u+w project
        cd project

        export HOME="$PWD"
        export UV_CACHE_DIR="$PWD/.uv-cache"

        if uv lock --check --offline --python ${python}/bin/python3; then
          echo "uv agrees that uv.lock is current" > "$out"
          exit 0
        fi

        # **`--frozen` names the cause, and it runs only after `--check`
        # refused.** `--check` resolves the project again, and a sandbox has no
        # index to resolve against, so it reports the package that it could not
        # read rather than the difference that made it resolve. `--frozen`
        # compares the lock file with the project and resolves nothing, so it
        # names a structural difference exactly: a workspace member that the
        # lock file does not hold, for one.
        #
        # It is a diagnostic and not the check. `--frozen` accepts a marker
        # that `--check` refuses, and uv says so itself: "the lockfile was only
        # checked for validity, not whether it is up-to-date". That warning is
        # also the reason this runs on the failure path alone, because a
        # successful build would otherwise print it every time.
        echo >&2
        echo "==> asking uv again, without a resolution, for the cause:" >&2
        uv lock --frozen --offline --python ${python}/bin/python3 >&2 || true

        cat >&2 <<'MESSAGE'

        uv refused the lock file of this project, and the messages above are its own.

        This is the `uvCheck` argument of `uv.nix`. uv ran with no network, so
        `--check` reports a package as unavailable when it wanted to resolve the
        project again. **That is the refusal, and not a fault of the sandbox**: uv
        resolves again only when `uv.lock` does not satisfy `pyproject.toml`.

        Run `uv lock` in the project, and build again.
        MESSAGE
        exit 1
      '';

  # The names that `application` holds, as a list, whichever form it took.
  applicationNames =
    if application == null then
      [ ]
    else if builtins.isList application then
      application
    else
      [ application ];

  # The program that `app` names, or `null` when nothing decides one.
  chosenProgram =
    if mainProgram != null then
      mainProgram
    else if builtins.length applicationNames == 1 then
      builtins.head applicationNames
    else
      null;

  # The application as a derivation. `./application.nix` says why a derivation
  # is necessary, and what the build of it verifies.
  applicationWrapper = import ./application.nix {
    inherit pkgs;
    application = builtins.outputOf planner.outPath "out";
    program = chosenProgram;
    name = environmentName;
  };

  # The same call with no application, which is the target of the environment.
  # `environment` of the result gives it, and `env` builds it.
  environmentTarget =
    if application == null then
      null
    else
      import ./uv.nix (
        arguments
        // {
          application = null;
          # **The suffix has to match, or one environment takes two store
          # paths.** `Plan.root` names the environment of an application
          # `<name>-env`, and a node name is the name of a store path.
          venvName = "${environmentName}-env";
        }
      );

  # **The environment as a derivation, so that one command builds it.**
  # A planner writes a `.drv`, so a caller of the command line builds the
  # planner and then builds `"$drv^out"`. This attribute holds the second step,
  # so `nix build --file ./uv.nix env` is the whole build and `result/bin`
  # holds the programs.
  #
  # It is the same shape as `app`, and `./application.nix` gives the reason
  # that shape is necessary: `builtins.outputOf` gives a string, and `nix
  # build` takes a derivation.
  environmentWrapper =
    let
      target = if environmentTarget == null then planner else environmentTarget;
      name = if application == null then environmentName else "${environmentName}-env";
    in
    import ./application.nix {
      inherit pkgs name;
      application = builtins.outputOf target.outPath "out";
      program = null;
    };

  # **A development shell over that same environment.**
  #
  # `nix develop --file ./uv.nix shell` puts `bin` of the environment on the
  # PATH, so `python`, `pytest` and the console script of every member run from
  # the shell.
  #
  # **The shell sets `DDRN_EDITABLE_ROOT`, and that is what it is for.** A PEP
  # 660 editable of this lab redirects to that variable rather than to a store
  # path, so one environment serves every tree. Every `check-*.nix` of this
  # directory sets the variable, because a derivation that reads an editable
  # must. A shell that did not set it would give an environment whose editable
  # members import nothing, and the message would name a directory called
  # `/src`.
  #
  # `toString` gives the directory of this machine, and not the copy of it in
  # the store. So the shell reads the tree that the user edits.
  #
  # `mkShellNoCC` is the shell with no C toolchain. Every package of the
  # environment comes out of the lock file, and the graph built each one
  # already, so a compiler in the shell adds a closure that nothing calls.
  developmentShell = pkgs.mkShellNoCC {
    name = "${environmentName}-shell";
    packages = [ environmentWrapper ];
    DDRN_EDITABLE_ROOT = toString project;
  };

  # **`tool.uv` is a setting of the project, and uv copies none of it into the
  # lock file.** So this reads the setting where uv keeps it, rather than
  # invent an argument for the same decision. `no-binary-package` names each
  # package to build from its source distribution even when a wheel fits.
  noBinary = pyproject.tool.uv."no-binary-package" or [ ];

  # **The project says which package needs a compiler, and it says so beside
  # `tool.uv`.** `noBinary` above reads a setting of the project where uv keeps
  # it. A build tool is the same kind of setting, and PEP 518 gives `[tool.*]`
  # to exactly this. So a user of `./uv.nix` writes one file:
  #
  #     [tool.uv]
  #     no-binary-package = ["cffi"]
  #
  #     [tool.ddrn.build-tools]
  #     cffi = ["stdenv.cc", "pkg-config", "libffi.dev", "libffi"]
  #
  # and `nix build --file uv.nix env --arg project ./.` needs no second
  # argument. uv2nix asks for an overlay in the Nix language for the same
  # decision, and `ddrn/PACKAGING-FACTS.md` records what that costs a user.
  #
  # **A value is a lookup, and it is not a rule.** The dots walk `pkgs`, so
  # `libffi.dev` names the `dev` output of one package. Nothing here globs,
  # compares a version or evaluates a marker: those belong to the planner, and
  # `ddrn/PACKAGING-FACTS.md` gives the boundary.
  #
  # **The key is a package name, and no check reads it here.** A name that this
  # target does not install is not a mistake: one lock file resolves to more
  # than one environment, so a table that covers the workspace names packages
  # that a single member never builds.
  ddrn = pyproject.tool.ddrn or { };

  # One attribute of `pkgs`, named by a dotted string, or a message that says
  # which table and which package asked for it.
  attributeOf =
    table: package: name:
    let
      parts = lib.splitString "." name;
    in
    if lib.hasAttrByPath parts pkgs then
      lib.getAttrFromPath parts pkgs
    else
      throw (
        "uv.nix: [tool.ddrn.${table}] of ${projectName} gives '${package}' the name "
        + "'${name}', and nixpkgs holds no attribute of that name. Each value names an "
        + "attribute of the nixpkgs that this call takes, and a dot walks into it."
      );

  declaredBuildTools = lib.mapAttrs (package: names: map (attributeOf "build-tools" package) names) (
    ddrn."build-tools" or { }
  );

  # **The argument wins for the package that it names, and for no other one.**
  # A caller who overrides `cffi` on the command line keeps the declaration of
  # every other package, which is what a per-package override means.
  chosenBuildTools = declaredBuildTools // buildTools;

  # **Which package comes from nixpkgs rather than the index, in that same
  # table.** The reason is the one that `./default.nix` gives at its
  # `fromNixpkgs` argument: a manylinux wheel carries a copy of every library
  # that it links against, and a package of nixpkgs links against the store.
  #
  #     [tool.ddrn.from-nixpkgs]
  #     pyzmq = "python3Packages.pyzmq"
  declaredFromNixpkgs = lib.mapAttrs (package: name: attributeOf "from-nixpkgs" package name) (
    ddrn."from-nixpkgs" or { }
  );

  chosenFromNixpkgs = declaredFromNixpkgs // fromNixpkgs;

  # **Which member ships an archive rather than a redirect.** `null` reads the
  # project, and a list replaces what the project says. This is the shape that
  # `select`, `extras` and `groups` already take, for the same reason: the
  # project holds a default, and the caller of one build overrides it.
  chosenFromSdist = if fromSdist != null then fromSdist else ddrn."from-sdist" or [ ];

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
              if lib.elem package.name chosenFromSdist then
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
      application
      python
      uvEditables
      ;

    buildTools = chosenBuildTools;
    fromNixpkgs = chosenFromNixpkgs;

    artifacts = lib.concatMap artifactsOf document.package;

    # `uv.lock` travels into the sandbox as a store path, and the planner
    # reads it there. The Nix half never resolves it.
    uvLock = lockFile;
    # The default takes every member, with no extra and no group, which is
    # what `uv sync --all-packages` takes. A caller that wants one member of a
    # workspace names that member alone.
    # **`null` travels, and the planner decides.** Which extras a member has is
    # in its `pyproject.toml`, and which groups uv takes by default is in
    # `tool.uv.default-groups` of that same file. `ddrn_venv._workspace` reads
    # both out of the listing below, so no rule of uv is in this file.
    uvSelect = select;
    uvExtras = extras;
    uvGroups = groups;

    # The `pyproject.toml` of each member, so that the planner can refuse a
    # lock file that the project has grown past.
    uvDeclarations = declarations;

    # The `pyproject.toml` of every directory under the workspace, so that the
    # planner can refuse a lock file that the workspace has grown a member
    # past. `./workspace.nix` walks, and `ddrn_venv._workspace` decides.
    uvWorkspace = workspace;

    # The backstop, which is off by default. `null` puts no store path in the
    # planner, so the project tree stays out of the store.
    uvCheck = if uvCheck then lockCheck else null;

    # **The roots of the environment come out of the resolution**, so this list
    # adds nothing to them. A caller that wants one more package than the lock
    # file resolves puts a PEP 508 requirement here.
    roots = [ ];

    venvName = environmentName;

    # **A local project of a uv workspace comes through `uvEditables`, and not
    # through here.** The resolution decides which of them this target
    # installs, and only the planner resolves. This argument stays for a tree
    # that no lock file mentions.
    editables = { };
  };
in

# `checkProject` is outermost, because every check below reads a file of the
# project and a directory that holds no such file must say that first.
checkProject (
  lib.throwIfNot (lockVersion == 1)
    "uv.nix: ${lockFile} says version ${builtins.toJSON lockVersion}, and this reader accepts 1"
    (
      lib.throwIf (lib.subtractLists localNames chosenFromSdist != [ ])
        (
          "uv.nix: fromSdist names ${builtins.toJSON (lib.subtractLists localNames chosenFromSdist)}, "
          + "and ${lockFile} holds no local package of that name. It holds ${builtins.toJSON localNames}."
        )
        (
          # **`lockCheck` is here whatever `uvCheck` says**, and Nix evaluates it
          # only when something asks for it. So `nix build --file ./uv.nix
          # lockCheck` runs uv over the project and builds no graph, and the
          # project tree still enters no store path of a build that never names
          # this attribute.
          #
          # `environment` is this same project with no application, so a caller
          # that ships one program and develops against the whole environment
          # writes the arguments once:
          #
          #     target = import ./uv.nix { project = ./.; application = "ws-cli"; };
          #     { packages.default = target.app; devShells.default = target.shell; }
          #
          # `app` is the same application as a derivation, for `nix run`, for
          # `lib.getExe` and for `environment.systemPackages`. `./application.nix`
          # says why a wrapper is necessary.
          #
          # `shell` is a development shell over the environment, and it sets
          # `DDRN_EDITABLE_ROOT` to the project. `developmentShell` above says
          # why that variable decides whether an editable member imports.
          #
          # `env` is the environment as a derivation, and it is what makes one
          # command enough:
          #
          #     nix build --file ./uv.nix env --arg project /path/to/project
          #     ./result/bin/python -c 'import rich'
          #
          # The result itself is the planner, so a build of it gives a `.drv`
          # and a second command builds `"$drv^out"`. `env` holds that second
          # step. It is the environment whether this call names an application
          # or not, so `app` and `env` of one call are the two halves of one
          # project.
          #
          # **It costs a second planner, and no second environment.** The two
          # planners differ in one variable, so Nix builds both. Every node that
          # they submit is content-addressing and identical, so the environment
          # itself is one store path that both graphs reach.
          planner
          // {
            inherit lockCheck;
            app =
              if application == null then
                throw (
                  "uv.nix: `app` is the application as a derivation, and this call names no application. "
                  + "Pass `application`, or build this result itself for the environment."
                )
              else
                applicationWrapper;
            env = environmentWrapper;
            shell = developmentShell;
            environment = if environmentTarget == null then planner else environmentTarget;
          }
        )
    )
)
