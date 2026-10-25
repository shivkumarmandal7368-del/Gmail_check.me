#!/usr/bin/env bash
# Production web — Vite preview (built assets + /api proxy to localhost:8080).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}/artifacts/gmail-checker"

if [[ -f "${ROOT}/deploy/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/deploy/.env"
  set +a
fi

export PORT="${WEB_PORT:-5173}"
export API_PORT="${API_PORT:-8080}"
export BASE_PATH="${BASE_PATH:-/}"
export NODE_ENV=production

if [[ ! -d dist/public ]]; then
  echo "Missing build — run: pnpm --filter @workspace/gmail-checker run build" >&2
  exit 1
fi

exec pnpm exec vite preview --config vite.config.ts --host 0.0.0.0 --port "${PORT}"
