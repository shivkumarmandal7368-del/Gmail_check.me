#!/usr/bin/env bash
# Vanguard MX — pull latest code, rebuild, restart ONLY vanguard-api + vanguard-web.
# Does NOT: apt upgrade, nginx, firewall, deploy/.env, or any other project on the VPS.
#
# Run on the VPS (as root or vanguard user with sudo for systemctl):
#   sudo bash scripts/vps-update.sh
#
# Optional:
#   VANGUARD_USER=vanguard
#   VANGUARD_REPO_DIR=/home/vanguard/vanguard-mx
#   VANGUARD_BRANCH=main

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"

APP_USER="${VANGUARD_USER:-vanguard}"
REPO_DIR="${VANGUARD_REPO_DIR:-${DEFAULT_REPO}}"
BRANCH="${VANGUARD_BRANCH:-main}"

if [[ ! -f "${REPO_DIR}/package.json" ]]; then
  echo "[vps-update] Repo not found at ${REPO_DIR}" >&2
  exit 1
fi

_run_as_app() {
  if [[ "$(id -un)" == "${APP_USER}" ]]; then
    bash -lc "$1"
  else
    sudo -u "${APP_USER}" bash -lc "$1"
  fi
}

echo "[vps-update] Repo: ${REPO_DIR}  branch: ${BRANCH}  user: ${APP_USER}"
echo "[vps-update] (Other apps on this VPS are not touched.)"

_run_as_app "
  set -euo pipefail
  cd '${REPO_DIR}'
  git fetch origin '${BRANCH}'
  git checkout '${BRANCH}'
  git pull --ff-only origin '${BRANCH}'
  pnpm install --frozen-lockfile 2>/dev/null || pnpm install
  if [[ -d .venv ]]; then
    .venv/bin/pip install -q -r artifacts/api-server/requirements.txt
  else
    pip3 install -q --user -r artifacts/api-server/requirements.txt || true
  fi
  eval \"\$(bash scripts/setup-chrome-for-testing.sh)\"
  pnpm --filter @workspace/api-server run build
  PORT=5173 BASE_PATH=/ pnpm --filter @workspace/gmail-checker run build
  git rev-parse HEAD > .deploy-version
  git log -1 --oneline | tee -a .deploy-version.log
"

if [[ "${EUID:-$(id -u)}" -eq 0 ]] || command -v systemctl >/dev/null; then
  SUDO=""
  [[ "${EUID:-$(id -u)}" -ne 0 ]] && SUDO="sudo"
  ${SUDO} systemctl restart vanguard-api vanguard-web
  sleep 2
  ${SUDO} systemctl is-active vanguard-api vanguard-web
  curl -sf "http://127.0.0.1:8080/api/proxy/status" >/dev/null && echo "[vps-update] API health OK" || echo "[vps-update] WARN: API health check failed"
else
  echo "[vps-update] Build done. Restart manually: sudo systemctl restart vanguard-api vanguard-web"
fi

echo "[vps-update] Done."
