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

PYTHON="${ROOT}/.venv/bin/python3"
if [[ ! -x "${PYTHON}" ]]; then
  python3 -m venv "${ROOT}/.venv"
  "${ROOT}/.venv/bin/pip" install -q -r "${ROOT}/artifacts/api-server/requirements.txt"
  PYTHON="${ROOT}/.venv/bin/python3"
fi
"${PYTHON}" -c 'import undetected_chromedriver, pyotp, selenium, requests' 2>/dev/null \
  || "${ROOT}/.venv/bin/pip" install -q -r "${ROOT}/artifacts/api-server/requirements.txt"

export PATH="${ROOT}/.venv/bin:${PATH}"

exec node --enable-source-maps "${ROOT}/artifacts/api-server/dist/index.mjs"
