# Vanguard MX — Gmail Bulk Checker

A pnpm monorepo with a React frontend and Express API server for bulk Gmail verification using SMTP, IMAP, and browser automation.

## How to Run

Two workflows run in parallel (start via the **▶ Project** button):

| Workflow | Command | Port |
|---|---|---|
| `Gmail Checker (frontend)` | `pnpm --filter @workspace/gmail-checker run dev` | 18726 |
| `API Server` | `pnpm --filter @workspace/api-server run dev` | 8080 |

## Architecture

```
artifacts/
  api-server/                        ← Express API (TypeScript, esbuild)
    gmail_uc_checker.py              ← Python Selenium browser automation
    src/lib/browserLoginChecker.ts   ← Node wrapper spawning Python per account
    src/lib/imapChecker.ts           ← IMAP login checker
    src/lib/emailVerifier.ts         ← SMTP MX verifier
    src/routes/emails.ts             ← API routes
  gmail-checker/                     ← React + Vite + Tailwind frontend
    src/pages/home.tsx               ← Main UI (SMTP / IMAP / Browser tabs)
lib/
  api-zod/                           ← Shared Zod schemas
  api-client-react/                  ← Generated React Query hooks
```

## Check Modes

1. **SMTP Check** — MX/SMTP handshake, no credentials needed
2. **IMAP Check** — Direct IMAP login, requires email + password
3. **Browser Check** — Selenium + undetected-chromedriver signs into Gmail
   - Requires a residential/mobile proxy (Replit datacenter IP is blocked by Google)
   - 28 real Android phone fingerprint profiles (antidetect)
   - Sticky proxy sessions per account
   - TOTP/2FA auto-entry via pyotp
   - Concurrent checking (1–10 threads)

## Python Dependencies

```bash
pip install -r artifacts/api-server/requirements.txt
```

Packages: `undetected-chromedriver`, `selenium`, `pyotp`, `requests`

## Test the API Directly

```bash
curl -s -X POST http://localhost:8080/api/emails/browser-check \
  -H "Content-Type: application/json" \
  --max-time 300 \
  -d '{
    "credentials":[{"email":"user@gmail.com","password":"pass","totp":"BASE32SECRET"}],
    "proxy":"http://user:pass@rp.example.com:6060",
    "concurrency":2,
    "freshProfile":true
  }'
```

## Important Notes

- **Browser Check requires a residential proxy** — datacenter IPs are blocked by Google
- **Chrome launch lock** — a cross-process `fcntl` file lock (`/tmp/gmail_checker_chrome_launch.lock`) serializes Chrome launches to prevent OOM crashes when running concurrent accounts
- **Fingerprints** are saved to `/tmp/gmail_checker_profiles/<email>/fingerprint.json`; `freshProfile: true` wipes them before each run
- Chromium is provided by Nix (`nixpkgs.geckodriver` entry; actual Chromium resolved via `which chromium`)

## User Preferences

- Keep the existing project structure and stack
