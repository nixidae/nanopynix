# A test workflow. nixidae renders it to
# `.github/workflows/nanopynix-schedule.yml`, with nanopynix as the working
# directory of every step. nixidae/nixidae#4.
{
  repo ? import ../../. { },
}:
let
  workflow = import ./lib.nix { inherit repo; };
  inherit (workflow)
    steps
    caps
    mkJob
    withCond
    withTimeout
    ;

  # **A dispatch can select one job, and a scheduled run always takes them
  # all.** `on_commit.nix` carries the same expression, so a run to test one
  # job does not start the entire nightly.
  #
  # `version-matrix` never takes this condition. Every job below reads the
  # version list it computes.
  dispatchable = builtins.mapAttrs (
    name: job:
    withCond "github.event_name != 'workflow_dispatch' || inputs.jobs == '' || contains(format(',{0},', inputs.jobs), ',${name},')" job
  );

  # **A dispatch tests the branch that it names, and a scheduled run tests
  # `main`.** `github.ref_name` is the default branch on a scheduled run, and
  # the default branch of nixidae is `main`.
  #
  # A dispatch needs the other half. The job selection above lets you run one
  # job of this workflow, and a checkout of `main` then builds code that the
  # job under test does not have.
  branch = "\${{ github.ref_name }}";
  matrixJob = "version-matrix";

  versionExpression = output: "\${{ fromJson(needs.${matrixJob}.outputs.${output}) }}";

  # One job per kind, each expanded across Nix versions via a real GHA
  # matrix -- there's no longer an install-mode axis to fan out over (see
  # ci/workflows/lib.nix), just the version.
  allTestJobs = {
    static-checks = workflow.mkStaticChecksJob {
      ref = branch;
      needs = [ matrixJob ];
    };

    test-regular =
      workflow.mkRegularTestJob {
        version = "\${{ matrix.version }}";
        ref = branch;
        needs = [ matrixJob ];
      }
      // {
        strategy = {
          fail-fast = false;
          matrix = {
            version = versionExpression "regular_versions";
          };
        };
      };

    test-ubsan =
      workflow.mkUbsanTestJob {
        version = "\${{ matrix.version }}";
        ref = branch;
        needs = [ matrixJob ];
      }
      // {
        strategy = {
          fail-fast = false;
          matrix = {
            version = versionExpression "ubsan_versions";
          };
        };
      };

    test-tsan =
      workflow.mkTsanTestJob {
        version = "\${{ matrix.version }}";
        ref = branch;
        needs = [ matrixJob ];
      }
      // {
        strategy = {
          fail-fast = false;
          matrix = {
            version = versionExpression "tsan_versions";
          };
        };
      };

    # The two builds against a libexpr with no collector. `test-nogc` runs on
    # every commit as well, because it has the measurement that issue #35 asks
    # for; `test-asan` is scheduled only, because it has none.
    # `ci/workflows/lib.nix` carries both halves of that reasoning.
    #
    # It stays here beside `test-asan` even so, and not only for symmetry: a
    # scheduled run is what tells a red `test-asan` apart from an evaluator
    # that does not work without the collector at all.
    test-nogc =
      workflow.mkNoGCTestJob {
        version = "\${{ matrix.version }}";
        ref = branch;
        needs = [ matrixJob ];
      }
      // {
        strategy = {
          fail-fast = false;
          matrix = {
            version = versionExpression "nogc_versions";
          };
        };
      };

    test-asan =
      workflow.mkAsanTestJob {
        version = "\${{ matrix.version }}";
        ref = branch;
        needs = [ matrixJob ];
      }
      // {
        strategy = {
          fail-fast = false;
          matrix = {
            version = versionExpression "asan_versions";
          };
        };
      };

  };
in
workflow.evalWorkflow {
  name = "On schedule";
  on = {
    schedule = [ { cron = "17 3 * * *"; } ];
    workflow_dispatch = {
      inputs = {
        jobs = {
          description = "Comma-separated job IDs to run; leave empty for the full matrix";
          required = false;
          type = "string";
          default = "";
        };
      };
    };
  };
  jobs = {
    version-matrix = mkJob {
      # One output for each variant suffix, plus the regular matrix.
      # `ci/workflows/lib.nix` builds both this and the step that fills it
      # from one list, so a new variant reaches the scheduled workflow without
      # an edit here.
      outputs = workflow.versionMatrixOutputs;
      steps = [
        (steps.checkout { ref = branch; })
        (steps.installNix { })
        (steps.cachix { })
        # The version list is computed here rather than rendered, so a nixpkgs
        # that arrives between renders can add or drop a Nix version and this
        # run still tests it. `ci/steps.nix` says why one script replaced five
        # `nix eval` calls that each repeated the variant suffixes as a regular
        # expression.
        (
          workflow.mkNixRunStep {
            name = "Compute Nix version matrices";
            attr = "version-matrix";
            cap = caps.versionMatrix;
          }
          // {
            id = "versions";
          }
        )
      ];
    };
  }
  // dispatchable allTestJobs;
}
