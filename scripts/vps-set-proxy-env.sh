#!/usr/bin/env bash
# Update PROXY_URL in deploy/.env and restart Vanguard services.
# Usage (on VPS): sudo bash scripts/vps-set-proxy-env.sh 'http://user:pass@host:port'
set -euo pipefail

PROXY="${1:-}"
if [[ -z "${PROXY}" ]]; then
  echo "Usage: $0 'http://USER:PASS@host:port'" >&2
  exit 1
fi
if [[ "${PROXY}" != http://* ]]; then
  echo "PROXY_URL must start with http:// (got: ${PROXY:0:20}...)" >&2
  exit 1
fi

REPO_DIR="${VANGUARD_REPO_DIR:-/home/vanguard/vanguard-mx}"
ENV_FILE="${REPO_DIR}/deploy/.env"
APP_USER="${VANGUARD_USER:-vanguard}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  exit 1
fi

# Escape for sed (| delimiter)
ESCAPED=$(printf '%s' "${PROXY}" | sed 's/[|&\\]/\\&/g')

if grep -q '^PROXY_URL=' "${ENV_FILE}"; then
  sed -i "s|^PROXY_URL=.*|PROXY_URL=${ESCAPED}|" "${ENV_FILE}"
else
  echo "PROXY_URL=${PROXY}" >> "${ENV_FILE}"
fi

if ! grep -q '^PROXY_PHONE_MODE=' "${ENV_FILE}"; then
  echo "PROXY_PHONE_MODE=1" >> "${ENV_FILE}"
fi

chmod 600 "${ENV_FILE}"
chown "${APP_USER}:${APP_USER}" "${ENV_FILE}"

systemctl restart vanguard-api vanguard-web
systemctl is-active vanguard-api vanguard-web

echo "==> PROXY_URL updated (host only): $(python3 - <<'PY'
from urllib.parse import urlparse
import os
u = urlparse(os.environ.get("PROXY", ""))
print(f"{u.hostname}:{u.port or 80}")
PY
PROXY="${PROXY}")"

curl -sf -X POST http://127.0.0.1:8080/api/proxy/check -H 'Content-Type: application/json' -d '{"useSaved":true}' | python3 -m json.tool
