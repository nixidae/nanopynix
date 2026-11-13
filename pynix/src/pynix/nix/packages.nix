/**
  The package walk of `pynix search`, as a function of a nixpkgs `lib`.

  `top pkgs` reads the top level: its packages, and the size of each set to
  enter. `group pkgs namesJson` reads the named sets, at any depth.
  `entryAt pathJson value` reads one attribute, and answers `{ package }`,
  `{ set }` with the number of names in it, or null. A path or a list of names
  crosses as JSON text.

  Every attribute goes through `builtins.tryEval`. nixpkgs holds packages that
  throw when they are evaluated at all: one that its licence forbids, one
  marked broken, one that no current platform builds. A walk returns one list,
  forced in one pass, so one such package would end the whole extraction.
  `deepSeq` forces the record inside it, so a field that throws skips its
  package too.

  `tryEval` does not catch every failure: it stops a `throw` and an `assert`,
  and not a missing attribute, a type error or a failed import from
  derivation. An overlay that names a missing input is one
  (nixidae/nixidae#1304). So when a walk fails, `pynix._packages` reads each
  attribute through `entryAt` and skips the ones that raise.
*/
lib:
let
  record = path: value: {
    attr = lib.concatStringsSep "." path;
    pname = value.pname or value.name or (lib.last path);
    version = value.version or "";
    description = value.meta.description or null;
    mainProgram = value.meta.mainProgram or null;
    broken = value.meta.broken or false;
    unfree = !(value.meta.available or true) || !(value.meta.license.free or true);
  };

  entry =
    path: value:
    let
      probed = builtins.tryEval (
        if lib.isDerivation value then
          let
            r = record path value;
          in
          builtins.deepSeq r { package = r; }
        else if builtins.isAttrs value && (value.recurseForDerivations or false) == true then
          { set = builtins.length (builtins.attrNames value); }
        else
          null
      );
    in
    if probed.success then probed.value else null;

  under =
    path: set:
    lib.concatLists (
      lib.mapAttrsToList (
        name: value:
        let
          here = path ++ [ name ];
          found = entry here value;
        in
        if found == null then
          [ ]
        else if found ? package then
          [ found.package ]
        else
          under here value
      ) set
    );

  entries =
    pkgs:
    builtins.filter (x: x.found != null) (
      lib.mapAttrsToList (name: value: {
        inherit name;
        found = entry [ name ] value;
      }) pkgs
    );
in
{
  entryAt = pathJson: entry (builtins.fromJSON pathJson);

  top =
    pkgs:
    let
      found = entries pkgs;
    in
    {
      packages = map (x: x.found.package) (builtins.filter (x: x.found ? package) found);
      sets = map (x: {
        inherit (x) name;
        size = x.found.set;
      }) (builtins.filter (x: x.found ? set) found);
    };

  group =
    pkgs: namesJson: lib.concatMap (name: under [ name ] pkgs.${name}) (builtins.fromJSON namesJson);
}
