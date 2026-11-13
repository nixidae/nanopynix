/**
  The option walks of `pynix search`, as a function of a nixpkgs `lib`.

  `metadata options` lists every option under the tree, with what a search
  ranks and shows. `values options` maps each option name to its `default`
  and `example`, and forces none until a caller selects one. `render value`
  renders one value of a configuration as a `default` is rendered.

  Both walks are one walk with a different entry, so the two cannot disagree
  about which options exist.
*/
lib:
let
  /**
    How deep the walk follows a sub-option before it stops.

    `lib.optionAttrSetToDocList` has no bound. Measured against a real
    `nixosConfigurations` of 24 941 options: depth 4 reaches 24 935 of them
    and depth 6 reaches every one, so 8 leaves margin.
  */
  depth = 8;

  walkWith =
    entry: depth: opts:
    builtins.concatMap (
      opt:
      let
        v = opt.visible or true;
        subVisible = if builtins.isBool v then v else v == "transparent";
        sub = (opt.type or { }).getSubOptions or (_: { }) opt.loc;
      in
      [ (entry opt) ]
      ++ (if depth > 0 && subVisible && sub != { } then walkWith entry (depth - 1) sub else [ ])
    ) (lib.collect lib.isOption opts);

  walk = entry: walkWith entry depth;

  # A value that a module wrote through `lib.literalMD` or
  # `lib.literalExpression` carries its own text, and every other value goes
  # through `lib.generators.toPretty`. `lib.options.renderOptionValue` is the
  # same two branches.
  render =
    value:
    if lib.isAttrs value && value ? _type && value ? text then
      {
        type = value._type;
        inherit (value) text;
      }
    else
      {
        type = "literalExpression";
        text = lib.generators.toPretty { multiline = true; } value;
      };
in
{
  inherit render;

  metadata =
    options:
    walk (opt: {
      name = lib.showOption opt.loc;
      description = opt.description or null;
      declarations = builtins.filter (x: x != lib.unknownModule) opt.declarations;
      internal = opt.internal or false;
      visible =
        let
          v = opt.visible or true;
        in
        if builtins.isBool v then v else v == "shallow";
      readOnly = opt.readOnly or false;
      type = opt.type.description or "unspecified";
    }) options;

  # One attrset keyed by name: `listToAttrs` forces the walk and every
  # `lib.showOption`, and no `default` or `example`. A caller forces one
  # option, and catches what that one raises. `defaultText` comes first,
  # because a module declares it for a default that cannot print.
  values =
    options:
    builtins.listToAttrs (
      walk (opt: {
        name = lib.showOption opt.loc;
        value = {
          default =
            if opt ? defaultText then
              render opt.defaultText
            else if opt ? default then
              render opt.default
            else
              null;
          example = if opt ? example then render opt.example else null;
        };
      }) options
    );
}
