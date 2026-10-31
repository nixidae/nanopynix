/**
  The Linux builder guest for `pynix build --linux-builder`
  (nixidae/nixidae#1138).

  `manifest` is a JSON file that names vzvm and what it boots. pynix reads it
  through `--linux-builder-guest` or `PYNIX_LINUX_BUILDER_GUEST`. pynix's own
  package does not depend on it: the guest is an aarch64-linux closure, and
  a Mac with no Linux builder cannot build it.

  `pynix` is the opt-in that does depend on it: see `withGuest`.
*/
{
  pkgs,
  sources,
  pynix,
}:
let
  inherit (pkgs) lib;

  daemonVsockPort = 3030;
  sshVsockPort = 22;

  guest = import "${sources.nixpkgs}/nixos/lib/eval-config.nix" {
    system = null;
    modules = [
      ./guest.nix
      { nixpkgs.hostPlatform = "aarch64-linux"; }
    ];
    specialArgs = { inherit daemonVsockPort sshVsockPort; };
  };

  inherit (guest.config.system.build) kernel netbootRamdisk toplevel;

  manifest = pkgs.writeText "pynix-linux-builder.json" (
    builtins.toJSON {
      kernel = "${kernel}/${guest.config.system.boot.loader.kernelFile}";
      initrd = "${netbootRamdisk}/initrd";
      cmdline = "init=${toplevel}/init ${toString guest.config.boot.kernelParams}";
      inherit daemonVsockPort sshVsockPort;
      sshHostKey = lib.trim (
        builtins.readFile "${sources.nixpkgs}/nixos/modules/profiles/keys/ssh_host_ed25519_key.pub"
      );
      vzvm = lib.getExe pkgs.vzvm;
    }
  );

  /**
    *pynix*, wrapped so that `--linux-builder` finds *manifest* with no
    setting. The guest is in the closure, so a garbage collection keeps it
    while the wrapper is installed; the overlay guest boots from the host
    store, so that store must hold it.

    Only `bin/pynix` changes. Everything else links to *pynix*, so the
    completions, the manual pages and the skill stay as they are. The
    completions call `pynix` off PATH with `_ARGCOMPLETE` set, which the
    wrapper passes through. A setting or the environment still overrides
    the guest.

    # Type

    ```
    withGuest :: { pynix :: Derivation, manifest :: Derivation } -> Derivation
    ```
  */
  withGuest =
    { pynix, manifest }:
    pkgs.symlinkJoin {
      name = "${pynix.name}-linux-builder";
      paths = [ pynix ];
      nativeBuildInputs = [ pkgs.makeBinaryWrapper ];
      postBuild = ''
        rm $out/bin/pynix
        makeWrapper ${pynix}/bin/pynix $out/bin/pynix \
          --set-default PYNIX_LINUX_BUILDER_GUEST ${manifest}
      '';
      inherit (pynix) meta;
    };
in
{
  inherit guest manifest withGuest;

  # nix build --file . internal.linuxBuilder.pynix
  pynix = withGuest { inherit pynix manifest; };
}
