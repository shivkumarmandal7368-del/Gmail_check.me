{pkgs}: {
  deps = [
    pkgs.xorg.xorgserver
    pkgs.xvfb-run
    pkgs.dbus
    pkgs.glib
    pkgs.cairo
    pkgs.pango
    pkgs.alsa-lib
    pkgs.expat
    pkgs.mesa
    pkgs.libxkbcommon
    pkgs.xorg.libXi
    pkgs.xorg.libXrandr
    pkgs.xorg.libXfixes
    pkgs.xorg.libXext
    pkgs.xorg.libXdamage
    pkgs.xorg.libXcomposite
    pkgs.xorg.libX11
    pkgs.xorg.libxcb
    pkgs.libdrm
    pkgs.cups
    pkgs.atk
    pkgs.nspr
    pkgs.nss
    pkgs.chromium
  ];
}
