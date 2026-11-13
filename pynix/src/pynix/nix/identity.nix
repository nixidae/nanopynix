/**
  The facts that tell two package sets built from one source apart, as JSON
  text. `pynix._packages.package_identity` hashes it into the cache key.

  The three fields cover the three cases of nixidae/nixidae#260. `system`
  covers a cross-compiled set. `config` covers `allowUnfree` and
  `allowBroken`, which change which attributes evaluate at all. `attrs`
  covers an overlay that adds or removes an attribute.

  A config entry may be a function, and `builtins.toJSON` raises on one where
  `builtins.tryEval` does not catch it: `allowUnfreePredicate` is one. So
  `mark` never hands a function to `toJSON`. A scalar joins the key by its
  value, and anything else by its type name: `gitConfig` reads as `"set"`. A
  change from one predicate to another is therefore not seen.
*/
pkgs:
let
  config = pkgs.config or { };
  mark =
    value:
    if builtins.isBool value then
      (if value then "true" else "false")
    else if builtins.isString value then
      value
    else if builtins.isInt value then
      builtins.toString value
    else
      builtins.typeOf value;
in
builtins.toJSON {
  system = pkgs.stdenv.hostPlatform.system or "unknown";
  attrs = builtins.length (builtins.attrNames pkgs);
  config = builtins.listToAttrs (
    map (name: {
      inherit name;
      value = mark (config.${name} or null);
    }) (builtins.attrNames config)
  );
}
