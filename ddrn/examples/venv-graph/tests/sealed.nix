# The whole example, over artefacts that this repository builds.
#
# **`ddrn/examples/venv-graph/run.sh` proves the example, and it needs a
# network.** Every artefact of `lock.json` comes from PyPI, so that proof
# cannot be a check of this repository. This file is the same graph over the
# fixtures of `./fixtures`, which are derivations, so it needs no index.
#
# What it exercises, and what only `run.sh` exercises:
#
#   here       the bootstrap of the installer by unpacking, a wheel install
#              with a console script, a source distribution whose backend the
#              planner reads out of the archive, a PEP 660 editable, the same
#              local project shipped as its own source distribution instead,
#              a project below the top of its tree and one below the top of its
#              archive, a chain of source distributions at both edges of
#              `sdistPlannerDepth`, the composition of the environment, and the
#              derivation that carries `meta` of an application.
#   run.sh     a binary wheel and `auto-patchelf`, and the choice between two
#              wheels whose tags compete.
#
# **The graph itself makes no impure call, and the two arguments decide the
# rest.** `builtins.storePath` runs only for a `nanopynixEnv` that arrives as
# a string, so a caller that passes the derivation needs no `--impure` for
# that. The two defaults are the impure part: `<nixpkgs>` is a lookup, and
# `test_sealed_graph.py` passes a string because it takes the environment from
# a separate build. `ddrn/NIX-FACTS.md` records the classification that makes
# each of those impure.
{
  pkgs ? import <nixpkgs> { },
  # A Python environment that has nanopynix, built against the patched Nix.
  # A derivation keeps this evaluation pure, and a string does not.
  nanopynixEnv,
  # The tree that the editable install reads. The check builds twice, with two
  # trees, and the environment stays one store path.
  editableTree ? ./fixtures/demo-app,
}:

