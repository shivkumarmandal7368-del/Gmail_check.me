# Vanguard MX — Gmail Bulk Checker

## Project Overview

pnpm monorepo. Gmail account validity checker with three modes:
- **SMTP** — basic MX/SMTP check (no credentials needed)
- **IMAP** — direct IMAP login check
- **Browser Check** — Selenium + undetected-chromedriver (Python) signs into Gmail via residential proxy with full fingerprint spoofing

## Architecture

```
artifacts/
  api-server/          → Express API on port 8080
    gmail_uc_checker.py          ← All Python Selenium browser automation
    src/lib/browserLoginChecker.ts  ← Node wrapper (concurrency, proxy rotation, sticky session)
    src/routes/emails.ts         ← Express routes (batch + SSE streaming)
  gmail-checker/       → React/Vite frontend on port 18726
    src/pages/home.tsx           ← Main UI (SMTP / IMAP / Browser tabs)
lib/
  api-zod/             → Zod schemas for API validation
  api-client-react/    → Generated React Query hooks
```

## How to Run

Workflows are pre-configured and start automatically:
- **Gmail Checker (frontend)**: `PORT=18726 BASE_PATH=/ pnpm --filter @workspace/gmail-checker run dev`
- **API Server**: `PORT=8080 pnpm --filter @workspace/api-server run dev`

### After a fresh import / clone

```bash
pnpm install
pip install -r artifacts/api-server/requirements.txt
```

Then restart both workflows.

## Key Dependencies

- **Python**: `undetected-chromedriver`, `selenium`, `pyotp`
- **Node**: Express 5, Vite 7, React, Zod, pnpm workspace

## Browser Check Notes

- Requires a **residential/mobile proxy** — Replit's datacenter IP is blocked by Google
- Enter proxy in the UI as: `http://user:pass@host:port`
- Sticky session IDs are injected automatically per account
- Chrome profiles stored at `/tmp/gmail_checker_profiles/<email>/`
- 28 Android phone fingerprint profiles for anti-detection

## User Preferences

_None recorded yet._
