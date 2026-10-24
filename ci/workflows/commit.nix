# A test workflow. nixidae renders it to
# `.github/workflows/nanopynix-commit.yml`, with nanopynix as the working
# directory of every step. nixidae/nixidae#4.
{
  repo ? import ../../. { },
}:
let
  workflow = import ./lib.nix { inherit repo; };
  inherit (workflow) withCond;

  testJobs = workflow.mkStaticTestJobs { };
  tsanTestJobs = workflow.mkStaticTsanTestJobs { };
  ubsanTestJobs = workflow.mkStaticUbsanTestJobs { };
  # The build with no collector, and the build under ASAN, on every commit.
  # `ci/workflows/lib.nix` gives the measurement that earns each slot.
  nogcTestJobs = workflow.mkStaticNoGCTestJobs { };
  asanTestJobs = workflow.mkStaticAsanTestJobs { };
  # One macOS job, on the floor version. `lib.nix` says why this one, and why
  # it carries `continue-on-error`. Issues #143 and #210.
  darwinTestJob = {
    test-darwin-nix_2_34 = workflow.mkDarwinTestJob {
      version = "nix_2_34";
    };
  };
  # Named alongside the test jobs so the `jobs` dispatch input can select it,
  # and so last-green waits for it. It is the cheapest job in the workflow.
  staticChecksJob = {
    static-checks = workflow.mkStaticChecksJob { };
  };
  # The jobs last-green waits for. **The macOS job is not one of them.**
  # GitHub skips a job whose `needs` failed, and `continue-on-error` stops the
  # run from failing without making the job succeed, so a red macOS job would
  # take last-green with it. That shape cost a docs deploy once already. Move
  # the job in here when it is green and `continue-on-error` comes off.
  gatingTestJobs =
    staticChecksJob
    // testJobs
    // tsanTestJobs
    // ubsanTestJobs
    // nogcTestJobs
    // asanTestJobs;
  # Everything the `jobs` dispatch input can name, which does include macOS.
  allTestJobs = gatingTestJobs // darwinTestJob;
  selectedTestJobs = builtins.mapAttrs (
    name: job:
    withCond "github.event_name != 'workflow_dispatch' || inputs.jobs == '' || contains(format(',{0},', inputs.jobs), ',${name},')" job
  ) allTestJobs;
in
workflow.evalWorkflow {
  name = "On commit";
  on = {
    # Keep ci-develop available as a pushed ref for focused workflow_dispatch
    # runs without starting the full push matrix.
    push = {
      branches-ignore = [ "ci-develop" ];
    };
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
  jobs = selectedTestJobs // {
    # **The commit that a bisect can start from.** It waits for every gating
    # job, and it moves a branch rather than publishing. See
    # `mkLastGreenJob` for why the macOS job is not one of them, and why the
    # gate sits on the step. Issue #283.
    #
    # `main` alone: every other branch is somebody's work in progress. The
    # branch is nixidae's, and its name says whose it is.
    last-green = withCond "github.ref == 'refs/heads/main' && !cancelled()" (
      workflow.mkLastGreenJob {
        gates = builtins.attrNames gatingTestJobs;
        branch = "nanopynix/last-green";
      }
    );
  };
}
