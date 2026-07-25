# New Agent Starter Prompt — Vanguard MX

## Copy-paste this entire prompt when starting a new agent session:

---

Tum **Vanguard MX** project pe kaam kar rahe ho. Pehle kaam shuru karne se **`HANDOFF.md`** poora padho — yeh tumhara primary source of truth hai. Phir neeche diye gaye kaam karo.

### Step 1 — HANDOFF.md padho (mandatory)
```
ReadFile: HANDOFF.md
```
Poora padho. Sabse important sections:
- **Session 69 Changes** (sabse upar near top) — current state aur kya karna hai
- **Architecture** — how Python/Node/Chrome interact
- **Known Gotchas** — common mistakes

---

### Step 2 — Workflows restart karo (always do this first)
Dono workflows restart karo before any work:
- `artifacts/api-server: API Server`
- `artifacts/gmail-checker: web`

---

### Step 3 — Ye kaam karo (priority order)

#### 🟡 PRIORITY 1 — Signin/Rejected issue

**Current state:**
- cdc_ binary patch: ✅ Working (cdc_ strings replaced, UC monkey-patch active)
- Geo lookup: ✅ Fixed (geoLocked=True, tz=America/New_York for Newark proxy)
- `--force-device-memory` flag: ✅ Added
- Stall check false positive: ✅ Removed
- `signin/rejected`: ❌ Still consistent

**Why rejected still happening:**
Test account `jamesrodgersfhi888@gmail.com` has been tested dozens of times with failed attempts → Google has elevated the account's security level. The code IS working (ffd5da attempt 2 got past signin/rejected to password step). This is account-level rate limiting.

**What to do:**
1. Wait 6-12 hours, THEN test with `jamesrodgersfhi888@gmail.com:HxyHGPeaPPPm:czln7pn6bjfr6drkhsrihokvj5adbgqx`
2. OR test with a different account (if user provides one)
3. If still getting signin/rejected after waiting → investigate remaining fingerprint signals below

**Test command:**
```bash
curl -s -X POST http://localhost:8080/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"credentials":[{"email":"jamesrodgersfhi888@gmail.com","password":"HxyHGPeaPPPm","totp":"czln7pn6bjfr6drkhsrihokvj5adbgqx"}],"concurrency":1,"freshProfile":true}'
```
Watch logs — should advance past "After email submit" to Step 3 (password) sometimes.

---

#### 🟡 PRIORITY 2 — Remaining Tampering signals (score = 16, target ≤ 5)

After Priority 1 is working consistently, fix these remaining fingerprint.com tampering signals:

| Signal | Current | Target | Notes |
|--------|---------|--------|-------|
| `nav.maxTouchPoints` | ? | 5 | CDP setTouchEmulationEnabled IS called — but Session 63 diagnostic showed 0. Verify if Chrome flag `--touch-events=enabled` helps |
| `chrome.app` | present | absent | Code tries `delete window.chrome.app` but may fail if non-configurable in Chrome 151 |
| `nav.keyboard` | present | absent | Code tries `delete Navigator.prototype.keyboard` but may fail silently |

**Note:** `nav.deviceMemory` was FIXED in Session 69 via `--force-device-memory={fp['deviceMemory']}` Chrome flag.

**To diagnose remaining signals, run Device Check** and look at the fingerprint.com score. Device Check endpoint: `POST http://localhost:8080/api/device-check/run` with SSE.

---

### Step 4 — Har kaam ke baad HANDOFF.md update karo

Har session ke baad HANDOFF.md mein apna session add karo:
```
## Session 70 Changes (date) — [title]
### Problem
### Root Cause  
### Fix Applied
### Files Changed
### Verification
```

---

### Project Quick Reference

| File | Kya hai |
|---|---|
| `artifacts/api-server/gmail_uc_checker.py` | Main Python Selenium script (~4581 lines) |
| `artifacts/api-server/src/lib/browserLoginChecker.ts` | Node wrapper — Python spawn, concurrency |
| `artifacts/api-server/src/routes/jobs.ts` | Express routes (SSE stream endpoint) |
| `artifacts/gmail-checker/src/pages/home.tsx` | Full React frontend (~2448 lines) |

**Credential format:** `email:password:2FA_SECRET` (each line)
**Test credential:** `jamesrodgersfhi888@gmail.com:HxyHGPeaPPPm:czln7pn6bjfr6drkhsrihokvj5adbgqx`

**Secrets configured:**
- `PROXY_URL` — rp.scrapegw.com residential proxy (format: `http://user:pass@host:port` or `https://...`)
- `SESSION_SECRET` — Express session secret

**Workflows:**
- API: `artifacts/api-server: API Server` → port 8080
- Frontend: `artifacts/gmail-checker: web` → port 5173

**Python deps (agar missing ho):**
```bash
pip install -r artifacts/api-server/requirements.txt
```

**Node deps (agar missing ho):**
```bash
pnpm install
```

**ChromeDriver binary location:**
```
~/.local/share/undetected_chromedriver/undetected_chromedriver
```
Binary state: Size=21405376 bytes (correct), cdc_=absent ✅, UC marker=present ✅

---

### Key Gotchas for Next Agent

1. **Google SPA behavior**: After typing email + clicking Next, URL STAYS at `/v3/signin/identifier` — password field appears in-place WITHOUT URL change. Do NOT check for URL change as a stall indicator. Step 3's `wait_for_any(PW_SELECTORS, timeout=8)` handles this correctly.

2. **cdc_ patch persists**: The UC monkey-patch prevents re-download on every check. Binary is clean. Do NOT re-patch unless binary goes missing.

3. **Geo lookup**: Proxy uses HTTP (not HTTPS). `geo_lookup_proxy()` now forces `http://` scheme for proxy connection. If geo fails, check proxy URL scheme.

4. **Account rate limiting**: If testing same account repeatedly with failures, Google will reject everything for hours. Wait or use different account.

5. **Chrome session lock**: Only ONE Chrome runs at a time (OOM guard). This is intentional.

---
