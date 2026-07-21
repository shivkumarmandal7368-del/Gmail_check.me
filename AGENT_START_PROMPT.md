# New Agent Starter Prompt — Vanguard MX

## Copy-paste this entire prompt when starting a new agent session:

---

Tum **Vanguard MX** project pe kaam kar rahe ho. Pehle kaam shuru karne se **`HANDOFF.md`** poora padho — yeh tumhara primary source of truth hai. Phir neeche diye gaye kaam karo.

### Step 1 — HANDOFF.md padho (mandatory)
```
ReadFile: HANDOFF.md
```
Poora padho. Sabse important sections:
- **Session 17 Changes** (sabse upar near top) — UNRESOLVED bug jo tumhe fix karna hai
- **Architecture** — how Python/Node/Chrome interact
- **Known Gotchas** — common mistakes

---

### Step 2 — Workflows restart karo (always do this first)
Dono workflows restart karo before any work:
- `artifacts/api-server: API Server`
- `artifacts/gmail-checker: web`

---

### Step 3 — Ye kaam karo (priority order)

#### 🔴 PRIORITY 1 — Concurrent Chrome crash fix (Session 17 — UNRESOLVED)

**Problem:** Jab 2 Gmail accounts ek saath check karte hain, ek fail hota hai:
```
HTTPConnectionPool(host='localhost', port=56445): Max retries exceeded
[Errno 111] Connection refused
```
Ek ek karke check karne pe sab theek kaam karta hai.

**Root cause:** Chrome launch lock sirf startup ke liye hold hota hai (~1s). Phir 2 Chrome instances ek saath chalte hain → RAM khatam → OOM killer ek Chrome ko kill karta hai mid-session → ChromeDriver connection lost → `unknown` result.

**Fix:** `artifacts/api-server/gmail_uc_checker.py` mein ek nayi lock add karo jo POORE Chrome session ke liye hold rahe.

**EXACT fix (Session 17 mein detailed hai, yahan summary):**

1. File mein `_CHROME_LAUNCH_LOCK_PATH` ke paas nayi constant add karo:
```python
_CHROME_SESSION_LOCK_PATH = "/tmp/gmail_checker_chrome_session.lock"
```

2. Chrome launch se PEHLE session lock acquire karo (line ~946 se pehle, jahan `_lock_fd` open hota hai):
```python
_session_lock_fd = open(_CHROME_SESSION_LOCK_PATH, "w")
log("Waiting for Chrome session slot…")
fcntl.flock(_session_lock_fd, fcntl.LOCK_EX)
log("Chrome session slot acquired")
```

3. `check_gmail()` ke main `try/finally` block mein (ya `_cleanup()` mein) session lock release karo:
```python
finally:
    try:
        fcntl.flock(_session_lock_fd, fcntl.LOCK_UN)
        _session_lock_fd.close()
    except Exception:
        pass
```

4. Existing `_CHROME_LAUNCH_LOCK_PATH` logic BILKUL mat chhuo — woh display + port allocation ke liye hai aur sahi kaam karta hai.

**Test karke verify karo:**
```bash
curl -s -X POST http://localhost:8080/api/emails/browser-check \
  -H "Content-Type: application/json" \
  --max-time 600 \
  -d '{
    "credentials":[
      {"email":"acct1@gmail.com","password":"pass1"},
      {"email":"acct2@gmail.com","password":"pass2"}
    ],
    "proxy":"http://USER:PASS@rp.scrapegw.com:6060",
    "concurrency":2,
    "freshProfile":true
  }'
```
Dono accounts ka result aana chahiye — koi `Connection refused` error nahi.

---

#### 🟡 PRIORITY 2 — v3/signin/TL=... page handling verify karo (Session 16)

**Background:** Google kabhi kabhi `https://accounts.google.com/v3/signin/TL=...` URL pe TOTP page dikhata hai (standard `challenge/totp` ke bajaye). Session 16 mein yeh fix hua tha lekin workflows fail/restart ke chakkar mein user ne bola abhi bhi `unknown` aa raha hai.

**Fix already in code — sirf verify karo:**
1. `gmail_uc_checker.py` line ~1626 check karo:
```python
_on_totp_url = (
    "challenge/totp" in url
    or "challenge/ipp" in url
    or ("v3/signin" in url and "v3/signin/identifier" not in url and "challenge" not in url)
)
```

2. `classify()` function mein (line ~1125) check karo:
```python
if (
    "v3/signin" in url
    and "v3/signin/identifier" not in url
    and "challenge" not in url
    and any(x in _low for x in ["google authenticator", "verification code from", "authenticator app", "verify that it's you"])
):
    return {"status": "opened", ...}
```

3. Wrong TOTP fallback (line ~1860) check karo:
```python
if "v3/signin" in url and "challenge" not in url:
    return {"status": "opened", ...}  # not wrong_password
```

Agar koi bhi missing hai toh add karo.

---

### Step 4 — Har kaam ke baad HANDOFF.md update karo

Har session ke baad HANDOFF.md mein apna session add karo:
```
## Session 18 Changes (date) — [title]
### Problem
### Root Cause  
### Fix Applied
### Files Changed
```

---

### Project Quick Reference

| File | Kya hai |
|---|---|
| `artifacts/api-server/gmail_uc_checker.py` | Main Python Selenium script (~2200+ lines) |
| `artifacts/api-server/src/lib/browserLoginChecker.ts` | Node wrapper — Python spawn, concurrency |
| `artifacts/api-server/src/routes/emails.ts` | Express routes (SSE stream endpoint) |
| `artifacts/gmail-checker/src/pages/home.tsx` | Full React frontend |

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

---
