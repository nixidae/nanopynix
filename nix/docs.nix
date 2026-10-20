{
  lib,
  stdenvNoCC,
  pythonSet,
}:
let
  # Built, not editable: this is a buildable artifact, so its inputs have to
  # come from the store. `pynix` here is the *library* out of the package set,
  # not the `mkApplication` wrapper of the same name in default.nix -- autodoc
  # imports modules, and an application exposes only `bin/`.
  pythonEnv = pythonSet.mkVirtualEnv "nanopynix-docs-env" {
    pynix = [ "docs" ];
  };
in
stdenvNoCC.mkDerivation {
  pname = "nanopynix-docs";
  version = "0";

  # What Sphinx reads: `docs/`, the logo it links, the sources `docs/conf.py`
  # and `_generate_pynix_reference.py` put on `sys.path`, and the examples
  # the pages include. `-W` fails the build on anything missing.
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../docs
      ../.assets
      ../grpclib-transports/docs/examples
      ../libpynix/examples
      (lib.fileset.fileFilter (file: file.hasExt "py") ../nanopynix/src)
      (lib.fileset.fileFilter (file: file.hasExt "py") ../pynix/src)
      (lib.fileset.fileFilter (file: file.hasExt "py") ../pynix-lsp/src)
      (lib.fileset.fileFilter (file: file.hasExt "py") ../grpclib-transports/src)
    ];
  };

  nativeBuildInputs = [ pythonEnv ];

  buildPhase = ''
    runHook preBuild
    NANOPYNIX_DOCS_OFFLINE=1 sphinx-build -b html docs "$out" -W
    runHook postBuild
  '';

  dontInstall = true;
}
