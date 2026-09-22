# NixOS module: the app as a systemd service.
#
# What the container needs and this does not: a /dev/bus/usb bind mount, a
# device cgroup rule for major 189, group_add to match the udev rule's GID,
# rootful Docker, and an entrypoint that checks permissions because hidapi
# loses the errno. Here the unit is just a device on the host, the udev rule is
# declared beside the service that needs it, and systemd owns the state
# directory, the user and the restart policy.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.discstakka-web;
in
{
  options.services.discstakka-web = {
    enable = lib.mkEnableOption "the Disc Stakka web catalogue";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./discstakka-web.nix { };
      description = "The discstakka-web package to run.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 5050;
      description = "Port to serve on. Not 5000: that is AirPlay on macOS.";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the port, so the PS3 on the LAN can reach it.";
    };
  };

  config = lib.mkIf cfg.enable {
    users.groups.discstakka = { };
    users.users.discstakka = {
      isSystemUser = true;
      group = "discstakka";
    };

    # The same rule as deploy/99-discstakka.rules, declared rather than copied
    # in by hand. On the USB device and not on hidraw, because `hid` is the
    # libusb backend and the app opens /dev/bus/usb/<bus>/<dev>.
    services.udev.extraRules = ''
      SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="0718", ATTR{idProduct}=="d000", MODE="0660", GROUP="discstakka"
    '';

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];

    systemd.services.discstakka-web = {
      description = "Disc Stakka web catalogue";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];

      environment = {
        DISCSTAKKA_PORT = toString cfg.port;
        DISCSTAKKA_DATA = "/var/lib/discstakka-web";
        # Cover art is user data and the package is read-only, so it cannot
        # live in the shipped static folder the way it does in a checkout.
        DISCSTAKKA_ART = "/var/lib/discstakka-web/art";
        # db.py stamps rows with a naive datetime.now(); without this the pages
        # present UTC as local. `or` would not do: time.timeZone exists and
        # defaults to null, so it never falls through.
        TZ = if config.time.timeZone != null then config.time.timeZone else "UTC";
      };

      serviceConfig = {
        ExecStart = lib.getExe cfg.package;
        User = "discstakka";
        Group = "discstakka";
        StateDirectory = "discstakka-web";
        Restart = "on-failure";

        # One owner at a time: the HID handle and the job registry live in
        # memory, so a second copy would fight this one for the device.
        Type = "exec";

        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        NoNewPrivileges = true;
        # Not PrivateDevices: that hides /dev/bus/usb, which is the unit.
      };
    };
  };
}
