# The application as a derivation.
#
# Dependencies come from nixpkgs rather than uv.lock here, because a store path
# has to be built, not downloaded from PyPI at runtime. That is the trade this
# route makes: `nix copy` to the host and no container, in exchange for a second
# place versions are chosen. They happen to match today.
{ lib, stdenvNoCC, makeWrapper, python314, python314Packages }:

let
  runtime = python314.withPackages (ps: with ps; [
    flask
    pillow
    requests
    hidapi
  ]);
in
stdenvNoCC.mkDerivation {
  pname = "discstakka-web";
  version = "0.1.0";

  src = lib.cleanSource ../.;

  nativeBuildInputs = [ makeWrapper ];

  # app.py has to keep templates/ and static/ as siblings: Flask derives both
  # from the importing module's location.
  installPhase = ''
    runHook preInstall

    mkdir -p $out/share/discstakka-web
    cp -r app.py discstakka templates static $out/share/discstakka-web/

    makeWrapper ${runtime}/bin/python $out/bin/discstakka-web \
      --add-flags $out/share/discstakka-web/app.py

    runHook postInstall
  '';

  # The same check the container build makes, for the same reason: the unit is
  # reached through usbfs, which only works while `hid` is the libusb backend.
  # nixpkgs builds against the system hidapi rather than the wheel's vendored
  # copy, so this is a different build and needs asserting independently.
  doInstallCheck = true;
  installCheckPhase = lib.optionalString stdenvNoCC.hostPlatform.isLinux ''
    so="$(${runtime}/bin/python -c 'import hid; print(hid.__file__)')"
    if ldd "$so" | grep -q 'libhidapi-libusb'; then
      echo "hid backend: libusb, needs /dev/bus/usb"
    else
      echo "hid is not linked against libhidapi-libusb; the udev rule and the" >&2
      echo "usbfs assumptions in README.md would not hold. Check the backend." >&2
      ldd "$so" >&2
      exit 1
    fi
  '';

  meta = {
    description = "Web catalogue for the Imation Disc Stakka USB CD carousel";
    license = lib.licenses.gpl2Plus;
    mainProgram = "discstakka-web";
    platforms = lib.platforms.unix;
  };
}
