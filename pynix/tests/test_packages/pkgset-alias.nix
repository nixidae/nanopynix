# The package set of pkgset.nix, with one alias shaped as nixpkgs shapes one:
# a package that warns when the walk forces it (nixidae/nixidae#1310).
{ }:
let
  base = import ./pkgset.nix { };
in
base
// {
  aliasedRipgrep = builtins.warn "'aliasedRipgrep' has been renamed to 'ripgrep'" base.ripgrep;
}
