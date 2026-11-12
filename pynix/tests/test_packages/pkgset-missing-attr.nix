# The package set of pkgset.nix, with one attribute that raises an error
# `builtins.tryEval` does not catch: an overlay that names a missing input
# (nixidae/nixidae#1304).
{ }:
import ./pkgset.nix { }
// {
  missingInput = { }.aiscalate;
}
