{
  # Where every dependency lives, as directories. nix/sources.nix says how
  # this repository finds nixidae, which holds them.
  sources ? import ./nix/sources.nix,
  system ? builtins.currentSystem,
  # allowUnfree, because nixidae asks for it too. Without it a build started
  # here and a build started from the nixidae root would use two different
  # package sets.
  pkgs ? import sources.nixpkgs {
    inherit system;
    config.allowUnfree = true;
  },
}:
let
  inherit (pkgs) lib;

  pyproject-nix = import sources.pyproject-nix { inherit lib; };

  # huggorm, whose generated bindings are the engine of every scope.
  huggorm = import sources.huggorm { inherit pkgs sources; };

  # Every Python package this repo needs that does *not* depend on
  # huggorm-bindings, added to the interpreter's own package set.
  #
  # The division is the point. Nothing here reaches nix-store/nix-expr or the
  # bindings, so none of it varies by Nix version and all of it is built once
  # rather than once per version. Putting it in the base interpreter rather
  # than in the per-version scope makes that structural instead of a
  # convention someone has to keep: the per-version overlay further down can
  # only usefully add packages that need the bindings, because everything
  # else already resolves before it runs. Adding a version-independent
  # package to that overlay by mistake would rebuild it three times over,
  # and there would be nothing to notice it.
  #
  # `pySelf.callPackage`, not `python3Packages.callPackage`: these must be
  # members of the set that resolves their dependencies, or `kr8s` built
  # against the plain set and `kr8s` seen from this one would be two
  # derivations of one source.
  #
  # Additive, with one exception that says why it is here and when it goes.
  # Every other name is this repo's own or vendored under nix/, so this set
  # forces no rebuild of nixpkgs' own Python packages and leaves the
  # interpreter derivation itself untouched.
  pythonBase = pkgs.python3.override {
    packageOverrides = pySelf: pyPrev: {
      # THE ONE OVERRIDE, and one reason for it now.
      #
      # **The five disabled tests are gone, because nixpkgs disables them
      # itself.** They were the ruff ones: the package runs ruff over the code
      # it generates and compares the result against a checked-in
      # expectation, and a newer ruff writes a blank line after
      # `from __future__ import annotations`. nixpkgs PR #548078 merged on
      # 2026-08-04 as `37fc74a8`, and the nixpkgs this repository pins,
      # `8be7bd0c`, is 6695 commits after it and none behind. Issue #49.
      #
      # Six of its tests start a real HTTP server on loopback, and the
      # Darwin build sandbox refuses the `bind`:
      #
      #   socketserver.py:478: PermissionError: [Errno 1] Operation not permitted
      #
      # 5948 pass and only these six fail, so this is the sandbox saying no
      # rather than the package being broken. `__darwinAllowLocalNetworking`
      # is nixpkgs' own switch for exactly that, and it is ignored on Linux,
      # so it costs nothing there and keeps the six tests running here
      # instead of deleting the coverage.
      #
      # Found making `ekn` build on macOS at all: this package reaches the
      # tree through tree-sitter-config, so those six failures took out the
      # whole toolchain.
      datamodel-code-generator = pyPrev.datamodel-code-generator.overridePythonAttrs (_old: {
        __darwinAllowLocalNetworking = true;
      });

      kr8s = pySelf.callPackage ./nix/kr8s.nix { src = sources.kr8s; };

      deferimport = pySelf.callPackage "${sources.deferimport}/nix/python-package.nix" { };

      tree-sitter-nix = pySelf.callPackage huggorm.lib.tree-sitter-nix {
        # This set, and not `python.pkgs`, which is the set from before
        # these overrides ran. huggorm's nix/tree-sitter-nix.nix gives
        # the measurement.
        pythonPackages = pySelf;
        # `pkgs.path` (the nixpkgs source tree) would otherwise be shadowed
        # by the Python set's own PyPI package literally named "path" --
        # passing `pkgs.path` explicitly sidesteps that entirely.
        nixpkgsPath = pkgs.path;
        # Same shadowing problem: the set's `tree-sitter` is the PyPI
        # bindings package, not pkgs.tree-sitter (the CLI derivation, whose
        # passthru has `buildGrammar`).
        treeSitterCli = pkgs.tree-sitter;
        treeSitterNixSrc = sources.tree-sitter-nix-numtide;
      };
    };
  };

  # Exports OpenTofu's built-in ("core") HCL block schema
  # (resource/data/count/for_each/lifecycle/...) as JSON for a given OpenTofu
  # version, on demand -- see tools/tofu-core-schema/package.nix and
  # pynix-lsp/src/pynix_lsp/_tofu_core_schema.py, which invokes this at LSP-
  # server runtime rather than baking a static snapshot. Independent of any
  # nanopynix/Nix version, so it lives here rather than inside
  # `lanes`.
  tofuCoreSchemaTool = pkgs.callPackage ./tools/tofu-core-schema/package.nix { };

  # Execs a program out of a *relocated* store with that store mounted at its
  # own logical path, the way `nix run` does -- see tools/store-exec/store-exec.c
  # for why this cannot be a binding and has to be a separate exec-final
  # binary. A no-op `execvp` when the store is not relocated, so callers route
  # through it unconditionally. Like tofuCoreSchemaTool it depends on no Nix
  # library, so it lives out here rather than in `lanes`.
  storeExecTool = pkgs.callPackage ./tools/store-exec/package.nix { };

  # **The same tool, as a list that is empty off Linux.** `store-exec.c`
  # rearranges the mount table and the package links `glibc.static`, so
  # `meta.platforms` is `lib.platforms.linux` and that is right. Forcing the
  # derivation on Darwin therefore throws from `check-meta.nix`, and it took
  # the whole test runner with it: the macOS job of #143 refused to evaluate
  # before it built anything.
  #
  # Nix is lazy, so binding `storeExecTool` above costs nothing on Darwin.
  # Only a list position forces it, and every consumer uses one, so the
  # `lib.optional` here is the single place that decides. `storeExecTool`
  # stays exported unchanged, for a consumer outside this repository that
  # already names it.
  storeExecTools = lib.optional pkgs.stdenv.hostPlatform.isLinux storeExecTool;

  # The completion spike: a tiny cyclopts program, the shell code that gives a
  # cyclopts script a dynamic completion, and a pty driver that proves it in
  # fish, bash and zsh. Version-independent, like the two tools above, so it
  # lives out here and not in `lanes`. Its own tests run in
  # its own build -- see nix/completion-spike.nix.
  completionSpike = pythonBase.pkgs.callPackage ./nix/completion-spike.nix { };

  # This repo's seam onto pyproject.nix's builders: `ps` builds package sets
  # (nixvenv's python-set.nix), `mkApp` turns one of their packages into a
  # release application (nix/mk-app.nix).
  inherit ((import sources.nixvenv { inherit pkgs sources; }).lib) ps;
  mkApp = pkgs.callPackage ./nix/mk-app.nix {
    pyprojectUtil = pkgs.callPackage pyproject-nix.build.util { };
  };

  # **The oldest Nix that this repository supports.** huggorm's `lanes`
  # name the versions, and evaluation fails if one is older than this.
  #
  # 2.31 was the version below it, and issue #126 holds the measurement that
  # removed it. On one commit, the `test-local` job skipped 107 tests on 2.31
  # and 13 on 2.35, and it took 12m22s against 7m39s. It was the job with the
  # least signal and the longest run.
  #
  # Raise this number when the next version earns the same measurement. Do not
  # add a version-specific branch to library code to keep an old one alive --
  # `AGENTS.md` gives that rule, and this floor is what makes it affordable.
  supportedNixFloor = "2.34";

  # One scope per huggorm lane: the lane's patched Nix, its collector, its
  # sanitizer and its bindings, and this repository's packages over them.
  # huggorm's `nix/versions.nix` holds every Nix patch, and `mkLane` there
  # applies each variant.
  lanes = lib.mapAttrs (
    name: lane:
    assert lib.assertMsg (lib.versionAtLeast lane.version supportedNixFloor)
      "huggorm's lane ${name} is Nix ${lane.version}, below supportedNixFloor ${supportedNixFloor}.";
    lane.overrideScope (
      final: _prev:
      let
        # `pythonBase` unchanged. Our own projects are pyproject.nix
        # builders packages in `pythonSet` below, and the one per-version
        # Python package, huggorm-bindings, goes straight into the builders
        # set as a lifted root.
        python = pythonBase;
      in
      {
        # The bindings and their generated surface, built with this
        # repository's interpreter set.
        python3Packages = python.pkgs;

        # Everything above the bindings is a pyproject.nix builders
        # package. The set is built once per Nix version and holds both
        # the built and the editable form of each project.
        # No sanitizer: nothing in these pure-Python
        # builds loads the instrumented extension, and preloading the
        # TSAN runtime here only instrumented `uv` -- see the comment
        # on the `nanopynix` override in that file.
        pyPackages = pkgs.callPackage ./nix/py-packages.nix {
          inherit
            ps
            python
            ;
          root = ./.;
          # The linked Nix version, so two builds of the same source
          # against different Nix components are distinguishable.
          inherit (final) version;
        };

        /*
          This repo's Python closure, optionally widened by a consumer's
          own projects.

          The parameters exist for a consumer that builds one of *its
          own* pyproject.toml projects against this closure --
          easykubenix does, for its `ekn` CLI. Adding the project to the
          overlay is not enough on its own: a set's nixpkgs packages are
          lifted once, from the roots `mkPythonSet` is seeded with, and
          the lifting machinery is internal to nixvenv's python-set.nix. So a
          dependency that only the consumer's project declares (`kr8s`,
          for `ekn`) has no way into the set after the fact -- an
          `overrideScope` can add the project but not the closure it
          needs. Passing `projectRoots` here reads that project's
          pyproject.toml alongside ours and resolves its dependencies
          the same way, which is the only place that can happen.

          Type: pythonSetWith :: AttrSet -> AttrSet
        */
        pythonSetWith =
          {
            # Consumer pyproject.toml directories, read for their
            # third-party dependencies exactly as ours are. Their own
            # names are excluded from the nixpkgs lookup automatically
            # (see `nixpkgsRootsFor`), since `overlay` supplies them.
            projectRoots ? [ ],
            # The consumer's own projects, as a standard overlay.
            # Composed *over* ours, so it can also replace one of them.
            overlay ? (_final: _prev: { }),
          }:
          ps.mkPythonSet {
            inherit python;
            # Sourced from nixpkgs: the build systems, plus the whole
            # third-party runtime closure, plus our own native extension.
            # `python.pkgs` already resolved every one of those names, so
            # the roots are just the propagated inputs nixpkgs computed --
            # no second hand-written dependency list to fall out of date.
            # `huggorm` is the library over the bindings, for a consumer
            # that moves off nanopynix one module at a time.
            # `huggorm-generated` carries the bindings' stubs, for a type
            # gate's venv, and `argparse-manpage` renders an application's
            # manual pages (nix/mk-app.nix).
            nixpkgsRoots = [
              final.huggorm-bindings
              final.huggorm
              final.huggorm-generated
              python.pkgs.argparse-manpage
            ]
            ++ ps.nixpkgsRootsFor {
              inherit python;
              # `completion-spike` is not one of `pyPackages`: it is a
              # nixpkgs `buildPythonApplication`, and it runs its own
              # tests in its own build. Its *declarations* are read here
              # anyway, so that `cyclopts` and `pexpect` reach this set
              # and the type gate can see the tree. Reading the
              # pyproject.toml is all `nixpkgsRootsFor` does, so this
              # adds no second package.
              projectRoots = final.pyPackages.projectRoots ++ [ ./completion-spike ] ++ projectRoots;
              # nixpkgs Python packages, but this scope's own -- lifted
              # in as roots above rather than looked up by name.
              exclude = [
                "huggorm-bindings"
                "huggorm"
                "huggorm-generated"
                "argparse-manpage"
              ];
            };
            overlay = lib.composeExtensions final.pyPackages.built overlay;
          };

        pythonSet = final.pythonSetWith { };

        # The same set with our projects swapped for editable installs.
        # `mkVirtualEnv` from here gives a venv whose site-packages
        # points back at this checkout.
        editablePythonSet = final.pythonSet.overrideScope final.pyPackages.editable;

        inherit (final.pythonSet)
          # The command-line layer that issue #222 moved out of
          # `pynix`. Exported so that a second Nix CLI in Python can
          # take it instead of copying it, which is the whole reason it
          # is a project of its own.
          libpynix
          # The pytest plugin, developed here alongside everything else.
          # Exported because a consumer's test suite may want it too --
          # see the note on the outer `inherit` for the one way to take
          # it that actually works.
          pytest-agent
          ;

        # The suite runner that CI executes, one per Nix version.
        testRunner = final.callPackage ./nix/tests.nix {
          inherit (final.pythonSet.pynix) version;
          inherit (sources) nixpkgs;
          inherit (final) sanitizer;
          inherit (final) pythonSet;
          # The one list that the dev shell also takes, so a tool the
          # suite needs cannot reach only one of them. See
          # nix/suite-runtime.nix.
          inherit (final) suiteRuntime;
        };

        pynix = mkApp {
          name = "pynix";
          inherit (final) pythonSet;
          # `pynix develop` resolves `store-exec` off PATH
          # (`pynix._store_layout`). The prefix runs a program out of a
          # store that is relocated, which is every store that pynix
          # opens away from the root one.
          #
          # `tofuCoreSchemaTool` was here as well until issue #107. It
          # belongs to the language server, so it is on the PATH of the
          # `pynix-lsp` application below.
          pathInputs = storeExecTools;
          completions = true;
          # Manual pages, rendered from the parser at build time. Issue #233.
          manPages = true;
          manExamples = ./pynix/man/examples.roff;
          skill = ./pynix/skill/pynix;
        };
        # The language server, as a release application of its own.
        # Issue #107 split it out of `pynix`, so that `pygls`,
        # `lsprotocol` and `jsonschema` are not in the closure of
        # `pynix build`. `pynix` is still a dependency of it, because the
        # server imports `pynix._nix_syntax` and `pynix._completion`.
        #
        # `tofuCoreSchemaTool` is here because
        # `pynix_lsp._tofu_core_schema` runs it at request time, rather
        # than reading a snapshot that this repository stores.
        # `storeExecTools` is here for the same reason it is on `pynix`:
        # the terranix dialect runs `tofu` out of the store that the
        # server evaluates against.
        pynix-lsp = mkApp {
          name = "pynix-lsp";
          inherit (final) pythonSet;
          pathInputs = [
            tofuCoreSchemaTool
          ]
          ++ storeExecTools;
        };
        shell = final.callPackage ./nix/shell.nix {
          pythonSet = final.editablePythonSet;
        };
        nonEditableShell = final.callPackage ./nix/shell.nix {
          inherit (final) pythonSet;
        };
        # A live, editable-install `pynix`/`ekn` env (no devtools --
        # see nix/shell.nix for the full interactive nanopynix shell),
        # exported so other repos can drop a hot-reloading `pynix`
        # into their own devShell/direnv without rebuilding on every
        # edit here. See nix/virtual-env.nix's own docstring for why no
        # env var is needed.
        pynixDevEnv = final.callPackage ./nix/virtual-env.nix {
          pythonSet = final.editablePythonSet;
        };
        pynixNonEditableDevEnv = final.callPackage ./nix/virtual-env.nix {
          inherit (final) pythonSet;
        };
        nanopynix-docs = final.callPackage ./nix/docs.nix { };
        # An attrset of derivations, not one derivation, so a failing
        # run names the gate. `flake.nix` puts it under `checks`; the
        # `packages` filter drops it, which is what we want.
        checks = final.callPackage ./nix/checks.nix {
          inherit completionSpike;
        };

        # What the suite needs on PATH, shared by the packaged runner and
        # the dev shell so the two cannot drift again. The file says which
        # drifts it already cost.
        suiteRuntime = final.callPackage ./nix/suite-runtime.nix {
          inherit tofuCoreSchemaTool storeExecTools;
          inherit (final.nixComponents) nix-cli;
        };
      }
    )
  ) huggorm.lanes;

  nanopynixVersions = lanes // {
    stable = lanes.nix_2_34;
    latest = lanes.nix_2_35;
  };

  # Per-version test runners, exposed individually as `nanopynix-tests-<name>`
  # flake packages so CI can build/run each Nix version in its own job.
  tests = lib.mapAttrs' (
    name: value: lib.nameValuePair "nanopynix-tests-${name}" value.testRunner
  ) lanes;

  # Every suffix that huggorm's `lanes` give a variant scope.
  #
  # **A suffix that is missing here does not fail.** It quietly puts a slow,
  # uncovered build into the regular per-commit matrix, because "not a variant"
  # is the default everywhere. `unlistedVariants` below turns that silence into
  # a build failure.
  variantSuffixes = [
    "-tsan"
    "-ubsan"
    "-asan"
    "-nogc"
  ];

  # The check that makes a forgotten suffix a build failure.
  #
  # huggorm's `lanes` carry a suffix for each variant axis, and every consumer
  # of these names sorts by that suffix. A new axis that nobody adds to
  # `ci/variants.nix` reads as a regular version everywhere, so it joins the
  # per-commit matrix as a slow build that collects no coverage. Nothing else
  # notices, because "not a variant" is the default.
  unlistedVariants = builtins.filter (
    name: builtins.match "^(nix_[0-9_]+|git|determinate)$" name == null && !hasKnownSuffix name
  ) (builtins.attrNames lanes);
  hasKnownSuffix = name: lib.any (suffix: lib.hasSuffix suffix name) variantSuffixes;

  # The version names of `tests`, grouped by variant, with the bare names under
  # `regular`. `ci/workflows/lib.nix` builds one job per entry, and
  # `ci/steps.nix` embeds the whole thing so that the scheduled workflow can
  # write its matrices with one `echo` rather than five `nix eval` calls.
  #
  # **Do not drop a version from a group to save CI minutes.** The settings and
  # store models carry 32 `nix_version_min`/`nix_version_removed` fields, and
  # the drift check is what proves each gate is set correctly: a field the
  # running Nix does not have shows up as `extra`, and a gate that hides a
  # field the running Nix does have shows up as `missing`. Neither can be seen
  # from one version. Dropping a version deletes that coverage in silence,
  # because the remaining jobs stay green.
  #
  # **Issue #126 dropped 2.31 anyway, and this is what it cost.** The gate
  # refused 31 of the 32 fields on 2.31, 15 on 2.34 and 1 on 2.35, so 2.31 was
  # the only job that saw a `since("2.34")` field refused. Those gates are now
  # true on every supported version, so the drift check never exercises them.
  # They are not dead -- a consumer can link an older Nix by hand -- but
  # nothing here proves them any more.
  #
  # That was a considered trade, and the measurement that bought it is in
  # `supportedNixFloor` above. The rule still holds for 2.34: it is the only
  # version that refuses a `since("2.35")` field, and dropping it would repeat
  # the loss with nothing left to catch it.
  ciVersionMatrix =
    let
      names = map (lib.removePrefix "nanopynix-tests-") (builtins.attrNames tests);
    in
    {
      regular = builtins.filter (name: !hasKnownSuffix name) names;
    }
    // builtins.listToAttrs (
      map (suffix: {
        name = lib.removePrefix "-" suffix;
        value = builtins.filter (lib.hasSuffix suffix) names;
      }) variantSuffixes
    );

  # The CI experiments. `ci/experiments.nix` gives the reason each one is a
  # package rather than a script in a workflow file.
  experiments = import ./ci/experiments.nix { inherit pkgs tests; };

  # Every body that a GitHub Actions step used to carry inline. `ci/steps.nix`
  # gives the reason each one is a package.
  ciSteps = import ./ci/steps.nix {
    inherit
      pkgs
      ciVersionMatrix
      variantSuffixes
      ;
    inherit tests;
  };

