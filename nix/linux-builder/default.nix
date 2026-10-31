/**
  The Linux builder guest for `pynix build --linux-builder`
  (nixidae/nixidae#1138).

  `manifest` is a JSON file that names vzvm and what it boots. pynix reads it
  through `--linux-builder-guest` or `PYNIX_LINUX_BUILDER_GUEST`. pynix's own
  package does not depend on it: the guest is an aarch64-linux closure, and
  a Mac with no Linux builder cannot build it.
*/
{ pkgs, sources }:
let
  inherit (pkgs) lib;

  daemonVsockPort = 3030;

  guest = import "${sources.nixpkgs}/nixos/lib/eval-config.nix" {
    system = null;
    modules = [
      ./guest.nix
      { nixpkgs.hostPlatform = "aarch64-linux"; }
    ];
    specialArgs = { inherit daemonVsockPort; };
  };

  inherit (guest.config.system.build) kernel netbootRamdisk toplevel;
in
{
  inherit guest;

  manifest = pkgs.writeText "pynix-linux-builder.json" (
    builtins.toJSON {
      kernel = "${kernel}/${guest.config.system.boot.loader.kernelFile}";
      initrd = "${netbootRamdisk}/initrd";
      cmdline = "init=${toplevel}/init ${toString guest.config.boot.kernelParams}";
      inherit daemonVsockPort;
      vzvm = lib.getExe pkgs.vzvm;
    }
  );
}
