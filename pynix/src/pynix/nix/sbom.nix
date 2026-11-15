# Records of derivations, as JSON: one record for each output.
#
# `closure drv`: the build closure of one derivation. A dependency is a
# derivation, or a list of derivations, in the attributes that `derivation`
# receives (`drvAttrs`), as bombon reads it.
#
# `values drvs`: each of a list of derivations and its sibling outputs, and
# nothing they depend on. For values an evaluator recorded as it coerced
# them (huggorm `recorded_derivation`), which already hold the closure.
#
# The walk uses no nixpkgs `lib`, because a value can come from any flake.
#
# `tryEval` guards each field that a package computes, so one package whose
# `meta` throws (an unfree license, a broken `src`) does not stop the SBOM.
# The JSON carries no string context: with it, the caller would realise the
# whole build closure.
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

  # `deepSeq`, because `tryEval` forces only the outer value: a throw inside a
  # list of licenses would escape it and stop `toJSON` instead. Only for the
  # values the JSON holds: `meta` itself can hold derivations, and a deep
  # force of those overflows the stack.
  guard =
    default: f:
    let
      result = tryEval (
        let
          v = f null;
        in
        builtins.deepSeq v v
      );
    in
    if result.success then result.value else default;

  shallow =
    default: f:
    let
      result = tryEval (f null);
    in
    if result.success then result.value else default;

  outputsOf = d: if d ? outputs then map (o: d.${o}) d.outputs else [ d ];

  depsOfValue =
    v:
    shallow [ ] (
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

  # nixpkgs `lib.licenses.toSPDX`, for a compound license (`AND`, `WITH`,
  # `+`). A plain license or a list stays in `license`.
  spdxExpression =
    l:
    let
      bracket =
        x:
        if x.licenseType == "compound" || x.licenseType == "exception" then
          "(${spdxExpression x})"
        else
          spdxExpression x;
    in
    if l.licenseType == "simple" then
      l.spdxId or "LicenseRef-nixos-${l.shortName}"
    else if l.licenseType == "compound" then
      builtins.concatStringsSep " ${l.operator} " (map bracket l.licenses)
    else if l.licenseType == "exception" then
      "${bracket l.license} ${l.operator} ${bracket l.exception}"
    else if l.licenseType == "plus" then
      "${bracket l.license}${l.operator}"
    else
      throw "unknown license type";

  licenseExpression =
    m:
    guard null (
      _:
      let
        l = m.license or null;
      in
      if
        isAttrs l
        && builtins.elem (l.licenseType or null) [
          "compound"
          "exception"
          "plus"
        ]
      then
        spdxExpression l
      else
        null
    );

  stringOrNull = v: if isString v then v else null;
  boolOrNull = v: if builtins.isBool v then v else null;
  strings =
    v:
    if isList v then
      filter isString v
    else if isString v then
      [ v ]
    else
      [ ];

  maintainer = p: {
    name = stringOrNull (p.name or null);
    email = stringOrNull (p.email or null);
    github = stringOrNull (p.github or null);
  };

  meta =
    d:
    let
      m = shallow { } (_: if isAttrs (d.meta or null) then d.meta else { });
      identifiers = shallow { } (_: if isAttrs (m.identifiers or null) then m.identifiers else { });
      # `v1` is the stable interface; the top level mirrors it.
      v1 = shallow { } (_: if isAttrs (identifiers.v1 or null) then identifiers.v1 else { });
    in
    {
      license = licenses m;
      licenseExpression = licenseExpression m;
      homepages = guard [ ] (_: strings (m.homepage or null));
      changelogs = guard [ ] (_: strings (m.changelog or null));
      downloadPage = guard null (_: stringOrNull (m.downloadPage or null));
      description = guard null (_: stringOrNull (m.description or null));
      longDescription = guard null (_: stringOrNull (m.longDescription or null));
      mainProgram = guard null (_: stringOrNull (m.mainProgram or null));
      position = guard null (_: stringOrNull (m.position or null));
      maintainers = guard [ ] (_: map maintainer (filter isAttrs (m.maintainers or [ ])));
      unfree = guard null (_: boolOrNull (m.unfree or null));
      insecure = guard null (_: boolOrNull (m.insecure or null));
      broken = guard null (_: boolOrNull (m.broken or null));
      knownVulnerabilities = guard [ ] (_: strings (m.knownVulnerabilities or null));
      cpe = guard null (_: stringOrNull (v1.cpe or identifiers.cpe or null));
      possibleCpes = guard [ ] (
        _:
        let
          given = v1.possibleCPEs or identifiers.possibleCPEs or [ ];
        in
        if isList given then filter isString (map (c: c.cpe or null) given) else [ ]
      );
      cpeParts = guard null (
        _:
        let
          given = v1.cpeParts or identifiers.cpeParts or null;
        in
        if isAttrs given then
          {
            part = stringOrNull (given.part or null);
            product = stringOrNull (given.product or null);
          }
        else
          null
      );
      purls = guard [ ] (
        _: strings (v1.purls or identifiers.purls or v1.purl or identifiers.purl or null)
      );
    };

  src =
    d:
    guard null (
      _:
      let
        s = d.src or null;
      in
      # fetchurl and fetchzip name archive `urls`. fetchgit, fetchFromGitHub
      # and fetchFromGitLab name the repository in `gitRepoUrl`, beside an
      # archive URL or none.
      if isAttrs s && (isList (s.urls or null) || isString (s.gitRepoUrl or null)) then
        {
          urls = if isList (s.urls or null) then filter isString s.urls else [ ];
          repository = stringOrNull (s.gitRepoUrl or null);
          hash = stringOrNull (s.outputHash or null);
          rev = stringOrNull (s.rev or null);
        }
      else
        null
    );

  # Only what names a file: `tryEval` catches no type error, and a package
  # may hold an attribute set of patches (gnulib's `passthru.patches`).
  patches =
    d:
    guard [ ] (
      _:
      let
        flatten = x: if isList x then concatMap flatten x else [ x ];
        names = x: isString x || builtins.isPath x || (isAttrs x && x ? outPath);
        given = d.patches or [ ];
      in
      if isList given then map toString (filter names (flatten given)) else [ ]
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
      drvPath = guard null (_: stringOrNull (d.drvPath or null));
      meta = meta d;
      src = src d;
      patches = patches d;
      buildReferences = map (o: o.outPath) (depsOf d);
      # The language a package is a module of, as its nixpkgs builder marks it.
      language = guard null (
        _:
        if d ? pythonModule then
          "python"
        else if d ? perlModule then
          "perl"
        else if d ? extensionName then
          "php"
        else
          null
      );
    };

  json = entries: builtins.unsafeDiscardStringContext (builtins.toJSON (map record entries));
in
{
  closure =
    drv:
    json (genericClosure {
      startSet = map item (outputsOf drv);
      operator = entry: map item (depsOf entry.drv);
    });
  values =
    drvs:
    json (genericClosure {
      startSet = map item (concatMap outputsOf drvs);
      operator = _: [ ];
    });
}
