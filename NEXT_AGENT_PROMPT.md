# Next Agent — Start Here

> **Pehle HANDOFF.md pura padho**, phir neeche diya hua kaam karo. Har update ke baad HANDOFF.md bhi update karte chalo.

---

## Tu Kahan Se Shuru Kar Raha Hai

**Session 12** — Concurrent browser check mein port conflict bug identify hua. Fix identify ho gayi hai lekin **DEPLOY NAHI HUI**. Tera pehla kaam hai yeh fix lagana aur test karna.

---

## 🔴 Abhi Ka Open Bug — Concurrent Port 38001 Conflict

### Kya Hua (Session 12)

User ne 2 accounts ek saath (concurrent) check kiye. Ek ka result sahi aaya, doosra fail hua:

```
Login error: HTTPConnectionPool(host=..., port=38001): Max retries exceeded ...
NewConnectionError: Failed to establish a new connection [Errno 111] Connection refused
```

### Root Cause

`undetected_chromedriver` jab `uc.Chrome(...)` start karta hai toh ChromeDriver HTTP service ke liye ek port bind karta hai. Default (ya fixed) port **38001** hota hai.

Jab 2 Python processes concurrently `uc.Chrome(...)` call karte hain:
- Process 1: ChromeDriver port 38001 pe bind ho jaata hai ✅
- Process 2: Port 38001 already in use → `Connection refused` ❌

Chrome launch lock (`fcntl.flock`) sirf Chrome *start* karne ko serialize karta hai — lock release hone ke baad dono ChromeDriver instances overlap karte hain (process 1 ka ChromeDriver abhi bhi port 38001 pe chal raha hai jab process 2 shuru hota hai).

### Fix — Kya Karna Hai

**File:** `artifacts/api-server/gmail_uc_checker.py`

**Step 1:** File ke top pe (imports ke saath, line ~10-20 ke paas) `socket` import add karo:

```python
import socket
```

**Step 2:** `socket` import ke neeche ya `_CHROME_LAUNCH_LOCK_PATH` ke paas yeh helper function add karo:

```python
def _find_free_port() -> int:
    """Get a random free TCP port for ChromeDriver to bind on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]
```

**Step 3:** `check_gmail()` function mein, Chrome launch lock ke ANDAR (lock acquire hone ke baad, `uc.Chrome(...)` call se pehle), ek free port pick karo:

Yahan existing code hai (line ~903-914):
```python
_lock_fd = open(_CHROME_LAUNCH_LOCK_PATH, "w")
log("Waiting for Chrome launch slot…")
fcntl.flock(_lock_fd, fcntl.LOCK_EX)
log("Chrome launch slot acquired — starting Chrome")
try:
    driver = uc.Chrome(
        options=options,
        browser_executable_path=chromium_path,
        headless=headless,
        version_main=138,
        use_subprocess=True,
    )
```

Isko yeh karo:
```python
_lock_fd = open(_CHROME_LAUNCH_LOCK_PATH, "w")
log("Waiting for Chrome launch slot…")
fcntl.flock(_lock_fd, fcntl.LOCK_EX)
log("Chrome launch slot acquired — starting Chrome")
_cd_port = _find_free_port()
log(f"ChromeDriver port: {_cd_port}")
try:
    driver = uc.Chrome(
        options=options,
        browser_executable_path=chromium_path,
        headless=headless,
        version_main=138,
        use_subprocess=True,
        port=_cd_port,
    )
```

**Why lock ke andar?** Lock ke andar port pick karne se guarantee hoti hai ki dono processes alag-alag ports lein — ek port select karta hai aur ChromeDriver launch karta hai, phir lock release hone ke baad doosra alag port select karta hai.

---

## Fix Ke Baad — Test Karo

### Step 1: API server restart karo
WorkflowsRestart tool se: `artifacts/api-server: API Server`

### Step 2: Concurrent test karo (2 accounts ek saath)