in
lib.throwIf (unlistedVariants != [ ])
  ''
    default.nix: these variant scopes carry a suffix that ci/variants.nix does
    not list, so every consumer reads them as regular Nix versions and CI runs
    them in the per-commit matrix with no coverage:
      ${builtins.concatStringsSep "\n    " unlistedVariants}
    Add the suffix to ci/variants.nix.
  ''
  {
    packages = {
      # `pytest-agent` is built in this repository's `pythonSet`. A
      # consumer venv names it in its own spec (`pytest-agent = [ ];`)
      # instead: a package of one builders set does not resolve in another.
      inherit (nanopynixVersions.stable)
        pynix
        pynix-lsp
        libpynix
        pytest-agent
        huggorm-bindings
        ;
      inherit tofuCoreSchemaTool storeExecTool;
      default = nanopynixVersions.stable.pynix;
    };
    lib = {
      # `pythonSetWith` seeds the set from the consumer's pyproject.toml too,
      # the only point at which a nixpkgs package can enter.
      inherit (nanopynixVersions.stable) pythonSet pythonSetWith;
      # Its `caBundle` wrapper lets a program that initialises OpenSSL at
      # import start in a build sandbox, which has no trust store. Issue #62.
      inherit mkApp;
    };
    nixosModules = { };
    easykubenixModules = { };
    docs.default = nanopynixVersions.stable.nanopynix-docs;

    # Not public: the names may change without notice.
    internal = {
      inherit
        sources
        pkgs
        nanopynixVersions
        pyproject-nix
        tests
        experiments
        ciSteps
        ciVersionMatrix
        storeExecTools
        ;
      inherit (nanopynixVersions.stable)
        pynixDevEnv
        pynixNonEditableDevEnv
        nonEditableShell
        ;
    };

    inherit (nanopynixVersions.stable) shell checks;

    # The workflows that run the tests. nixidae renders each one to
    # `.github/workflows/nanopynix-<name>.yml`. They read this repository
    # through `repo`, built from the same arguments, so they see the sources
    # nixidae passed in.
    ciTestWorkflows =
      let
        repo = import ./. { inherit sources system pkgs; };
      in
      {
        commit = import ./ci/workflows/commit.nix { inherit repo; };
        schedule = import ./ci/workflows/schedule.nix { inherit repo; };
        experiment = import ./ci/workflows/experiment.nix { inherit repo; };
      };

    # One scope per huggorm lane, plus `stable` and `latest`, which this file
    # names. Every huggorm dependant in nixidae keys its own `lanes` on these
    # names (nixidae/nixidae#1108). `nanopynixVersions` is the same set.
    lanes = nanopynixVersions;
  }
