# This repo's own Python projects, as a pyproject.nix builders overlay.
#
# One definition per project, rendered two ways -- built, and editable for the
# dev shell -- and used as each other's dependencies in both. The two overlays
# below differ only in which renderer they call, so a build input added for
# one is present in the other and they cannot drift.
{
  lib,
  ps,
  python,
  root,
  # The linked Nix version, appended as a PEP 440 local segment to every
  # project here that reaches huggorm-bindings -- see `nixLinked` below.
  version,
}:

let
  projectSource =
    dir:
    ps.projectSource {
      src = root + "/${dir}";
      # A store name holds no `/`, and `dir` may be nested -- see `dirOf`
      # below.
      name = "${lib.replaceStrings [ "/" ] [ "-" ] dir}-source";
    };

  # Every project that reaches huggorm-bindings is built against one specific
  # Nix version's C++ components
  # and is not interchangeable with the same source built against another, so
  # the version says which one. A PEP 440 local segment (`+nix...`), because
  # a plain suffix would read as a newer release of the same package.
  #
  # libpynix, test-support, nanopynix-testing and pytest-agent deliberately
  # don't get it: none of them reaches the bindings, so each
  # really is the same package whatever Nix is linked, and suffixing them
  # would rebuild them once per version for nothing.
  nixLinked = rendered: { version = "${rendered.version}+nix${version}"; };

  # Applied to both the built and the editable form of a project, so a build
  # input added for one is present in the other.
  extraFor = {
    # No `nixLinked`, although this project declares `--file`, `--flake` and
    # `--attr`. It declares those three and reads none of them, so it reaches
    # `huggorm-bindings` through nothing and really is the same package
    # whatever Nix is linked. `pytest-agent` is here for the same reason.
    libpynix = _pySelf: rendered: {
      meta = rendered.meta // {
        platforms = lib.platforms.unix;
      };
    };

    pynix =
      _pySelf: rendered:
      nixLinked rendered
      // {
        meta = rendered.meta // {
          platforms = lib.platforms.unix;
        };
      };

    # `nixLinked`, for the same reason as `pynix`: this project imports pynix,
    # so it reaches the bindings, and a build against one Nix
    # version does not fit another.
    pynix-lsp =
      _pySelf: rendered:
      nixLinked rendered
      // {
        meta = rendered.meta // {
          platforms = lib.platforms.unix;
        };
      };

    pytest-agent = _pySelf: rendered: {
      meta = rendered.meta // {
        platforms = lib.platforms.unix;
      };
    };

    test-support = _pySelf: rendered: {
      meta = rendered.meta // {
        platforms = lib.platforms.unix;
      };
    };

    nanopynix-testing = _pySelf: rendered: {
      meta = rendered.meta // {
        license = lib.licenses.asl20;
        platforms = lib.platforms.unix;
      };
    };
  };

  # Per-project options. No `extras` here on purpose: the renderer's `extras`
  # argument only feeds PEP-508 marker evaluation -- `optional-dependencies`
  # is populated from *every* declared extra regardless. Which extras are
  # actually installed is chosen per environment, in the `mkVirtualEnv` spec,
  # which is where it belongs: the same `pynix` is a release application
  # without its test extra and a dev install with it.
  projects = {
    # The command-line layer, which issue #222 moved out of
    # `pynix/src/pynix/_cli.py`. `easykubenix` had copied the same 359 lines
    # to get a second Nix CLI, and the two diverged in six days. A separate
    # project and not a module of `pynix`: a consumer that takes it should not
    # have to take the evaluator with it.
    libpynix = { };
    pynix = { };
    # The language server, and every dialect it understands. Separate from
    # `pynix` so that `pygls`, `lsprotocol` and `jsonschema` are not
    # dependencies of `pynix build`. Measured before the split: `import pynix`
    # took 0.440 s over 966 modules, and 0.309 s over 904 after it. Issue #107.
    #
    # `pynix` mounts it through an optional import, so this is a dependency of
    # nothing here -- an environment that leaves it out still has a working
    # `pynix`, without the `lsp` subcommand.
    pynix-lsp = { };
    # The helpers that every suite here shares. Editable like the rest, so a
    # change to a helper is live in the dev shell without a rebuild.
    test-support = { };
    # The beartype hook that every suite loads first.
    nanopynix-testing = { };
    # In the set so the dev shell can install it editable -- pytest-agent is
    # developed here alongside everything else. Deliberately *not* in the
    # test runner's venv: it auto-activates on import and would start writing
    # `.pytest-agent/` run directories from CI.
    pytest-agent = { };
  };

  # Where a project's source is, relative to the repository. The attribute
  # name, unless the project said otherwise.
  dirOf = name: opts: opts.dir or name;
in
{
  # The project directories, for `nixpkgsRootsFor`. Derived from `projects`
  # rather than written out again, so adding a project is one edit.
  projectRoots = lib.mapAttrsToList (name: opts: root + "/${dirOf name opts}") projects;

  # The overlay: every project built normally.
  built =
    pySelf: _pyPrev:
    lib.mapAttrs (
      name: opts:
      pySelf.callPackage (ps.mkProject {
        projectRoot = projectSource (dirOf name opts);
        inherit python;
        extra = extraFor.${name} pySelf;
      }) { }
    ) projects;

  /*
    The same projects as editable installs, for the dev shell and the test
    runner. Layered *over* `built` so that anything not listed keeps its
    built form, and so a project's dependencies resolve to the editable copy
    of its siblings rather than a store copy of them. A project marked
    `editable = false` is exactly that case: it stays as `built` supplied it.
  */
  editable =
    pySelf: _pyPrev:
    lib.mapAttrs (
      name: opts:
      pySelf.callPackage (ps.mkEditableProject {
        projectRoot = root + "/${dirOf name opts}";
        # The *project* root, not its `src/` -- `mkDerivationEditable` reads
        # the wheel's own package layout out of pyproject.toml and appends
        # the subdirectory itself. Passing `src/` produced `.../src/src` path
        # hooks that silently pointed nowhere; nothing failed, because
        # pytest.ini's `pythonpath` was quietly supplying the source instead.
        #
        # `toString`, not a path value: a path would be copied into the store
        # and the install would stop being live. That also makes this set
        # local-checkout-only -- under flake evaluation `root` *is* a store
        # path and the renderer rejects it outright -- which is why only the
        # dev shell uses it. See nix/tests.nix.
        root = toString (root + "/${dirOf name opts}");
        inherit python;
        extra = extraFor.${name} pySelf;
      }) { }
    ) (lib.filterAttrs (_: opts: opts.editable or true) projects);
}
