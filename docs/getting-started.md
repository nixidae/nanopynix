# Getting started

nanopynix ships `pynix` as `packages.pynix`. Every command below builds it
from this repository with no flake.

## Run pynix once

```console
$ nix run --file https://github.com/nixidae/nanopynix/archive/main.tar.gz packages.pynix -- eval --expr '1 + 1'
2
```

## Install pynix

Add the package to a NixOS configuration. Pass your own `pkgs`, so that
pynix uses your package set:

```nix
{ pkgs, ... }:
let
  nanopynix = import (builtins.fetchTarball "https://github.com/nixidae/nanopynix/archive/main.tar.gz") {
    inherit pkgs;
  };
in
{
  environment.systemPackages = [ nanopynix.packages.pynix ];
}
```

home-manager takes the same package in `home.packages`. A flake
configuration takes the same package from
`inputs.nanopynix.packages.${system}.pynix`.

## Next steps

- {doc}`pynix/index` shows the common commands.
- {doc}`pynix/configuration` sets a default once, not on each command.
- `packages.pynix-lsp` is the language server for an editor.
