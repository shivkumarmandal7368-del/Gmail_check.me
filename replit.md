# Vanguard MX

Email verification tool with three modes: SMTP check, IMAP login check, and browser-based Gmail login check.

## Run & Operate

- Frontend (Vite dev server): `PORT=18726 BASE_PATH=/ pnpm --filter @workspace/gmail-checker run dev`
- API server (Express): `PORT=8080 pnpm --filter @workspace/api-server run dev`
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `DATABASE_URL` is auto-provided by Replit's built-in PostgreSQL (runtime-managed)

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `artifacts/gmail-checker/` — React + Vite frontend (port 18726)
- `artifacts/api-server/` — Express 5 API server (port 8080)
- `artifacts/api-server/src/lib/browserLoginChecker.ts` — Puppeteer-based Gmail browser login checker
- `artifacts/api-server/src/lib/imapChecker.ts` — IMAP credential checker
- `artifacts/api-server/src/lib/emailVerifier.ts` — SMTP email verifier
- `artifacts/api-server/src/routes/emails.ts` — all three check endpoints
- `artifacts/gmail-checker/src/pages/home.tsx` — main UI (SMTP / IMAP / Browser Check tabs)
- `scripts/post-merge.sh` — runs `pnpm install --frozen-lockfile` + DB push on merge

## Architecture decisions

- Browser Check uses Puppeteer + puppeteer-extra-plugin-stealth against Chromium from Nix store. Google blocks Replit's datacenter IP, so a **residential proxy is required** for browser check to work.
- API is built with esbuild to `dist/index.mjs` before starting (no ts-node in prod or dev).
- All three check modes share a single Express router; the frontend switches between them client-side.
- Orval codegen generates typed React Query hooks from the OpenAPI spec — run `pnpm --filter @workspace/api-spec run codegen` after spec changes.

## Product

Three email verification modes accessible from one UI:
1. **SMTP Check** — verifies email address existence via SMTP handshake (no credentials needed)
2. **IMAP Check** — tests Gmail credentials via IMAP login (app password required)
3. **Browser Check** — uses a real Chromium browser to log into Gmail (supports TOTP; requires residential proxy on Replit)

Results can be filtered and downloaded as `.txt` lists.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

- **Browser Check requires a residential proxy on Replit** — Replit's datacenter IP is blocked by Google. Without a proxy all accounts return `verification_required`.
- Chromium path is resolved via `which chromium` with a Nix store fallback hardcoded in `browserLoginChecker.ts` — if Chromium version changes, update that path.
- `pnpm install` must be run after cloning/importing before workflows will start (deps not committed).
- Browser Check is sequential (~20-40s per account); long lists block the endpoint for the full duration.
- **"Couldn't sign you in — not be secure" error** = UA-CH mismatch. Fixed via `Network.setUserAgentOverride` with `userAgentMetadata` (sets `Sec-CH-UA` HTTP headers to Android) + `navigator.userAgentData` spoof in STEALTH_JS. If this error recurs, the persistent Chrome profile for that account is auto-wiped and the next attempt starts fresh.
- **`--user-agent` flag alone is not enough** — it changes `navigator.userAgent` but NOT `Sec-CH-UA` / `Sec-CH-UA-Mobile` / `Sec-CH-UA-Platform` HTTP headers. The CDP `Network.setUserAgentOverride` call with `userAgentMetadata` is required to align both.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