let
  fixtures = import ./fixtures { inherit pkgs; };

  # The same graph twice, and one attribute of one entry is the difference.
  # `mode` says which form the local project installs in, and `default.nix`
  # gives the two values.
  graph =
    { name, mode }:
    import ../. {
      inherit pkgs nanopynixEnv;
      inherit (fixtures) artifacts;
      roots = [
        "demo-lib"
        "demo-tool"
      ];
      installer = "installer";
      venvName = name;
      editables = {
        demo-app = {
          inherit mode;
          src = ./fixtures/demo-app;
          root = "$DDRN_EDITABLE_ROOT";
        };
      };
    };

  planner = graph {
    name = "sealed-venv";
    mode = "editable";
  };

  venv = builtins.outputOf planner.outPath "out";

  # **The release form of the same project.** `build_sdist` writes the archive
  # that an index would serve, and a second node builds a wheel out of it. So
  # this environment holds the code, and the tree that `$DDRN_EDITABLE_ROOT`
  # names answers for nothing.
  sdistPlanner = graph {
    name = "sealed-sdist-venv";
    mode = "sdist";
  };

  sdistVenv = builtins.outputOf sdistPlanner.outPath "out";

  # Run the environment inside the store that holds it. `make-venv.py` writes
  # an absolute symlink for each entry, and a chroot store puts the store under
  # a prefix, so those symlinks resolve only from inside a build of that store.
  check = derivation {
    name = "sealed-venv-check";
    system = pkgs.stdenv.hostPlatform.system;
    builder = "${pkgs.bash}/bin/bash";

    DDRN_EDITABLE_ROOT = "${editableTree}";

    args = [
      "-c"
      ''
        set -eu
        {
          # **The environment is one store path, and this line names it.** The
          # test builds this derivation twice, with two trees, and compares
          # the two lines. A reference scan of the output would not answer the
          # question, because a report that names no store path has no
          # reference to scan.
          echo "environment"
          printf '  %s\n' "${venv}"

          # A console script exists only if the installer that the planner
          # bootstrapped wrote it. An unpacked wheel carries no such file.
          echo "console script"
          printf '  %s\n' "$("${venv}/bin/demo-lib")"

          # `demo-tool` is a source distribution, and the backend that builds
          # it is another artefact of the same list. The line below exists only
          # if the planner read `pyproject.toml` out of the archive and
          # registered that backend as a node.
          echo "from a source distribution"
          printf '  %s\n' "$("${venv}/bin/python" -c 'import demotool; demotool.main()')"

          # The editable reads the tree that this derivation named, and the
          # environment carries no copy of it.
          echo "editable"
          printf '  %s\n' "$("${venv}/bin/python" -c 'import demoapp; print(demoapp.greet())')"
        } > "$out"
      ''
    ];
  };

  # **The application, and the derivation that carries its `meta`.**
  # `../application.nix` is the file under test, and `uv.nix` is the other
  # caller of it. `test_projects.py` covers that caller over a lock file, which
  # needs a network. This one needs none.
  appPlanner = import ../. {
    inherit pkgs nanopynixEnv;
    inherit (fixtures) artifacts;
    roots = [ "demo-lib" ];
    installer = "installer";
    venvName = "sealed-app";
    application = "demo-lib";
    editables = { };
  };

  applicationOf =
    program:
    import ../application.nix {
      inherit pkgs program;
      application = builtins.outputOf appPlanner.outPath "out";
      name = "sealed-app";
    };

  app = applicationOf "demo-lib";

  # **The build verifies the program rather than trust the name.** `demo-lib`
  # installs one console script, and this names another one.
  appWrongProgram = applicationOf "not-a-program";

  # **The two sources of `uv.lock` that name a place, in one graph.** Neither
  # one is a registry and neither one is a directory of the workspace, and both
  # may put the project below the top of what they name:
  #
  #   git   a checkout. `uv.nix` fetches it during evaluation and gives it as a
  #         tree, so one PEP 517 `build_wheel` call over that tree is the whole
  #         build. `nested-app` stands for it.
  #   url   an archive. `uv.nix` joins the address of the source to the hash of
  #         the `sdist` table, so what arrives is an ordinary source
  #         distribution that carries a subdirectory. `demo-nested` stands for
  #         it, and its build is still two nodes, because a planner has to read
  #         the declaration out of the archive first.
  #
  # **Each of the two carries a decoy above the project.** The top of the tree
  # and the top of the archive both declare a backend that no artefact of this
  # menu supplies, so a build that ignored the subdirectory fails and names the
  # backend that it looked for.
  #
  # **The two also cover the two ways that a backend arrives.** `nested-app`
  # asks for `demo-backend`, which the menu holds. `demo-nested` carries its
  # own, with the `backend-path` of PEP 517 and an empty `requires`, which is
  # what `hatchling` declares.
  nestedPlanner = import ../. {
    inherit pkgs nanopynixEnv;
    artifacts = fixtures.nestedArtifacts;
    roots = [ "demo-nested" ];
    installer = "installer";
    venvName = "sealed-nested-venv";
    editables = {
      nested-app = {
        src = ./fixtures/nested-app;
        mode = "wheel";
        subdirectory = "backend";
      };
    };
  };

  nestedVenv = builtins.outputOf nestedPlanner.outPath "out";

  nestedCheck = derivation {
    name = "sealed-nested-check";
    system = pkgs.stdenv.hostPlatform.system;
    builder = "${pkgs.bash}/bin/bash";

    args = [
      "-c"
      ''
        set -eu
        {
          echo "environment"
          printf '  %s\n' "${nestedVenv}"

          # The tree route. The line exists only if the node ran the backend in
          # `backend/`, because the top of the tree declares another one.
          echo "from a tree, below its top"
          printf '  %s\n' "$("${nestedVenv}/bin/python" -c 'import nestedapp; print(nestedapp.greet())')"

          # **The install holds the code, and that is the whole difference from
          # an editable.** No variable names a tree for this environment.
          echo "loaded from"
          printf '  %s\n' "$("${nestedVenv}/bin/python" -c 'import nestedapp; print(nestedapp.__file__)')"

          # The archive route, through the planner that read the declaration
          # under `backend/` out of the archive.
          echo "from an archive, below its top"
          printf '  %s\n' "$("${nestedVenv}/bin/python" -c 'import demonested; print(demonested.greet())')"
        } > "$out"
      ''
    ];
  };

  # **A chain of source distributions, and the bound that `default.nix` sets.**
  # Each link of `fixtures.chainArtifacts` declares the next one as its build
  # backend, so a build of one link needs a planner for the link below it.
  # `sdistPlannerDepth` says how many such planners a graph may hold, and the
  # chain is one link longer than that bound.
  #
  # The two roots below differ in nothing but the name, and one of them builds:
  #
  #   link-a  three planners, which is the bound. It builds.
  #   link-0  four planners, which passes the bound. `graph.nix` throws, and
  #           the message names the constant.
  chain =
    root:
    import ../. {
      inherit pkgs nanopynixEnv;
      artifacts = fixtures.chainArtifacts;
      roots = [ root ];
      installer = "installer";
      venvName = "${root}-venv";
      editables = { };
    };

  chainPlanner = chain "link-a";
  chainVenv = builtins.outputOf chainPlanner.outPath "out";

  # **Realising this is what fails, and building the planner is not.** The
  # outer planner registers the graph and submits it, and nothing there reads
  # past the first level. The throw arrives when the third planner evaluates
  # `graph.nix` and asks for a fourth.
  tooDeepPlanner = chain "link-0";
  tooDeepVenv = builtins.outputOf tooDeepPlanner.outPath "out";

  chainCheck = derivation {
    name = "sealed-chain-check";
    system = pkgs.stdenv.hostPlatform.system;
    builder = "${pkgs.bash}/bin/bash";

    args = [
      "-c"
      ''
        set -eu
        {
          echo "environment"
          printf '  %s\n' "${chainVenv}"

          # `link-a` exists only because three planners ran, one for each of
          # `link-a`, `link-b` and `link-c`. The wheel `link-d` ends the chain.
          echo "the chain built"
          printf '  %s\n' "$("${chainVenv}/bin/python" -c 'import linka; print(linka.__file__)')"
        } > "$out"
      ''
    ];
  };

  # The same report, for the environment that ships `demo-app` rather than
  # linking it. The two reports differ in one line: where the module came from.
  sdistCheck = derivation {
    name = "sealed-sdist-check";
    system = pkgs.stdenv.hostPlatform.system;
    builder = "${pkgs.bash}/bin/bash";

    # **This variable must reach nothing.** An editable install of the same
    # project reads the tree that it names. A release holds its own code, so
    # the line below has to name a path inside the environment.
    DDRN_EDITABLE_ROOT = "${editableTree}";

    args = [
      "-c"
      ''
        set -eu
        {
          echo "environment"
          printf '  %s\n' "${sdistVenv}"

          echo "from its own source distribution"
          printf '  %s\n' "$("${sdistVenv}/bin/python" -c 'import demoapp; print(demoapp.greet())')"

          # The archive holds the code, so this path is inside the environment.
          # The editable check prints a path under `$DDRN_EDITABLE_ROOT` here.
          echo "loaded from"
          printf '  %s\n' "$("${sdistVenv}/bin/python" -c 'import demoapp; print(demoapp.__file__)')"
        } > "$out"
      ''
    ];
  };
in
{
  inherit
    fixtures
    planner
    venv
    check
    sdistPlanner
    sdistVenv
    sdistCheck
    nestedPlanner
    nestedVenv
    nestedCheck
    chainPlanner
    chainVenv
    chainCheck
    tooDeepPlanner
    tooDeepVenv
    appPlanner
    app
    appWrongProgram
    ;
}
