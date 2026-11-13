# The package set of pkgset.nix, with an attribute that raises an error
# `builtins.tryEval` does not catch: an overlay that names a missing input
# (nixidae/nixidae#1304). One sits at the top level and one inside a set the
# walk enters.
{ }:
let
  base = import ./pkgset.nix { };
in
base
// {
  missingInput = { }.aiscalate;
  pythonPackages = base.pythonPackages // {
    missingInput = { }.aiscalate;
  };
}
