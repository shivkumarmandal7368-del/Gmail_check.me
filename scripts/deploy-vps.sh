#!/usr/bin/env bash
# Vanguard MX — one-shot VPS install (Ubuntu 22.04 / 24.04)
# Does NOT modify other apps. Default install dir: /opt/vanguard-mx-browser
#
# Usage (on your VPS as root):
#   curl -fsSL https://raw.githubusercontent.com/shivkumarmandal7368-del/Gmail_check.me/main/scripts/deploy-vps.sh | bash
# Or after git clone:
#   sudo bash scripts/deploy-vps.sh
#
# Optional env:
#   VANGUARD_INSTALL_DIR=/opt/vanguard-mx-browser
#   VANGUARD_WEB_PORT=8088          # UI (+ /api proxy via vite preview)
#   VANGUARD_API_PORT=8080            # internal API (do not expose publicly if possible)
#   PROXY_URL=http://user:pass@host:port   # residential/mobile proxy for Browser Check

set -euo pipefail

INSTALL_DIR="${VANGUARD_INSTALL_DIR:-/opt/vanguard-mx-browser}"
WEB_PORT="${VANGUARD_WEB_PORT:-8088}"
API_PORT="${VANGUARD_API_PORT:-8080}"
REPO_URL="${VANGUARD_REPO_URL:-https://github.com/shivkumarmandal7368-del/Gmail_check.me.git}"
BRANCH="${VANGUARD_BRANCH:-main}"
ENV_FILE="/etc/vanguard-mx.env"

echo "==> Vanguard MX VPS deploy"
echo "    Install dir: ${INSTALL_DIR}"
echo "    Web UI port: ${WEB_PORT} (http://YOUR_VPS_IP:${WEB_PORT})"
echo "    API port:    ${API_PORT} (localhost only recommended)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  git curl ca-certificates \
  python3 python3-pip python3-venv \
  xvfb xdotool unzip \
  build-essential

# Node 20 via NodeSource (skip if node >= 20 already)
if ! command -v node >/dev/null 2>&1 || [[ "$(node -p process.versions.node.split('.')[0])" -lt 20 ]]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y -qq nodejs
fi

corepack enable
corepack prepare pnpm@10.33.4 --activate

if [[ ! -d "${INSTALL_DIR}/.git" ]]; then
  git clone --branch "${BRANCH}" --depth 1 "${REPO_URL}" "${INSTALL_DIR}"
else
  git -C "${INSTALL_DIR}" fetch origin "${BRANCH}"
  git -C "${INSTALL_DIR}" checkout "${BRANCH}"
  git -C "${INSTALL_DIR}" pull --ff-only origin "${BRANCH}"
fi

cd "${INSTALL_DIR}"
pnpm install --frozen-lockfile 2>/dev/null || pnpm install
pip3 install -q -r artifacts/api-server/requirements.txt

# Chrome for Testing + env exports
eval "$(bash scripts/setup-chrome-for-testing.sh)"

if [[ ! -f "${ENV_FILE}" ]]; then
  touch "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
fi
{
  echo "# Vanguard MX — edit and restart: systemctl restart vanguard-mx-api vanguard-mx-web"
  echo "PORT=${API_PORT}"
  echo "PROXY_URL=${PROXY_URL:-}"
  echo "CHROME_BINARY=${CHROME_BINARY:-}"
  echo "CHROME_FOR_TESTING_ROOT=${CHROME_FOR_TESTING_ROOT:-}"
  echo "LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}"
  echo "CHROME_VERSION_MAIN=${CHROME_VERSION_MAIN:-}"
  echo "CHROME_VERSION_FULL=${CHROME_VERSION_FULL:-}"
} >"${ENV_FILE}.new"
mv "${ENV_FILE}.new" "${ENV_FILE}"
if [[ -z "${PROXY_URL:-}" ]]; then
  echo "==> Wrote ${ENV_FILE} — PROXY_URL empty (OK for first boot). UI/API will run; Gmail checks may show VERIFY until you add proxy later."
else
  echo "==> Wrote ${ENV_FILE} — PROXY_URL set for Browser Check"
fi

pnpm exec tsc --build || true
pnpm --filter @workspace/api-server run build
PORT="${WEB_PORT}" BASE_PATH=/ API_PORT="${API_PORT}" NODE_ENV=production \
  pnpm --filter @workspace/gmail-checker run build

# systemd — API
cat >/etc/systemd/system/vanguard-mx-api.service <<EOF
[Unit]
Description=Vanguard MX API (Browser Check backend)
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}/artifacts/api-server
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/node --enable-source-maps ${INSTALL_DIR}/artifacts/api-server/dist/index.mjs
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# systemd — Web (production preview + /api proxy)
cat >/etc/systemd/system/vanguard-mx-web.service <<EOF
[Unit]
Description=Vanguard MX Web UI
After=network.target vanguard-mx-api.service
Requires=vanguard-mx-api.service

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}/artifacts/gmail-checker
Environment=PORT=${WEB_PORT}
Environment=BASE_PATH=/
Environment=API_PORT=${API_PORT}
Environment=NODE_ENV=production
ExecStart=$(command -v pnpm) run preview
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable vanguard-mx-api vanguard-mx-web
systemctl restart vanguard-mx-api vanguard-mx-web

sleep 2
if curl -sf "http://127.0.0.1:${API_PORT}/api/proxy/status" >/dev/null; then
  echo "==> API OK on port ${API_PORT}"
else
  echo "==> WARNING: API health check failed — run: journalctl -u vanguard-mx-api -n 50"
fi
if curl -sf "http://127.0.0.1:${WEB_PORT}/" >/dev/null; then
  echo "==> Web UI OK"
else
  echo "==> WARNING: Web UI check failed — run: journalctl -u vanguard-mx-web -n 50"
fi

echo ""
echo "==> Done. Open in browser:"
echo "    http://$(curl -sf ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}'):${WEB_PORT}"
echo ""
echo "    Lock down firewall (recommended — only your IP):"
echo "    ufw allow from YOUR_HOME_IP to any port ${WEB_PORT} proto tcp"
echo "    ufw enable"
echo ""
echo "    Logs: journalctl -u vanguard-mx-api -f"
echo "          journalctl -u vanguard-mx-web -f"
