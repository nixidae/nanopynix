# The CI experiments, and the one interface that `workflows/experiment.nix` runs.
#
# **An experiment is a package here, and not a script in a YAML file.** A
# workflow becomes available to `workflow_dispatch` only after that workflow
# reaches the default branch, so a new YAML file costs a push to `develop`
# before it can run once. A new attribute in this file costs nothing: the
# generic runner already carries the name as an input, and the branch that
# holds the experiment is an input too.
#
# Each experiment reads its matrix cell from `MATRIX_JSON`, which the runner
# sets to `toJSON(matrix)`. An experiment that takes no matrix ignores it.
#
# Run one the way CI runs it:
#
#   MATRIX_JSON='{"arm":"control"}' nix run --file . internal.experiments.<name>
#
# None is defined now. The last one, `gc-soak`, measured a collector defect of
# the nanopynix library, and went with that library.
_: { }
