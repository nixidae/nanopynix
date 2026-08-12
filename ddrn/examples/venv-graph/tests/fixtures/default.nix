# The artefacts of the sealed test, built rather than fetched.
#
# **The example proper takes every artefact from PyPI, so a test over it needs
# a network.** These fixtures are derivations, so the sealed test needs none:
# each artefact is built from a source tree of this directory, or from a
# package that nixpkgs already provides.
#
# What the fixtures cover, and what they do not:
#
#   covered      the bootstrap of the installer, a wheel install, a console
#                script, a source distribution whose backend the planner has
#                to read out of the archive, a chain of four such
#                distributions, a PEP 660 editable, and the composition of the
#                environment.
#   not covered  a binary wheel and `auto-patchelf`, and the choice between
#                two wheels whose tags differ. `run.sh` covers both, over the
#                real lock file, and it needs a network.
{ pkgs }:

let
  inherit (pkgs) lib;

  python = pkgs.python3;

  # One builder for both scripts. `SOURCE_DATE_EPOCH` is here for the same
  # reason `graph.nix` sets it: a `.pyc` or a zip entry that records the time
  # of the build makes the output different on every run.
  run =
    script: name: args:
    derivation (
      {
        inherit name;
        system = pkgs.stdenv.hostPlatform.system;
        builder = "${pkgs.bash}/bin/bash";
        PATH = "${pkgs.coreutils}/bin";
        SOURCE_DATE_EPOCH = "315532800";
        args = [
          "-c"
          ''exec "${python}/bin/python3" "${script}"''
        ];
      }
      // args
    );

  #: A distribution name, as PEP 427 spells it inside a file name.
  escape = name: builtins.replaceStrings [ "-" ] [ "_" ] name;

  mkWheel =
    {
      pname,
      version,
      src,
      entryPoints ? [ ],
    }:
    run ./mk-wheel.py "${escape pname}-${version}-py3-none-any.whl" {
      inherit src;
      distName = pname;
      distVersion = version;
      entryPoints = lib.concatStringsSep "\n" entryPoints;
    };

  mkSdist =
    {
      pname,
      version,
      src,
    }:
    run ./mk-sdist.py "${escape pname}-${version}.tar.gz" {
      inherit src;
      distName = pname;
      distVersion = version;
    };

  # **`installer` comes from nixpkgs, and it has to become a wheel again.**
  # The graph bootstraps by unzipping the wheel of the installer, because
  # nothing can install the installer. nixpkgs ships the installed tree rather
  # than the wheel, so this repacks that tree. The version is what nixpkgs
  # gives, so the fixture does not pin a second one.
  installerWheel = mkWheel {
    pname = "installer";
    inherit (pkgs.python3Packages.installer) version;
    src = "${pkgs.python3Packages.installer}/${python.sitePackages}";
  };

  demoBackend = mkWheel {
    pname = "demo-backend";
    version = "1.0.0";
    src = ./demo-backend;
  };

  demoLib = mkWheel {
    pname = "demo-lib";
    version = "2.1.0";
    src = ./demo-lib;
    entryPoints = [ "demo-lib=demolib:main" ];
  };

  demoTool = mkSdist {
    pname = "demo-tool";
    version = "3.0.0";
    src = ./demo-tool;
  };

  # **A project that holds C code, for the `buildTools` argument.**
  # `../../default.nix` gives every node `coreutils`, `unzip`, `tar`, `gzip`
  # and the interpreter, and no compiler. A build of this archive therefore
  # fails until the caller names one, and `../sealed.nix` builds it both ways.
  nativeSdist = mkSdist {
    pname = "demo-native";
    version = "1.0.0";
    src = ./demo-native;
  };

  # **An archive that holds two projects, for the URL source of `uv.lock`.**
  # uv writes `source = { url = "…", subdirectory = "backend" }` when the
  # archive that a URL names holds the project below its top. The archive of
  # `pypa/hatch` is that case, and `tests/fixtures/locks/url-subdirectory.lock`
  # records it.
  #
  # The top of this archive declares a backend that no artefact supplies, so a
  # reader that ignored the subdirectory fails and says which backend it looked
  # for. `./nested-app/pyproject.toml` is the same decoy for the tree route.
  #
  # **This project also carries its own backend, the way `hatchling` does.**
  # PEP 517 calls that `backend-path`, and `requires` is then empty, because
  # nothing has to be resolved at all. So the two nested fixtures cover the two
  # halves of the real case: this one takes its backend out of the archive, and
  # `./nested-app` takes one out of the menu.
  nestedSdist = mkSdist {
    pname = "demo-nested";
    version = "1.0.0";
    src =
      pkgs.runCommand "demo-nested-source"
        {
          outerText = ''
            [build-system]
            requires = []
            build-backend = "the_backend_of_the_wrong_project"

            [project]
            name = "demo-nested-outer"
            version = "0.0.0"
          '';
          innerText = ''
            [build-system]
            requires = []
            build-backend = "demoback"
            backend-path = ["support"]

            [project]
            name = "demo-nested"
            version = "1.0.0"
          '';
          moduleText = ''
            """The project below the top of an archive that holds two."""

            from __future__ import annotations


            def greet() -> str:
                return "hello from the project below the top of its archive"
          '';
        }
        ''
          mkdir -p "$out/backend/src/demonested" "$out/backend/support"
          printf '%s' "$outerText" > "$out/pyproject.toml"
          printf '%s' "$innerText" > "$out/backend/pyproject.toml"
          printf '%s' "$moduleText" > "$out/backend/src/demonested/__init__.py"
          cp ${chainModule} "$out/backend/support/demoback.py"
        '';
  };

  # **A chain of source distributions, for the bound that `default.nix` sets.**
  # Each link declares the next one as its build backend, so building any link
  # needs a planner for the link below it. `sdistPlannerDepth` says how many
  # such planners a graph may hold, and this chain is one longer than that
  # bound, so one root reaches the bound and another passes it.
  #
  # Every link carries the same module, which is `demoback`. It reads
  # `pyproject.toml` of the directory that it runs in, so one file serves as
  # the backend of each link and as the code that each link installs.
  chainModule = ./demo-backend/demoback.py;

  chainSdist =
    {
      pname,
      module,
      requires,
      backend,
    }:
    mkSdist {
      inherit pname;
      version = "1.0.0";
      src =
        pkgs.runCommand "${pname}-source"
          {
            pyprojectText = ''
              [build-system]
              requires = ["${requires}"]
              build-backend = "${backend}"

              [project]
              name = "${pname}"
              version = "1.0.0"
            '';
          }
          ''
            mkdir -p "$out/src/${module}"
            cp ${chainModule} "$out/src/${module}/__init__.py"
            printf '%s' "$pyprojectText" > "$out/pyproject.toml"
          '';
    };

  # The end of the chain, and the only link that is a wheel. A wheel needs no
  # planner, so the chain stops here rather than run on for ever.
  chainEnd = mkWheel {
    pname = "link-d";
    version = "1.0.0";
    src = pkgs.runCommand "link-d-source" { } ''
      mkdir -p "$out/linkd"
      cp ${chainModule} "$out/linkd/__init__.py"
    '';
  };

  # `link-0` is the extra link, and it is the one that passes the bound. The
  # two roots of `../sealed.nix` differ in nothing else.
  chainLinks = [
    {
      pname = "link-0";
      module = "link0";
      requires = "link-a";
      backend = "linka";
    }
    {
      pname = "link-a";
      module = "linka";
      requires = "link-b";
      backend = "linkb";
    }
    {
      pname = "link-b";
      module = "linkb";
      requires = "link-c";
      backend = "linkc";
    }
    {
      pname = "link-c";
      module = "linkc";
      requires = "link-d";
      backend = "linkd";
    }
  ];

  chainArtifacts = [
    {
      package = "installer";
      inherit (pkgs.python3Packages.installer) version;
      filename = installerWheel.name;
      kind = "wheel";
      drv = installerWheel;
    }
    {
      package = "link-d";
      version = "1.0.0";
      filename = chainEnd.name;
      kind = "wheel";
      drv = chainEnd;
    }
  ]
  ++ map (
    link:
    let
      sdist = chainSdist link;
    in
    {
      package = link.pname;
      version = "1.0.0";
      filename = sdist.name;
      kind = "sdist";
      drv = sdist;
    }
  ) chainLinks;

  # Each row is what `default.nix` of the example calls an artefact: the
  # metadata that a planner reads, and the derivation that produces the file.
  artifacts = [
    {
      package = "installer";
      inherit (pkgs.python3Packages.installer) version;
      filename = installerWheel.name;
      kind = "wheel";
      drv = installerWheel;
    }
    {
      package = "demo-backend";
      version = "1.0.0";
      filename = demoBackend.name;
      kind = "wheel";
      drv = demoBackend;
    }
    {
      package = "demo-lib";
      version = "2.1.0";
      filename = demoLib.name;
      kind = "wheel";
      drv = demoLib;
    }
    {
      package = "demo-tool";
      version = "3.0.0";
      filename = demoTool.name;
      kind = "sdist";
      drv = demoTool;
    }
  ];

  # The menu of the graph that compiles. It holds the installer, the backend
  # that every fixture builds through, and the one archive that ships C code.
  nativeArtifacts = [
    {
      package = "installer";
      inherit (pkgs.python3Packages.installer) version;
      filename = installerWheel.name;
      kind = "wheel";
      drv = installerWheel;
    }
    {
      package = "demo-backend";
      version = "1.0.0";
      filename = demoBackend.name;
      kind = "wheel";
      drv = demoBackend;
    }
    {
      package = "demo-native";
      version = "1.0.0";
      filename = nativeSdist.name;
      kind = "sdist";
      drv = nativeSdist;
    }
  ];

  # The menu of the graph that covers the two sources which name a place. It is
  # `artifacts` plus the nested archive, and `../sealed.nix` gives that graph a
  # nested tree as well.
  nestedArtifacts = artifacts ++ [
    {
      package = "demo-nested";
      version = "1.0.0";
      filename = nestedSdist.name;
      kind = "sdist";
      drv = nestedSdist;
      # **This is the one key that a URL source adds.** `default.nix` puts it
      # on the inputs of the planner of this distribution, and that planner
      # reads the declaration below it and builds there.
      subdirectory = "backend";
    }
  ];
in
{
  inherit
    mkWheel
    mkSdist
    artifacts
    chainArtifacts
    nativeArtifacts
    nestedArtifacts
    installerWheel
    ;
}
