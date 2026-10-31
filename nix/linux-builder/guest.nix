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
  options,
  lib,
  pkgs,
  daemonVsockPort,
  sshVsockPort,
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
    # Harmless without nested virtualization: /dev/kvm only appears when
    # vzvm boots the guest at EL2.
    "kvm"
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
    # NixOS lists `kvm` whether or not /dev/kvm exists. pynix-kvm-feature
    # adds it when the guest really has it.
    # mkForce: nix.nix defines the list too, and lists concatenate.
    system-features = lib.mkForce (
      lib.remove "kvm" (options.nix.settings.type.getSubOptions [ ]).system-features.default
    );
  };
  nix.extraOptions = "!include /run/pynix/nix.conf";

  # `kvm` here means aarch64 guests only. Nix features belong to the daemon,
  # not to a system, so an x86_64-linux derivation that requires `kvm` is
  # accepted too, and its qemu gets no acceleration.
  systemd.services.pynix-kvm-feature = {
    wantedBy = [ "nix-daemon.service" ];
    before = [ "nix-daemon.service" ];
    after = [ "systemd-modules-load.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      mkdir -p /run/pynix
      if [ -e /dev/kvm ]; then
        echo "extra-system-features = kvm" > /run/pynix/nix.conf
      fi
    '';
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

  # `pynix run --linux-builder` runs its program over ssh (nixidae/nixidae#1147).
  # The runner makes a key pair per run and shares the public half; a build
  # shares none, so nothing can log in.
  #
  # Our own vsock socket, as for the daemon, and not systemd-ssh-generator's
  # (turned off below): with the generator, ssh connected and no sshd
  # answered.
  systemd.sockets.pynix-sshd-vsock = {
    wantedBy = [ "sockets.target" ];
    socketConfig = {
      ListenStream = "vsock::${toString sshVsockPort}";
      Accept = true;
      TriggerLimitIntervalSec = 0;
    };
  };
  systemd.services."pynix-sshd-vsock@" = {
    serviceConfig = {
      ExecStart = "-${lib.getExe' config.services.openssh.package "sshd"} -i -f /etc/ssh/sshd_config";
      KillMode = "process";
      StandardInput = "socket";
      StandardError = "journal";
    };
  };
  services.openssh = {
    enable = true;
    startWhenNeeded = true;
    # The key is fixed, below. With `hostKeys = [ ]` NixOS still writes
    # sshd-keygen.service, empty; systemd refuses to load it, and the
    # per-connection sshd that waits for it never sent its banner.
    generateHostKeys = false;
    hostKeys = lib.mkForce [
      {
        type = "ed25519";
        path = "/etc/ssh/ssh_host_ed25519_key";
      }
    ];
    settings = {
      PasswordAuthentication = false;
      PermitRootLogin = "prohibit-password";
      # virtiofs shows the share's files as uid 1000, and StrictModes refuses
      # an authorized_keys file that root does not own. No guest process can
      # write the read-only share.
      StrictModes = false;
      # vsock delivers no RST, so a session whose peer is gone never ends
      # without keepalives.
      ClientAliveInterval = 60;
      ClientAliveCountMax = 3;
    };
    authorizedKeysFiles = lib.mkForce [ "/run/pynix-keys/authorized_keys" ];
  };
  # nixpkgs' fixed builder key: the manifest carries its public half, so the
  # host checks the guest with no prompt and nothing in a known_hosts file.
  # `${modulesPath}/...` here is a string with no store context: nothing
  # copied the file into a store, so it was missing in the guest and sshd
  # exited with no host key. readFile and writeText make a real store path.
  # A unit installs it, because sshd refuses a 0444 host key.
  systemd.services.pynix-ssh-host-key = {
    unitConfig.DefaultDependencies = false;
    wantedBy = [ "sockets.target" ];
    after = [ "local-fs.target" ];
    before = [ "pynix-sshd-vsock.socket" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      ${pkgs.coreutils}/bin/install -D -m 0600 ${pkgs.writeText "ssh_host_ed25519_key" (builtins.readFile "${modulesPath}/profiles/keys/ssh_host_ed25519_key")} /etc/ssh/ssh_host_ed25519_key
    '';
  };
  security.pam.services.sshd.startSession = lib.mkForce false;
  fileSystems."/run/pynix-keys" = {
    device = "pynix-keys";
    fsType = "virtiofs";
    options = [
      "ro"
      "nofail"
    ];
  };

  # The host's $HOME, at the same path, so the caller's directory and HOME
  # mean the same in here. The path comes on the kernel command line,
  # because it differs per user and the guest is the same for all.
  systemd.services.pynix-home = {
    # Out of the default dependencies: those order a service after
    # basic.target, which is after sockets.target, and `before` the ssh
    # socket closed that into a cycle. systemd broke it by dropping the
    # socket, so ssh found nothing on vsock:22.
    unitConfig.DefaultDependencies = false;
    wantedBy = [ "sockets.target" ];
    after = [ "local-fs.target" ];
    before = [ "pynix-sshd-vsock.socket" ];
    path = [
      pkgs.util-linux
      pkgs.coreutils
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      for word in $(cat /proc/cmdline); do
        case "$word" in pynix.home=*) home=''${word#pynix.home=} ;; esac
      done
      [ -n "''${home-}" ] || exit 0
      mkdir -p "$home"
      mount -t virtiofs pynix-home "$home"
    '';
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
    "systemd-ssh-generator"
    "systemd-system-update-generator"
    "systemd-tpm2-generator"
    "systemd-veritysetup-generator"
  ] (_: "/dev/null");
}
