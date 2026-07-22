# Vanguard MX — Gmail Bulk Checker

## Project Overview

**Vanguard MX** is a Gmail account bulk checker with three modes:
- **SMTP** — basic MX/SMTP check (no credentials needed)
- **IMAP** — direct IMAP login check
- **Browser Check** — main feature: Selenium + undetected-chromedriver (Python), signs into Gmail via residential proxy with full antidetect fingerprinting

## Architecture

pnpm monorepo with two artifacts:

| Artifact | Tech | Port |
|---|---|---|
| `artifacts/gmail-checker` | React + Vite frontend | 5173 |
| `artifacts/api-server` | Express (Node) + Python Selenium backend | 8080 |

Key files:
- `artifacts/api-server/gmail_uc_checker.py` — All Python Selenium browser automation (~2200 lines)
- `artifacts/api-server/src/lib/browserLoginChecker.ts` — Node wrapper: spawns Python, concurrency, sticky session
- `artifacts/api-server/src/routes/emails.ts` — Express routes (SSE stream endpoint)
- `artifacts/gmail-checker/src/pages/home.tsx` — Full React frontend

## How to Run

Both workflows are pre-configured and start automatically:

- **Frontend:** `artifacts/gmail-checker: web` → `PORT=5173 BASE_PATH=/ pnpm --filter @workspace/gmail-checker run dev`
- **API Server:** `artifacts/api-server: API Server` → `PORT=8080 pnpm --filter @workspace/api-server run dev`

If workflows fail after a fresh import, restart both. If deps are missing:
```bash
pnpm install
pip install -r artifacts/api-server/requirements.txt
```

## Environment / Secrets

No Replit secrets required. The proxy password is entered manually in the UI each time.

**Proxy format (enter in UI):**
```
http://USERNAME:PASSWORD@rp.scrapegw.com:6060
```

## Credential Format

```
email:password
email:password:BASE32_TOTP_SECRET
```

The 3rd field is the base32 TOTP secret (from Google Authenticator app setup), NOT an app password.

## Known Issues

- **Concurrent Chrome crash (UNRESOLVED):** When 2+ accounts check simultaneously, one may fail with `Connection refused` due to OOM. Fix documented in `HANDOFF.md` Session 17. Workaround: use concurrency=1.
- Browser Check requires a **residential proxy** — Replit's datacenter IP is blocked by Google.
- Each check takes 35–120 seconds per account (intentional human-like delays).

## User Preferences

- Keep the project's existing stack and structure.
- HANDOFF.md is the source of truth for session history and open bugs — update it after each session.
