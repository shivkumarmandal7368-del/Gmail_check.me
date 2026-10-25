#!/usr/bin/env bash
# Vanguard MX — one-shot VPS bootstrap (Ubuntu 22.04 / 24.04, Debian 12).
# Run as root or with sudo:  curl -fsSL ... | bash   OR   sudo bash scripts/vps-install.sh
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/vps-install.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"

APP_USER="${VANGUARD_USER:-vanguard}"
APP_HOME="/home/${APP_USER}"
REPO_DIR="${VANGUARD_REPO_DIR:-${DEFAULT_REPO}}"
NODE_MAJOR="${VANGUARD_NODE_MAJOR:-20}"

echo "[vps-install] System packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  ca-certificates curl git unzip xvfb xauth nginx \
  fonts-liberation libatk-bridge2.0-0 libatk1.0-0 \
  libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 \
  libx11-xcb1 libxcomposite1 libxdamage1 libxfixes3 libxkbcommon0 libxrandr2 \
  libxi6 libxrender1 libxtst6 libpango-1.0-0 libcairo2 libglib2.0-0 \
  python3 python3-pip python3-venv python3-full
# libasound package name differs on 24.04 vs 22.04
apt-get install -y -qq libasound2t64 2>/dev/null || apt-get install -y -qq libasound2

if ! id "${APP_USER}" &>/dev/null; then
  echo "[vps-install] Creating user ${APP_USER}..."
  useradd -m -s /bin/bash "${APP_USER}"
fi

if [[ ! -f "${REPO_DIR}/package.json" ]]; then
  echo "[vps-install] Repo not found at ${REPO_DIR} — set VANGUARD_REPO_DIR or clone first." >&2
  exit 1
fi
chown -R "${APP_USER}:${APP_USER}" "${REPO_DIR}" 2>/dev/null || true

echo "[vps-install] Node.js ${NODE_MAJOR}..."
if ! command -v node >/dev/null 2>&1 || [[ "$(node -v | cut -d. -f1 | tr -d v)" -lt "${NODE_MAJOR}" ]]; then
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt-get install -y -qq nodejs
fi

echo "[vps-install] pnpm..."
if ! command -v pnpm >/dev/null 2>&1; then
  npm install -g pnpm@10
fi

echo "[vps-install] App dependencies (as ${APP_USER})..."
sudo -u "${APP_USER}" bash -lc "
  set -euo pipefail
  cd '${REPO_DIR}'
  pnpm install --frozen-lockfile
  pnpm run typecheck:libs || true
  python3 -m venv .venv
  .venv/bin/pip install -q setuptools
  .venv/bin/pip install -q -r artifacts/api-server/requirements.txt
  eval \"\$(bash scripts/setup-chrome-for-testing.sh)\"
  echo \"Chrome: \$(\"\${CHROME_BINARY}\" --version)\"
  pnpm --filter @workspace/api-server run build
  PORT=5173 BASE_PATH=/ pnpm --filter @workspace/gmail-checker run build
"

ENV_FILE="${REPO_DIR}/deploy/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${REPO_DIR}/deploy/env.example" "${ENV_FILE}"
  chown "${APP_USER}:${APP_USER}" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  echo "[vps-install] Edit secrets: nano ${ENV_FILE}"
fi

echo "[vps-install] systemd units..."
install -m 644 "${REPO_DIR}/deploy/systemd/vanguard-api.service" /etc/systemd/system/
install -m 644 "${REPO_DIR}/deploy/systemd/vanguard-web.service" /etc/systemd/system/
sed -i "s|__APP_USER__|${APP_USER}|g; s|__REPO_DIR__|${REPO_DIR}|g" /etc/systemd/system/vanguard-api.service
sed -i "s|__APP_USER__|${APP_USER}|g; s|__REPO_DIR__|${REPO_DIR}|g" /etc/systemd/system/vanguard-web.service

systemctl daemon-reload
systemctl enable vanguard-api vanguard-web

if [[ -f "${REPO_DIR}/deploy/nginx/vanguard.conf" ]]; then
  install -m 644 "${REPO_DIR}/deploy/nginx/vanguard.conf" /etc/nginx/sites-available/vanguard
  ln -sf /etc/nginx/sites-available/vanguard /etc/nginx/sites-enabled/vanguard
  # Do not remove default or other sites — Vanguard listens on 8888 only
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl reload nginx
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q active; then
    ufw allow 8888/tcp || true
  fi
fi

echo ""
echo "=== Done ==="
echo "1. Edit: ${ENV_FILE}  (PROXY_URL, SESSION_SECRET, optional DOMAIN)"
echo "2. Start: systemctl start vanguard-api vanguard-web"
echo "3. Logs:  journalctl -u vanguard-api -f"
echo "4. UI:    http://YOUR_SERVER_IP/  (nginx → web on 5173, API on 8080)"
