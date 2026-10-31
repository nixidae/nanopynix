# The Linux builder guest: a NixOS system that vzvm boots for `pynix build
# --linux-builder` (nixidae/nixidae#1138).
#
# Taken from croshome's hosts/macbook/vz-builder/guest.nix, cut down to the
# store and the daemon. The store is a local-overlay store whose lower layer
# is the host's /nix over virtiofs, so the guest starts with every path the
# host has, and the disk holds only new outputs. That needs a case-sensitive
# /nix on the host. The daemon serves the host over vsock, not ssh.
{
  modulesPath,
  config,
  lib,
  pkgs,
  daemonVsockPort,
  ...
}:
let
  # check-mount=false: stage 1 mounts the lower layer under /sysroot, so
  # /proc/self/mounts names `/sysroot/host-nix/nix/store` after the pivot,
  # and Nix's string comparison fails on a correct layering.
  # `%3F` is a literal `?` inside the value of a query parameter.
  overlayStoreUri = lib.concatStrings [
    "local-overlay://"
    "?lower-store=/host-nix%3Fread-only=true"
    "&upper-layer=/nix/.rw-store/store"
    "&check-mount=false"
  ];
in
{
  imports = [ "${modulesPath}/installer/netboot/netboot.nix" ];

  system.stateVersion = "26.11";

  boot.kernelPackages = pkgs.linuxPackages_latest;

  boot.kernelParams = [
    "console=hvc0"
    "quiet"
    "systemd.log_level=warning"
    # PID 1 reads it from the environment. The default burst of 5 mount
    # events per second stalled stage 1 for ~1.6 s (systemd/systemd#28264).
    "SYSTEMD_DEFAULT_MOUNT_RATE_LIMIT_BURST=100"
    # A halted guest looks alive to vzvm, and every open build hangs. A
    # reboot ends a VZ VM, so the builds fail instead.
    "panic=1"
    "oops=panic"
  ];

  # The default module set walks buses this VM does not have: two waits of
  # ~0.8 s each before the mounts.
  boot.initrd.includeDefaultModules = false;
  boot.initrd.availableKernelModules = [
    "virtio_pci"
    "virtio_net"
    "virtio_console"
    "virtio_rng"
    "fuse"
    "virtiofs"
    "virtio_blk"
    "overlay"
  ];
  boot.initrd.kernelModules = [ "vmw_vsock_virtio_transport" ];
  boot.kernelModules = [
    "virtiofs"
    "overlay"
  ];
  boot.initrd.systemd.tpm2.enable = false;

  # x86_64-linux through Rosetta: the binfmt handler and extra-platforms.
  virtualisation.rosetta.enable = true;

  # The root is the disk the runner creates empty on every start. A tmpfs
  # root would hold every output in guest RAM, which the host never gets
  # back. `supportedFilesystems` is what puts mkfs.xfs in the initrd.
  boot.initrd.supportedFilesystems = [ "xfs" ];
  # mkForce on the whole entry: netboot sets it with mkImageMediaOverride,
  # which beats a mkForce on one attribute.
  fileSystems."/" = lib.mkForce {
    device = "/dev/vda";
    fsType = "xfs";
    autoFormat = true;
    noCheck = true;
    options = [ "noatime" ];
  };
  fileSystems."/nix/.rw-store" = lib.mkForce {
    enable = false;
    device = "none";
    fsType = "tmpfs";
  };

  # On the guest's disk, not on virtiofs: the guest clock runs ~70 ms behind
  # macOS, and host mtimes from the future made meson report clock skew.
  systemd.tmpfiles.rules = [ "d /nix/.rw-store/build 0755 root root -" ];

  # The host connects over vsock, so the network serves substitution only.
  # networkd, because dhcpcd held up boot for 4.6 s. `useDHCP` makes no
  # .network unit under networkd, so the interface needs one of its own.
  networking.hostName = "pynix-linux-builder";
  networking.useNetworkd = true;
  networking.useDHCP = false;
  systemd.network.networks."10-uplink" = {
    matchConfig.Name = "en*";
    networkConfig.DHCP = "ipv4";
    linkConfig.RequiredForOnline = "no";
  };
  networking.firewall.enable = false;
  services.timesyncd.enable = false;

  nix.settings = {
    experimental-features = [
      "local-overlay-store"
      "read-only-local-store"
    ];
    max-jobs = "auto";
    cores = 0;
    build-dir = "/nix/.rw-store/build";
    # The disk is new on every start.
    fsync-metadata = false;
    # Every client proxies to the daemon. The daemon's command line names the
    # overlay store and beats nix.conf, so it does not proxy to itself.
    store = "daemon";
  };

  # `%` doubled for systemd, where `%3F` is a specifier and the unit fails to
  # parse.
  systemd.services.nix-daemon.serviceConfig.ExecStart = [
    ""
    "@${lib.getExe' config.nix.package "nix-daemon"} nix-daemon --daemon --store ${
      lib.replaceStrings [ "%" ] [ "%%" ] overlayStoreUri
    }"
  ];
  # The daemon opens the lower store once. Without the mount it opens an
  # empty one and refetches every lower path, silently.
  systemd.services.nix-daemon.unitConfig.RequiresMountsFor = [
    "/host-nix/nix"
    "/nix/.rw-store"
  ];

  # The store and its database: Nix reads the lower store through a store
  # object, so the database must be there too.
  fileSystems."/host-nix/nix" = {
    device = "hostnix";
    fsType = "virtiofs";
    options = [ "ro" ];
    neededForBoot = true;
  };
  # One lower layer only: Nix requires exactly one, equal to the lower
  # store's directory. The guest's own closure is in the host store, so
  # netboot's squashfs is not needed.
  fileSystems."/nix/store" = lib.mkForce {
    overlay = {
      lowerdir = [ "/host-nix/nix/store" ];
      upperdir = "/nix/.rw-store/store";
      workdir = "/nix/.rw-store/work";
    };
    neededForBoot = true;
  };
  netboot.storeContents = lib.mkForce [ ];
  fileSystems."/nix/.ro-store" = lib.mkForce {
    enable = false;
    device = "none";
    fsType = "tmpfs";
  };
  systemd.services.register-nix-paths.enable = lib.mkForce false;

  # One `nix-daemon --stdio` per connection, as root, so the daemon trusts
  # it. vzvm forwards a loopback port on the host to this vsock port.
  systemd.sockets.nix-daemon-vsock = {
    wantedBy = [ "sockets.target" ];
    socketConfig = {
      ListenStream = "vsock::${toString daemonVsockPort}";
      Accept = true;
      # Past 64 connections systemd stops accepting, and clients hang.
      MaxConnections = 512;
      TriggerLimitIntervalSec = 0;
    };
  };
  systemd.services."nix-daemon-vsock@" = {
    requires = [ "nix-daemon.socket" ];
    after = [ "nix-daemon.socket" ];
    serviceConfig = {
      ExecStart = "${lib.getExe' config.nix.package "nix-daemon"} --stdio";
      StandardInput = "socket";
      StandardOutput = "socket";
      StandardError = "journal";
    };
  };

  # Each unit below runs on every boot of a VM that lives for one command.
  documentation.enable = false;
  documentation.nixos.enable = false;
  console.enable = false;
  services.getty.enable = false;
  systemd.oomd.enable = false;
  services.logrotate.enable = false;
  # The root is new on every boot, so systemd treats each boot as a first
  # boot and an update.
  environment.etc."machine-id".text = "70796e69786c696e75786275696c6400\n";
  systemd.services.systemd-update-done.enable = false;
  systemd.services.systemd-journal-catalog-update.enable = false;
  systemd.services.systemd-random-seed.enable = false;
  systemd.services.systemd-update-utmp.enable = false;
  systemd.generators = lib.genAttrs [
    "systemd-bless-boot-generator"
    "systemd-cryptsetup-generator"
    "systemd-factory-reset-generator"
    "systemd-getty-generator"
    "systemd-gpt-auto-generator"
    "systemd-hibernate-resume-generator"
    "systemd-imds-generator"
    "systemd-import-generator"
    "systemd-integritysetup-generator"
    "systemd-system-update-generator"
    "systemd-tpm2-generator"
    "systemd-veritysetup-generator"
  ] (_: "/dev/null");
}
