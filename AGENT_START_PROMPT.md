# New Agent Starter Prompt — Vanguard MX

## Copy-paste this entire prompt when starting a new agent session:

---

Tum **Vanguard MX** project pe kaam kar rahe ho. Pehle kaam shuru karne se **`HANDOFF.md`** poora padho — yeh tumhara primary source of truth hai. Phir neeche diye gaye kaam karo.

### Step 1 — HANDOFF.md padho (mandatory)
```
ReadFile: HANDOFF.md
```
Poora padho. Sabse important sections:
- **Session 67 Changes** (sabse upar near top) — UNRESOLVED bug jo tumhe fix karna hai
- **Architecture** — how Python/Node/Chrome interact
- **Known Gotchas** — common mistakes

---

### Step 2 — Workflows restart karo (always do this first)
Dono workflows restart karo before any work:
- `artifacts/api-server: API Server`
- `artifacts/gmail-checker: web`

---

### Step 3 — Ye kaam karo (priority order)

#### 🔴 PRIORITY 1 — ChromeDriver cdc_ patch (Session 67 — UNRESOLVED)

**Problem:** `signin/rejected` — Google immediately rejects after email submit. Root cause confirmed.

**Root cause:** ChromeDriver binary at `~/.local/share/undetected_chromedriver/undetected_chromedriver` has **11 occurrences** of `cdc_adoQpoasnfa76pfcZLmcfl_` still present. UC's patcher regex `{window.cdc...;}` does NOT match Chrome 151's binary format, so UC's auto-patch silently fails. These strings are injected as JavaScript variables (`window.cdc_adoQpoasnfa76pfcZLmcfl_Window`, etc.) into every page Chrome opens — Google reads them and immediately detects automation → `signin/rejected`.

**Confirmed facts (do NOT re-investigate):**
- `cdc_adoQpoasnfa76pfcZLmcfl_` count in binary: **11**
- UC regex `{window.cdc...;}` match: **NONE** (UC's patcher is broken for Chrome 151)
- `is_binary_patched()` returns **True** (marker present from a previous partial run) — so UC won't auto-re-patch on its own
- MOBILE_UA is already Chrome 151 ✅ (line 2598 in gmail_uc_checker.py updates fp["chromeVersion"] before building UA)
- Proxy (PROXY_URL secret) is working ✅ — T-Mobile residential IPs

**EXACT fix — run this Python script:**
```python
import random, string
binary_path = '/home/runner/.local/share/undetected_chromedriver/undetected_chromedriver'
with open(binary_path, 'rb') as f:
    content = f.read()
old = b'cdc_adoQpoasnfa76pfcZLmcfl_'  # 28 bytes
count_before = content.count(old)
# Must be SAME length as old (28 bytes) — binary string replacement requires same size
random.seed(99887)
new = ('rvx_' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=24))).encode()[:28]
new_content = content.replace(old, new)
with open(binary_path, 'wb') as f:
    f.write(new_content)
print(f'Replaced {count_before} occurrences → {new_content.count(old)} remaining')
```

**After patching, ALSO add a permanent auto-patch in `gmail_uc_checker.py`** so it self-heals on every restart. Add this function near the top of `check_gmail()` — before the Chrome launch section — so a fresh UC download never leaves cdc_ strings:

```python
def _ensure_chromedriver_patched():
    """Force-patch the UC chromedriver binary if cdc_ strings are present.
    UC 3.5.5's built-in patcher uses a regex that doesn't match Chrome 151's
    binary format, so we do the replacement manually here."""
    import glob, random, string as _string
    uc_dir = os.path.expanduser('~/.local/share/undetected_chromedriver/')
    paths = glob.glob(uc_dir + '*chromedriver*')
    old = b'cdc_adoQpoasnfa76pfcZLmcfl_'  # 28 bytes
    random.seed(77331)
    new = ('rvx_' + ''.join(random.choices(_string.ascii_lowercase + _string.digits, k=24))).encode()[:28]
    for path in paths:
        try:
            with open(path, 'rb') as f:
                content = f.read()
            if old in content:
                new_content = content.replace(old, new)
                with open(path, 'wb') as f:
                    f.write(new_content)
                log(f'[patch] Patched {content.count(old)} cdc_ refs in {os.path.basename(path)}')
            else:
                log(f'[patch] {os.path.basename(path)} already clean')
        except Exception as e:
            log(f'[patch] Warning: {e}')
```

Call `_ensure_chromedriver_patched()` inside `check_gmail()` right after the `import undetected_chromedriver as uc` block and before the Chrome options setup.

**Test after fix:**
```bash
curl -s -X POST http://localhost:8080/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"credentials":[{"email":"jamesrodgersfhi888@gmail.com","password":"HxyHGPeaPPPm","totp":"czln7pn6bjfr6drkhsrihokvj5adbgqx"}],"concurrency":1,"freshProfile":true}'
```
Watch logs — should advance past "After email submit" to Step 3 (password) without `signin/rejected`.

---

#### 🟡 PRIORITY 2 — Remaining Tampering signals (score = 16, target ≤ 5)

After Priority 1 is fixed and Gmail login works, fix these remaining fingerprint.com tampering signals (from Session 66 diagnostic):

| Signal | Current | Target | Notes |
|--------|---------|--------|-------|
| `nav.deviceMemory` | undefined | 8 | CDP `Emulation.setDeviceMemoryOverride` removed in Chrome 151. Try Chrome flag `--force-device-memory=8` in options |
| `nav.maxTouchPoints` | 0 | 5 | CDP `Emulation.setTouchEmulationEnabled` IS being called but not propagating. Try calling it BEFORE stealth JS injection, not after |
| `chrome.app` | present | absent | Code exists (line ~1571) but not taking effect in Chrome 151 |
| `nav.keyboard` | present | absent | Code exists (line ~1621) but not taking effect |

**For `nav.deviceMemory` Chrome flag approach:**
```python
options.add_argument("--force-device-memory=8")  # add to Chrome options in check_gmail()
```

**For `nav.maxTouchPoints` — try early CDP before page load:**
Ensure `Emulation.setTouchEmulationEnabled` is called immediately after Chrome launches, before any `driver.get()`.

---

### Step 4 — Har kaam ke baad HANDOFF.md update karo

Har session ke baad HANDOFF.md mein apna session add karo:
```
## Session 68 Changes (date) — [title]
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
| `artifacts/api-server/gmail_uc_checker.py` | Main Python Selenium script (~4448 lines) |
| `artifacts/api-server/src/lib/browserLoginChecker.ts` | Node wrapper — Python spawn, concurrency |
| `artifacts/api-server/src/routes/jobs.ts` | Express routes (SSE stream endpoint) |
| `artifacts/gmail-checker/src/pages/home.tsx` | Full React frontend (~2448 lines) |

**Credential format:** `email:password:2FA_SECRET` (each line)
**Test credential:** `jamesrodgersfhi888@gmail.com:HxyHGPeaPPPm:czln7pn6bjfr6drkhsrihokvj5adbgqx`

**Secrets configured:**
- `PROXY_URL` — rp.scrapegw.com residential proxy (set, working)
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

---
