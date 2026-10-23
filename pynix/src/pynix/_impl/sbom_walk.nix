# The build closure of a derivation, as JSON: one record for each output.
#
# A dependency is a derivation, or a list of derivations, in the attributes
# that `derivation` receives (`drvAttrs`), as bombon reads it. The walk uses
# no nixpkgs `lib`, because the target can come from any flake.
#
# `tryEval` guards each field that a package computes, so one package whose
# `meta` throws (an unfree license, a broken `src`) does not stop the SBOM.
# The JSON carries no string context: with it, the caller would realise the
# whole build closure.
drv:
let
  inherit (builtins)
    concatLists
    concatMap
    filter
    genericClosure
    isAttrs
    isList
    isString
    map
    tryEval
    ;

  isDerivation = x: isAttrs x && (x.type or null) == "derivation";

  guard =
    default: f:
    let
      result = tryEval (f null);
    in
    if result.success then result.value else default;

  outputsOf = d: if d ? outputs then map (o: d.${o}) d.outputs else [ d ];

  depsOfValue =
    v:
    guard [ ] (
      _:
      if isDerivation v then
        outputsOf v
      else if isList v then
        concatMap outputsOf (filter isDerivation v)
      else
        [ ]
    );

  depsOf = d: concatLists (map depsOfValue (builtins.attrValues (d.drvAttrs or { })));

  item = d: {
    key = d.outPath;
    drv = d;
  };

  closure = genericClosure {
    startSet = map item (outputsOf drv);
    operator = entry: map item (depsOf entry.drv);
  };

  license =
    l:
    if isString l then
      l
    else if isAttrs l then
      {
        spdxId = l.spdxId or null;
        fullName = l.fullName or l.shortName or null;
      }
    else
      null;

  licenses =
    m:
    guard null (
      _:
      if !(m ? license) then
        null
      else if isList m.license then
        map license m.license
      else
        license m.license
    );

  stringOrNull = v: if isString v then v else null;

  meta =
    d:
    let
      m = guard { } (_: if isAttrs (d.meta or null) then d.meta else { });
      identifiers = guard { } (_: if isAttrs (m.identifiers or null) then m.identifiers else { });
    in
    {
      license = licenses m;
      homepage = guard null (
        _:
        let
          h = m.homepage or null;
        in
        if isList h then stringOrNull (builtins.head h) else stringOrNull h
      );
      description = guard null (_: stringOrNull (m.description or null));
      cpe = guard null (_: stringOrNull (identifiers.cpe or null));
      possibleCpes = guard [ ] (
        _: filter isString (map (c: c.cpe or null) (identifiers.possibleCPEs or [ ]))
      );
    };

  src =
    d:
    guard null (
      _:
      let
        s = d.src or null;
      in
      if isAttrs s && s ? urls then
        {
          urls = filter isString s.urls;
          hash = stringOrNull (s.outputHash or null);
        }
      else
        null
    );

  patches =
    d:
    guard [ ] (
      _:
      let
        flatten = x: if isList x then concatMap flatten x else [ x ];
      in
      map toString (flatten (d.patches or [ ]))
    );

  record =
    entry:
    let
      d = entry.drv;
    in
    {
      path = entry.key;
      name = guard null (_: stringOrNull (d.name or null));
      pname = guard null (_: stringOrNull (d.pname or null));
      version = guard null (_: stringOrNull (d.version or null));
      outputName = guard null (_: stringOrNull (d.outputName or null));
      outputHash = guard null (_: stringOrNull (d.outputHash or null));
      meta = meta d;
      src = src d;
      patches = patches d;
      buildReferences = map (o: o.outPath) (depsOf d);
    };
in
builtins.unsafeDiscardStringContext (builtins.toJSON (map record closure))
