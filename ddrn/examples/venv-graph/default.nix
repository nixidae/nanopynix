# A Python environment, resolved by `packaging` and built as a graph.
#
# `ddrn/examples/venv` is the same environment under the released protocol. It
# makes the same packaging decisions and then emits **one** derivation, because
# a planner that writes to `$out` can emit exactly one. Two things follow, and
# this example removes both:
#
#   1. Every wheel is installed in a single build step, so a change to one
#      wheel rebuilds all of them.
#   2. A source distribution cannot be built at all. The backend that builds it
#      would have to exist before the plan runs, and the plan is what chooses
#      the backend.
#
# **The Nix half here knows nothing about Python.** It reads the lock file,
# makes a `fetchurl` for every artefact, and hands the planner that menu. The
# planner decides which artefacts this target needs, and what the graph over
# them is. `graph.nix` says how one node is built.
#
# This example does NOT run under the Nix of this repository's pin. It needs
# the patched Nix that `nix/nix-master.nix` reads.
# `ddrn/examples/venv-graph/run.sh` sets it up.
{
  pkgs ? import <nixpkgs> { },
  # A Python environment that has nanopynix, built against the same Nix that
  # builds this derivation.
  #
  # **A derivation here keeps the evaluation pure, and a string does not.**
  # `builtins.storePath` is refused in pure evaluation mode. `run.sh` and
  # `tests/test_sealed_graph.py` both take the environment from a separate
  # build, so both pass a string and both pass `--impure`. A caller that builds
  # the environment in this evaluation passes the derivation, and needs
  # neither.
  nanopynixEnv,
  # The lock file to resolve. The default is the one beside this file, and
  # every artefact of it comes from PyPI.
  lock ? builtins.fromJSON (builtins.readFile ./lock.json),
  # **The seam between the reader and the graph.** One entry for each
  # artefact: the metadata a planner reads, and the derivation that produces
  # the file. `null` means "read the lock file above, and fetch each artefact".
  #
  # A reader of a different lock format fills this list, and so does a test
  # that builds its artefacts rather than fetching them.
  artifacts ? null,
  roots ? lock.roots,
  installer ? lock.installer,
  # **A `uv.lock` for the planner to resolve, as a path.** `./uv.nix` gives a
  # value here, and `null` means that `roots` is the whole selection.
  #
  # The Nix half does not read this file. It reads the artefacts of it, which
  # `./uv.nix` enumerates, and it hands the file itself to the planner.
  # `ddrn_venv._uvlock` resolves it there, with `packaging`, and adds what it
  # resolves to `roots`.
  uvLock ? null,
  # Which packages of `uvLock` to install, and which extras and which
  # dependency groups of each one. `uv.lock` records none of these three
  # decisions, so they are an argument.
  uvSelect ? null,
  # Which extras of each member to enable, and which dependency groups, when
  # `uvSelect` is `null`. `./uv.nix` gives the whole rule at its `extras` and
  # `groups` arguments, and `ddrn_venv._workspace` holds it. `null` on both is
  # `uv sync` with no flag.
  uvExtras ? null,
  uvGroups ? null,
  # The tree of each package of `uvLock` that builds from a directory, keyed by
  # its normalised name, as `{ source, root, mode, subdirectory }`. **Only the
  # planner knows which of them this target installs**, because only the planner
  # resolves, so every such package of the lock file gets an entry and the
  # resolution picks among them.
  #
  # A git source arrives here too. `uv.nix` fetches the checkout during
  # evaluation and gives it `mode = "wheel"`, because a checkout is a directory
  # and no archive stands between the tree and the wheel.
  uvEditables ? { },
  # The `pyproject.toml` of each member of `uvLock` that lives in a directory
  # of the workspace, keyed by its normalised name, as a store path.
  #
  # **This is what tells a stale lock file from a current one.** `uv.lock`
  # records the declaration of each member in `[package.metadata]`. A project
  # whose `pyproject.toml` has grown a dependency since the last `uv lock`
  # would otherwise build the environment of the old declaration and say
  # nothing. `ddrn_venv._freshness` makes the comparison, and it gives the
  # rules that it takes from `Lock::satisfies` of uv.
  #
  # An empty set turns the check off, and that is the default. A caller that
  # gives `uvLock` but no project directory has nothing to compare against.
  # `./uv.nix` reads a project, so it names one file for each member.
  # `tests/sealed.nix` gives no `uvLock` at all, so no comparison reaches it.
  uvDeclarations ? { },
  # The `pyproject.toml` of every directory under the workspace, keyed by the
  # path of that directory relative to the root.
  #
  # **`uvDeclarations` cannot find a member that the lock file never saw**,
  # because the lock file is the list of what it compares. A directory that a
  # `[tool.uv.workspace] members` glob names, and that `uv.lock` does not hold,
  # is the other half of a stale lock file. `./workspace.nix` walks the tree to
  # list it, and `ddrn_venv._workspace` decides which directories are members.
  #
  # An empty set turns that comparison off, and it is the default.
  uvWorkspace ? { },
  # A derivation that runs `uv lock --check` over the project, or `null`.
  #
  # **This is a backstop, and the planner is the check.** `_freshness` compares
  # the lock file with each `pyproject.toml`, and `Lock::satisfies` of uv holds
  # 21 ways for a lock file to be stale. The planner implements the ones that a
  # build reaches. This derivation asks uv itself, so the ones that the planner
  # does not implement stop the build as well.
  #
  # The value is an input of the planner and nothing reads it, so uv answers
  # before any node of the graph builds. `./uv.nix` builds the derivation, and
  # its `uvCheck` argument says what it costs.
  uvCheck ? null,
  # The one member to render as an application, or `null` for the environment.
  #
  # **A virtual environment is not a program.** It carries `bin/python`,
  # `bin/activate` and the console script of every member, so a NixOS
  # configuration that puts one in `environment.systemPackages` shadows the
  # interpreter of the system. An application is the tree of one member,
  # pointing into that same environment, and `scripts/make-application.py`
  # gives the two rules that decide what reaches it.
  #
  # `pyproject.nix` calls this `util.mkApplication`, and it does the same
  # thing: it reads the `$out` of one package as the template of the shape.
  # Here that template is the node that installed the package, which is a
  # store path of this same graph.
  application ? null,
  # Each package to build from its source distribution, even when a wheel fits.
  # This is `tool.uv.no-binary-package`, which uv keeps in `pyproject.toml` and
  # writes into no lock file, so `./uv.nix` reads it from the project.
  noBinary ? [ ],
  # Each package that nixpkgs supplies instead of the index, as
  # `{ pyzmq = pkgs.python3Packages.pyzmq; }`. The planner then makes no wheel
  # node for that name at all.
  #
  # **This is the tier 4 mechanism, and it builds nothing here.** Each
  # derivation is instantiated by this evaluation and handed to the planner as
  # a `.drv` and an output path with the dependency between them discarded, the
  # same way that every artefact of the menu travels. `graph.nix` puts the
  # dependency back for the ones the planner used, so the outer store
  # substitutes the output from a cache. `ddrn/PACKAGING-FACTS.md` gives the
  # measurement, under "Tier 4, the pre-instantiated menu".
  #
  # **The version is the one that nixpkgs holds, and not the one that the lock
  # file pins.** Nothing reconciles the two, and the planner prints both.
  fromNixpkgs ? { },
  # What one package needs to build, by package name, as
  # `{ psutil = [ pkgs.stdenv.cc ]; }`.
  #
  # **A wheel needs nothing here, and a source distribution that holds C code
  # needs a compiler.** `graph.nix` gives every node `coreutils`, `unzip`,
  # `tar`, `gzip` and the interpreter, and no more: a compiler in that list
  # would enter the closure of every pure Python wheel as well.
  #
  # Each entry is a store path, and it reaches the node in four ways:
  #
  #   PATH             `bin` of the entry, in front of the base path.
  #   CPPFLAGS         `-isystem` for `include` of the entry.
  #   LDFLAGS          `-L` for `lib` of the entry.
  #   PKG_CONFIG_PATH  `lib/pkgconfig` of the entry.
  #
  # A directory that the entry does not hold costs nothing, so an entry that
  # is only a program adds no flag that matters.
  #
  # **`NIX_CFLAGS_COMPILE` is not one of them, and `graph.nix` says why.** The
  # wrapper of the compiler reads a name that carries the salt of its
  # platform, and only a setup hook writes that name.
  #
  # **Name each output that the build reads.** A node of this graph runs no
  # `stdenv`, so no setup hook follows `.dev` for you:
  # `[ pkgs.stdenv.cc pkgs.zlib.dev pkgs.zlib ]` gives the compiler, the
  # headers and the library.
  #
  # **The tools reach the node of that package alone.** A build that needs the
  # compiler of another package is a build whose declaration is wrong, and the
  # failure names the package that has to declare it.
  buildTools ? { },
  # The interpreter that the environment runs, and that every install step
  # uses. **This is the whole runtime closure of an environment**, and
  # `ddrn/PACKAGING-FACTS.md` measures it: 219 MB of the 238 MB that the
  # environment of `projects/pure-app` costs.
  #
  # **Keep the default unless the target is an empty store.** A store that
  # holds `pkgs.python3` already shares 22 of the 23 paths of any override, so
  # a stripped interpreter adds a second CPython rather than replacing the
  # first. The environment then costs 82 MB on that target, against 19 MB with
  # this default. A single-purpose container image is the case where the
  # override wins, and it wins 33% of 238 MB there.
  #
  # `pkgs.python3Minimal` is not a candidate at all: it drops `zlib`, so it
  # cannot open a wheel. A stripped override keeps every optional dependency,
  # and it costs a build of CPython that no binary cache answers:
  #
  #     python = pkgs.python3.override {
  #       stripConfig = true;
  #       stripIdlelib = true;
  #       stripTests = true;
  #       stripTkinter = true;
  #     };
  #
  # **The marker environment comes from this same interpreter**, so the version
  # that the planner plans for and the version that the environment holds are
  # one value and cannot drift.
  python ? pkgs.python3,
  venvName ? "demo-venv",
  # Overrides for the PEP 508 marker environment of the target.
  #
  # `platform_release` and `platform_version` are the two keys that a build
  # cannot know, so both are empty by default. A lock file that carries a
  # marker of either key needs a value here, and `uv2nix` takes the same
  # override for the same reason.
  markers ? { },
  # One entry for each local project to install from its own tree.
  #
  #   src   the project tree, which Nix copies into the store. The backend
  #         reads it, and the environment does not.
  #   root  what an editable install redirects to. A literal path names one
  #         tree forever. A reference to a variable, which is the default,
  #         keeps the environment the same bytes for every tree, so the tree
  #         can change with no rebuild. The other two modes hold the code, so
  #         they need no root and the default is empty.
  #   mode  which of the three forms to build. `editable` is the PEP 660
  #         install that `uv sync` performs, and it is the default. `sdist` is
  #         the archive that `uv build` writes and that an index serves, and a
  #         second node builds the wheel out of it. `wheel` is one PEP 517
  #         `build_wheel` call over the tree, which is what `pip install .`
  #         does and what a git source of `uv.lock` needs.
  #   subdirectory
  #         where the project lives inside `src`, when it is not at the top of
  #         it. `uv.lock` writes this for a repository that holds the project
  #         below its root. The default is empty.
  editables ? {
    myapp = {
      src = ./fixtures/myapp;
      root = "$DDRN_EDITABLE_ROOT";
    };
  },
}:

let
  # One `fetchurl` per artefact. `fetchurl` is a fixed-output derivation, so
  # instantiating it costs nothing and downloads nothing. The lock file lists
  # 24 artefacts, and this build downloads the ones that the planner picks.
  fetch =
    entry:
    pkgs.fetchurl {
      inherit (entry) url;
      hash = "sha256:${entry.sha256}";
      name = entry.filename;
    };

  # The artefacts, either as the caller gave them or as the lock file names
  # them. Each entry carries `drv`, which is a derivation and not a path, or
  # `path`, which names a file that this machine already holds.
  entries =
    if artifacts != null then
      artifacts
    else
      map (entry: (builtins.removeAttrs entry [ "url" ]) // { drv = fetch entry; }) lock.artifacts;

  # **The two `unsafeDiscard*` builtins are what keep the menu lazy.**
  # `unsafeDiscardOutputDependency` makes the `.drv` an input source of this
  # derivation while the *output* stays unbuilt, so the planner can name the
  # derivation without causing the download. `unsafeDiscardStringContext`
  # gives the planner the output path with no dependency on it.
  #
  # `graph.nix` puts the dependency back, with `builtins.appendContext`, for
  # the artefacts that the planner chooses.
  #
  # **An entry that carries a `path` needs neither builtin, and passes
  # through.** Such an artefact is a file that this machine already holds, so
  # there is nothing to fetch and nothing to defer. `uv.nix` writes one for a
  # source of kind `path`, and `graph.nix` names it with `builtins.storePath`.
  row =
    entry:
    if entry ? path then
      entry
    else
      (builtins.removeAttrs entry [ "drv" ])
      // {
        drv = builtins.unsafeDiscardOutputDependency entry.drv.drvPath;
        out = builtins.unsafeDiscardStringContext entry.drv.outPath;
      };

  # One entry for each package that nixpkgs supplies. It is the same shape that
  # `row` gives an artefact, plus the version that nixpkgs holds, which the
  # planner prints beside the version that the lock file pinned.
  nixpkgsRows = builtins.mapAttrs (
    _: drv: (row { inherit drv; }) // { inherit (drv) version; }
  ) fromNixpkgs;

  # A string names a store path that this evaluation did not make, and only an
  # impure evaluation may do that. A derivation needs no such primop.
  plannerEnvPath =
    if builtins.isString nanopynixEnv then builtins.storePath nanopynixEnv else nanopynixEnv;

  # `packaging` is what the planner needs, and only the planner needs it. The
  # environment that comes out carries no trace of it. It is pure Python, so it
  # goes on `PYTHONPATH` beside the nanopynix environment.
  #
  # **This one stays on `pkgs.python3`, and it does not follow `python`.** The
  # planner runs on `plannerEnv`, which is the interpreter that nanopynix was
  # built against, so `packaging` has to match that one and not the interpreter
  # of the environment. Neither reaches the runtime closure.
  packagingPath = "${pkgs.python3Packages.packaging}/${pkgs.python3.sitePackages}";

  # `ddrn_venv`, which every planner of this example imports. `fileset` takes
  # the Python files and nothing else, so a `__pycache__` of the working tree
  # does not reach the store and does not change this path.
  plannerLib = pkgs.lib.fileset.toSource {
    root = ./planner;
    fileset = pkgs.lib.fileset.fileFilter (file: file.hasExt "py") ./planner;
  };

  #: How many levels of planner a chain of source distributions may use. A
  #: planner of a distribution resolves a backend, and that backend may be a
  #: source distribution which needs a planner of its own. The recursion has to
  #: stop somewhere, so it stops here, and `graph.nix` names this constant when
  #: a chain runs past it.
  #:
  #: `tests/functional/dyn-drv/eval-submit.sh` of the patched Nix runs three
  #: levels, which is what this value allows.
  #:
  #: **The bound is tested at both edges.** `tests/fixtures/default.nix` holds
  #: a chain of four source distributions, and the two roots of
  #: `tests/sealed.nix` differ in nothing but the name: `link-a` uses three
  #: planners and builds, and `link-0` needs a fourth and makes `graph.nix`
  #: throw. `ddrn/NIX-FACTS.md` records the message and the level that it
  #: comes from.
  sdistPlannerDepth = 3;

  # **A caller that names no tool gets no key.** `Plan.tools_for` follows the
  # same rule for a node, and the reason is the same: the document is the
  # input of a derivation, so an empty key is a byte that every planner of
  # every project carries for nothing.
  #
  # Each entry becomes the store path of the package. The string carries its
  # context, so the planner takes a dependency on every one of them, and
  # `builtins.storePath` in `graph.nix` then finds each in the sandbox.
  buildToolPaths = pkgs.lib.optionalAttrs (buildTools != { }) {
    buildTools = pkgs.lib.mapAttrs (_: roots: map (root: "${root}") roots) buildTools;
  };

  baseInputs =
    buildToolPaths
    // {
      inherit
        roots
        installer
        noBinary
        application
        ;
      fromNixpkgs = nixpkgsRows;
      artifacts = map row entries;
      tools = {
        bash = "${pkgs.bash}";
        coreutils = "${pkgs.coreutils}";
        unzip = "${pkgs.unzip}";
        gnutar = "${pkgs.gnutar}";
        gzip = "${pkgs.gzip}";
        python = "${python}";
        # The three below reach only a node that installs a binary wheel.
        patchelf = "${pkgs.patchelf}";
        # `pkgs.autoPatchelfHook` is a setup hook, and a graph node runs no setup
        # hook. `pkgs.auto-patchelf` is the program that the hook calls.
        autoPatchelf = "${pkgs.auto-patchelf}";
        bintools = "${pkgs.stdenv.cc.bintools}";
        gccLib = "${pkgs.stdenv.cc.cc.lib}";
        # The three below reach only a node of kind `sdist`, which is a planner
        # and needs what this derivation needs: an interpreter that has
        # nanopynix, `packaging`, and the shared planner code.
        plannerEnv = "${plannerEnvPath}";
        packaging = "${pkgs.python3Packages.packaging}";
        plannerLib = "${plannerLib}";
      };
      scripts = {
        unpackWheel = "${./scripts/unpack-wheel.sh}";
        installWheel = "${./scripts/install-wheel.sh}";
        buildSdist = "${./scripts/build-sdist.sh}";
        buildLocal = "${./scripts/build-local.py}";
        makeVenv = "${./scripts/make-venv.py}";
        makeApplication = "${./scripts/make-application.py}";
        planSdist = "${./scripts/plan-sdist.py}";
        # `graph.nix` builds the planner of a source distribution, and that
        # planner evaluates `graph.nix` again for the graph it submits.
        graphNix = "${./graph.nix}";
      };
      # The planner reads the `pyproject.toml` of each tree below, and resolves
      # the backend that the tree asks for against the same lock file.
      editables = pkgs.lib.mapAttrs (_: spec: {
        source = "${spec.src}";
        # **Only an editable install has a root.** The other two forms hold the
        # code, so nothing redirects to a tree of the user and the value is empty.
        root = spec.root or "";
        mode = spec.mode or "editable";
        subdirectory = spec.subdirectory or "";
      }) editables;
    }
    // pkgs.lib.optionalAttrs (uvLock != null) {
      # The lock file itself, as a store path. `builtins.toJSON` keeps the string
      # context, so the file becomes an input source of every planner and is
      # present in the sandbox.
      uv = {
        lock = "${uvLock}";
        select = uvSelect;
        extras = uvExtras;
        groups = uvGroups;
        editables = uvEditables;
        declarations = uvDeclarations;
        workspace = uvWorkspace;
      };
    };

  # What every planner reads about the target it plans for. The planner runs on
  # one machine and plans for another, so these are values.
  #
  # **The marker environment carries every key of PEP 508, and it has to.**
  # `Marker.evaluate` of `packaging` fills a key that the caller leaves out
  # from the interpreter that is running, so a key that this attrset does not
  # give would describe the machine that plans. `ddrn/PACKAGING-FACTS.md` holds
  # the measurement, and `ddrn_venv.target_from_environ` refuses a short set.
  inherit (pkgs.stdenv) hostPlatform;

  #: The two keys that `platform.release()` and `platform.version()` answer,
  #: which is the kernel of the machine that runs the environment. No build
  #: knows that, so both are empty here. `pyproject-nix` does the same, in
  #: `lib/pep508.nix`, and it gives the same reason. A caller that needs a
  #: marker of either key overrides `markers`.
  targetMarkers = {
    implementation_name = python.implementation;
    implementation_version = python.version;
    os_name = "posix";
    platform_machine = hostPlatform.parsed.cpu.name;
    platform_python_implementation = if python.implementation == "cpython" then "CPython" else "PyPy";
    platform_release = "";
    platform_system = if hostPlatform.isDarwin then "Darwin" else "Linux";
    platform_version = "";
    python_full_version = python.version;
    python_version = pkgs.lib.versions.majorMinor python.version;
    sys_platform = if hostPlatform.isDarwin then "darwin" else "linux";
  };

  targetEnv = {
    DDRN_TARGET = builtins.toJSON {
      pythonVersion = pkgs.lib.versions.majorMinor python.version;
      # The platform of a wheel tag, which PEP 425 spells differently from
      # `platform_machine`.
      platform = "${if hostPlatform.isDarwin then "macosx" else "linux"}_${hostPlatform.parsed.cpu.name}";
      markers = targetMarkers // markers;
    };
  };

  plannerBase = {
    system = pkgs.stdenv.hostPlatform.system;
    builder = "${pkgs.bash}/bin/bash";

    requiredSystemFeatures = [ "builder-rpc-v0" ];

    # The submitted object is a derivation, and every derivation ingests as
    # text.
    __contentAddressed = true;
    outputHashMode = "text";
    outputHashAlgo = "sha256";

    NIX_CONFIG = "experimental-features = nix-command ca-derivations dynamic-derivations";

    DDRN_SYSTEM = pkgs.stdenv.hostPlatform.system;
    DDRN_GRAPH_NIX = "${./graph.nix}";
  }
  // targetEnv;

  plannerScript = path: ''
    set -eu
    export PYTHONPATH=${packagingPath}:${plannerLib}
    # Nix runs a builder with the *basename* of the builder as `argv[0]`, and
    # CPython derives `sys.prefix` from `argv[0]`. A virtual environment
    # therefore has to be entered through its full path.
    exec ${plannerEnvPath}/bin/python ${path}
  '';

  # **The planner of one source distribution, instantiated here and not
  # inside the sandbox.** A planner cannot read a store path that the planner
  # itself added, so `builtins.outputOf` on a derivation that a planner wrote
  # fails with "opening file ...: No such file or directory". Instantiating it
  # out here makes the `.drv` an input source of the outer planner, so the read
  # succeeds. `ddrn/NIX-FACTS.md` holds the measurement.
  sdistPlanner =
    depth: entry:
    derivation (
      plannerBase
      // {
        # A `.drv` name is accepted only for a text-ingested derivation with
        # one output named `out` (`primops.cc:1815`), and the submitted object
        # is a `.drv`.
        name = "${entry.package}-${entry.version}.drv";

        # **The archive is a real dependency of this derivation.** That is the
        # whole point: this planner reads a file that no evaluation can read,
        # and Nix downloads it before the planner runs.
        #
        # An archive of a `path` source needs no download, and the string that
        # `uv.nix` wrote already carries the context of the file. Nix makes
        # that file an input source of this planner in the same way.
        DDRN_SDIST = if entry ? path then entry.path else entry.drv;

        # **The menu is the same for every planner of one level, so it is
        # serialised once for that level and not once for each planner.**
        # `levelJson` is a list, and a list is a memo, so `builtins.toJSON`
        # runs one time for each level. It ran one time for each planner while
        # the row below was part of this document, and the lock file of the
        # benchmark then paid 27.9 MB of JSON for each level.
        # `ddrn/PACKAGING-FACTS.md` holds both measurements.
        DDRN_INPUTS = builtins.elemAt levelJson (depth - 1);

        # **A URL source names where its project lives inside the archive.**
        # `uv.nix` puts `subdirectory` on the artefact, and the planner of this
        # distribution reads the declaration there. An artefact of the index
        # carries no such key.
        #
        # This is the one part that differs between two planners of one level,
        # and it is a few hundred bytes.
        DDRN_SDIST_ROW = builtins.toJSON (
          {
            inherit (entry) package version filename;
          }
          // pkgs.lib.optionalAttrs (entry ? subdirectory) { inherit (entry) subdirectory; }
        );

        args = [
          "-c"
          (plannerScript ./scripts/plan-sdist.py)
        ];
      }
    );

  # **The levels are a list, and not a recursive function, because a list is a
  # memo.** Nix evaluates each element of a list at most once, and it
  # re-evaluates the body of a function at every call. `sdistPlanner` needs the
  # level below it, and there is one planner for each source distribution, so a
  # function costs O(n ^ depth) and this list costs O(n * depth).
  #
  # `ddrn/NIX-FACTS.md` holds the measurement of both, and the lock files that
  # this lab measured hold up to 98 source distributions. The recursive form
  # did not reach such a lock file.
  levels = builtins.genList inputsAt (sdistPlannerDepth + 1);

  # **The same memo, over the text of each level.** Every planner of one level
  # reads the same menu, so the JSON of that menu is one string for the level.
  # A `builtins.toJSON` inside `sdistPlanner` would run one time for each
  # planner instead, over a document of a quarter of a megabyte.
  levelJson = map builtins.toJSON levels;

  # The inputs, with a planner for each source distribution, to `depth`
  # levels. This maps over the lock file and not over `baseInputs.artifacts`,
  # because a planner needs the `url` that `row` removes.
  inputsAt =
    depth:
    baseInputs
    // {
      artifacts = map (
        entry:
        let
          base = row entry;
        in
        if entry.kind == "sdist" && depth > 0 then
          let
            planner = sdistPlanner depth entry;
          in
          base
          // {
            # The same bargain as `row` strikes for a fetch: the `.drv` is an
            # input source, so the file is present and the planner has not run.
            plannerDrv = builtins.unsafeDiscardOutputDependency planner.drvPath;
            plannerOut = builtins.unsafeDiscardStringContext planner.outPath;
          }
        else
          base
      ) entries;
    };

  inputs = builtins.elemAt levels sdistPlannerDepth;
in
derivation (
  plannerBase
  // {
    # The name says what this derivation is. The root that it submits is named
    # `demo-venv`, and the two no longer have to agree.
    name = "venv-planner";

    # **Every store path that the planner names travels in this string.**
    # `builtins.toJSON` keeps the string context of what it serialises, so each
    # `.drv` of the menu, each tool and each script becomes an input source of
    # this derivation. That is what makes them present in the sandbox, and it
    # is what lets `builtins.storePath` and `builtins.appendContext` name them.
    DDRN_INPUTS = builtins.toJSON inputs;

    DDRN_VENV_NAME = venvName;
  }
  // pkgs.lib.optionalAttrs (uvCheck != null) {
    # **Nothing reads this, and that is the whole design.** A store path in an
    # environment variable of a derivation is an input of that derivation, so
    # Nix builds `uvCheck` before it runs this planner, and a refusal of uv
    # stops the build here.
    DDRN_UV_CHECK = "${uvCheck}";
  }
  // {
    args = [
      "-c"
      (plannerScript ./plan.py)
    ];
  }
)
