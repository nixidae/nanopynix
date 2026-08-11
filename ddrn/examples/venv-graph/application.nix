# A derivation of the application, so that `meta` has somewhere to live.
#
# **`builtins.outputOf` gives a bare string, and a string carries no `meta`.**
# Measured: `nix run` over that string fails with `attribute 'type' does not
# exist`, and an attribute set that names `outPath` and `drvPath` by hand fails
# with `path "/0a988..." is not in the Nix store`. This derivation is the only
# shape that works, and its whole content is one symbolic link to the
# application. Measured: it adds 176 bytes and one path to a closure of
# 238229120 bytes. `ddrn/NIX-FACTS.md` holds both measurements, under "A dynamic
# output carries no `meta`".
#
# **The output of this derivation is deferred, like every node of the graph.**
# Its input is the application, whose path no evaluation knows, so Nix cannot
# compute this path either. `nix run` realises the installable and then reads
# the result, so it works. `lib.getExe` interpolates `outPath` during
# evaluation, so it gives a placeholder: that placeholder is correct inside
# another derivation, and it is not a path that a shell can use.
#
# **The build verifies the program rather than trust the name.** A member
# usually installs a console script of its own name, and a member that does not
# is why `program` is an argument at all.
#
# `uv.nix` calls this for its `app` attribute, and `tests/sealed.nix` calls it
# over the fixtures. The second caller is what covers this file with no network.
{
  pkgs,
  # The application, as `builtins.outputOf <planner>.outPath "out"`.
  application,
  # The program that `meta.mainProgram` names, or `null` for no `meta`.
  program ? null,
  # The name of the derivation. `program` takes this place when it is not null,
  # because the name of a program is the better name for a result.
  name,
}:

pkgs.runCommand (if program != null then program else name)
  {
    inherit application;
    meta = pkgs.lib.optionalAttrs (program != null) { mainProgram = program; };
  }
  ''
    ${pkgs.lib.optionalString (program != null) ''
      if [ ! -x "$application/bin/${program}" ]; then
        echo "the application holds no program called '${program}'." >&2
        echo "It holds:" >&2
        ls -1 "$application/bin" >&2
        echo "Name one of those with the 'mainProgram' argument." >&2
        exit 1
      fi
    ''}
    ln -s "$application" "$out"
  ''
