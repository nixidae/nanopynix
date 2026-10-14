# The Pages workflow. ci/render.py renders it to
# `.github/workflows/on_pages.yml`, the one workflow of nanopynix's own
# repository, which josh exports from nixidae (nixidae/nixidae#4). The tests
# run in nixidae, from commit.nix, schedule.nix and experiment.nix.
let
  workflow = import ./lib.nix { };
  inherit (workflow) withCond;
in
workflow.evalWorkflow {
  name = "Pages";
  on = {
    push.branches = [ "main" ];
    workflow_dispatch = null;
  };
  jobs = {
    docs-build = withCond "github.ref == 'refs/heads/main'" (workflow.mkDocsBuildJob { });
    docs-deploy = withCond "github.ref == 'refs/heads/main'" (
      workflow.mkDocsDeployJob { needs = "docs-build"; }
    );
  };
}
