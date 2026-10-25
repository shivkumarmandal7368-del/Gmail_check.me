#!/usr/bin/env bash
# Production API start — Chrome env + Node server. Used by systemd.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

if [[ -f "${ROOT}/deploy/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/deploy/.env"
  set +a
fi

export PORT="${PORT:-8080}"
export NODE_ENV="${NODE_ENV:-production}"

eval "$(bash "${ROOT}/scripts/setup-chrome-for-testing.sh")"

python3 -c 'import undetected_chromedriver, pyotp, selenium, requests' 2>/dev/null \
  || pip3 install --user -r "${ROOT}/artifacts/api-server/requirements.txt"

exec node --enable-source-maps "${ROOT}/artifacts/api-server/dist/index.mjs"
