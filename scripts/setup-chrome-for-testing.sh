#!/usr/bin/env bash
set -euo pipefail

# Chrome for Testing is intentionally bootstrapped from Google's signed public
# manifest rather than relying on the older Chromium package in replit.nix.
# The cache lives outside the repository, and this script is run by the API
# workflow on every boot; a missing cache is therefore self-healing.

cache_root="${CHROME_FOR_TESTING_ROOT:-${HOME}/.cache/vanguard-mx/chrome-for-testing}"
manifest_url="https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
mkdir -p "${cache_root}"

read -r chrome_version chrome_url < <(
  python3 - "${manifest_url}" <<'PY'
import json
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=20) as response:
    manifest = json.load(response)

stable = manifest["channels"]["Stable"]
version = stable["version"]
url = next(
    item["url"]
    for item in stable["downloads"]["chrome"]
    if item["platform"] == "linux64"
)
print(version, url)
PY
)

chrome_dir="${cache_root}/${chrome_version}"
chrome_binary="${chrome_dir}/chrome-linux64/chrome"
if [[ ! -x "${chrome_binary}" ]]; then
  archive="${cache_root}/chrome-${chrome_version}.zip"
  temporary_dir="${cache_root}/.install-${chrome_version}-$$"
  rm -rf "${temporary_dir}"
  mkdir -p "${temporary_dir}"
  echo "[chrome-setup] Downloading Chrome for Testing ${chrome_version}" >&2
  curl --fail --location --retry 3 --retry-delay 2 --output "${archive}.part" "${chrome_url}"
  mv "${archive}.part" "${archive}"
  unzip -q -o "${archive}" -d "${temporary_dir}"
  rm -rf "${chrome_dir}"
  mv "${temporary_dir}" "${chrome_dir}"
  rm -f "${archive}"
fi

ln -sfn "${chrome_dir}/chrome-linux64" "${cache_root}/current"
chrome_binary="${cache_root}/current/chrome"

# Chrome for Testing is not wrapped by Nix, so explicitly provide the runtime
# library closure needed by the unwrapped binary.
library_path="$(
  nix eval --raw --impure --expr '
    let pkgs = import <nixpkgs> {};
    in pkgs.lib.makeLibraryPath [
      pkgs.glib
      pkgs.nss
      pkgs.nspr
      pkgs.atk
      pkgs.at-spi2-atk
      pkgs.cups
      pkgs.dbus
      pkgs.libdrm
      pkgs.libgbm
      pkgs.mesa
      pkgs.systemd
      pkgs.pango
      pkgs.cairo
      pkgs.gtk3
      pkgs.alsa-lib
      pkgs.libxkbcommon
      pkgs.xorg.libXi
      pkgs.xorg.libXrandr
      pkgs.xorg.libXfixes
      pkgs.xorg.libXext
      pkgs.xorg.libXdamage
      pkgs.xorg.libXcomposite
      pkgs.xorg.libX11
      pkgs.xorg.libxcb
      pkgs.xorg.libXrender
      pkgs.xorg.libXtst
      pkgs.expat
    ]
  '
)"

chrome_major="${chrome_version%%.*}"
runtime_ld_library_path="${library_path}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

# stdout is deliberately shell-sourceable; diagnostics stay on stderr.
printf 'export CHROME_BINARY=%q\n' "${chrome_binary}"
printf 'export CHROME_VERSION_MAIN=%q\n' "${chrome_major}"
printf 'export LD_LIBRARY_PATH=%q\n' "${runtime_ld_library_path}"
printf 'export CHROME_FOR_TESTING_ROOT=%q\n' "${cache_root}"
echo "[chrome-setup] Ready: $(LD_LIBRARY_PATH="${runtime_ld_library_path}" "${chrome_binary}" --version)" >&2