```bash
curl -s -X POST http://localhost:8080/api/emails/browser-check \
  -H "Content-Type: application/json" \
  --max-time 600 \
  -d '{
    "credentials":[
      {"email":"regenawallgk795@gmail.com","password":"<PASSWORD>","totp":"<TOTP_SECRET>"},
      {"email":"donnalyncht681@gmail.com","password":"<PASSWORD>","totp":"<TOTP_SECRET>"}
    ],
    "proxy":"http://kp7d2s4gfeiszz7:<PROXY_PASSWORD>@rp.scrapegw.com:6060",
    "concurrency":2,
    "freshProfile":true
  }'
```

Credentials user se poochh lo — woh UI mein dalenge aur test karenge, ya logs mein se dekho.

### Expected Results:
- `regenawallgk795@gmail.com` → `opened`
- `donnalyncht681@gmail.com` → `verification_required`
- **Koi port 38001 error nahi** — dono `ChromeDriver port:` log line mein alag ports dikhne chahiye

### Logs check karo:
```bash
cat /tmp/logs/artifacts_api-server_*.log 2>/dev/null | tail -100
# ya
ls -t /tmp/logs/ | head -5
cat /tmp/logs/<latest_log_file> | tail -100
```

---

## Agar Port Fix Kaam Kare

→ HANDOFF.md update karo Session 12 under — fix deployed + test results  
→ `What's Next (Future Work)` section mein se concurrent port bug remove karo

## Agar Port Fix Ke Baad Bhi Error Aaye

Alternate fix — `port` parameter support na kare purani UC version mein:

```python
# uc.Chrome ka port parameter UC version 3.5.5 mein supported hai
# Agar error aaye "unexpected keyword argument 'port'" toh:
# ChromeService explicitly create karo:
from selenium.webdriver.chrome.service import Service as ChromeService
import subprocess, shutil
_cdpath = shutil.which("chromedriver") or "/nix/store/.../chromedriver"
_svc = ChromeService(executable_path=_cdpath, port=_cd_port)
driver = uc.Chrome(options=options, service=_svc, ...)
```

---

## Session 12 Quick Summary

**Single account test (ek ek):** ✅ Both working
- `regenawallgk795@gmail.com` → `opened` ✅ (~73s)
- `donnalyncht681@gmail.com` → `verification_required` ✅ (~57s)

**Concurrent test (ek saath):** ❌ Port 38001 conflict
- Ek account → correct result
- Doosra account → `HTTPConnectionPool port=38001 Connection refused`

**Fix:** `port=_find_free_port()` in `uc.Chrome(...)` call — **NOT YET DEPLOYED**

---

## Important Files

| File | Purpose |
|---|---|
| `artifacts/api-server/gmail_uc_checker.py` | ALL Python Selenium code — ~2101 lines |
| `artifacts/api-server/src/lib/browserLoginChecker.ts` | Node wrapper — Python spawn, concurrency |
| `artifacts/api-server/src/routes/emails.ts` | Express routes — SSE endpoint |
| `artifacts/gmail-checker/src/pages/home.tsx` | React frontend — FULL UI |
| `HANDOFF.md` | **Har session ke baad update karo** |

## Workflows

```
artifacts/api-server: API Server     → port 8080
artifacts/gmail-checker: web          → port 5173
```

---

## Credential Format (CRITICAL — Kabhi Mat Bhoolna)

```
email:password:BASE32_TOTP_SECRET
```

- 3rd field = Google Authenticator app ka **TOTP secret** (base32 string)
- **App password NAHI hai** — yeh alag hota hai
- `pyotp.TOTP(secret.replace(" ","").upper()).now()` → 6-digit code
- TOTP code har 30 seconds mein rotate hota hai

---

## HANDOFF Update Rule

**Har kaam ke baad HANDOFF.md mein update karo:**
- Session number aur date header update karo (top pe `_Last updated` line)
- New section add karo: `## Session 12 Changes`
- Kya fix kiya, root cause, test results
- `What's Next` section update karo

---

Good luck! Fix lagao, test karo, HANDOFF update karo.
