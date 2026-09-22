# Nix deployment (prototype)

Builds the app as a derivation so it can be `nix copy`d to the carousel host and
run natively, instead of shipped as a container.

```sh
nix build .#default
./result/bin/discstakka-web            # honours DISCSTAKKA_DATA / _ART / _PORT

nix copy --to ssh://carousel .#default # then enable the module on that host
```

NixOS:

```nix
{
  imports = [ discstakka-web.nixosModules.default ];
  services.discstakka-web = { enable = true; openFirewall = true; };
}
```

## What this removes

Everything in `docker-compose.yml` that exists only to get a USB device into a
container: the `/dev/bus/usb` bind mount, `device_cgroup_rules` for major 189,
`group_add` matching the udev rule's GID, the rootful-Docker requirement, and
`docker-entrypoint.sh`, whose job is reporting permission problems that hidapi
cannot. Natively the unit is just a device on the host, and the udev rule is
declared next to the service that needs it.

## What it costs

**Dependencies come from nixpkgs, not `uv.lock`.** A store path has to be built,
not fetched from PyPI at runtime, so this is a second place versions are chosen.
They match today; nothing keeps them matching. `uv2nix` would close that by
building derivations from the lock, at the cost of another flake input.

**The hidapi build is different.** nixpkgs sets `HIDAPI_SYSTEM_HIDAPI` and links
against the system hidapi rather than the vendored copy in the PyPI wheel. The
whole device path assumes `hid` is the libusb backend, so `discstakka-web.nix`
asserts it with `ldd`, the way the Dockerfile asserts it by grepping symbols.
**That assertion has never run on hardware.** Nothing here has: it is built and
exercised on darwin, and only evaluated for Linux.

## Not done

- Building on Linux at all, let alone against a real carousel.
- `nix copy` to a host, and a reboot-survives test.
- Deciding whether this replaces the container or sits beside it.
