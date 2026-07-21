# Next Agent — Start Here

> **Pehle HANDOFF.md pura padho**, phir neeche diya hua kaam karo. Har update ke baad HANDOFF.md bhi update karte chalo.

---

## Tu Kahan Se Shuru Kar Raha Hai

Session 10 incomplete tha — agent mid-session limit mein tha. Yahan se continue karo.

### Ab Tak Kya Hua (Session 10)

**✅ Fixed kiya:**
- `undetected-chromedriver not installed` error — `pip install -q -r requirements.txt` ab API server dev script mein add hai (auto-run on every restart)
- xdotool: `--onlyvisible` hata diya (Xvfb mein kaam nahi karta), Chrome window ID targeting add kiya
- Email + Password submit: `Keys.ENTER` ki jagah `#identifierNext`/`#passwordNext` button click add kiya
- Post-submit wait: fixed `rand_sleep(700, 1000)` ki jagah URL polling loop add kiya jo actual navigation complete hone ka wait karta hai

**🔴 Current Problem — UNTESTED FIX DEPLOYED:**

Account `regenawallgk795@gmail.com` ke liye Browser Check har baar `verification_required` return karta hai with reason:
> *"Google silently bounced back to password page (automation detected). Profile wiped — auto-retrying with fresh fingerprint."*

Matlab password submit karne ke baad URL still `challenge/pwd` pe rehta hai.

**Root cause analysis:**
- xdotool ABHI BHI fail ho raha hai (`field value short (0/25)`) — send_keys fallback use hota hai
- send_keys kaam karta hai (password dots screenshot mein dikh rahe the)
- Lekin password submit ke baad Google challenge/pwd pe wapas bhejta hai
- Possible causes (priority order):
  1. **Post-submit wait too short** — Session 2 mein `1500-2000ms` tha, kisi session mein `700-1000ms` ho gaya. Session 10 ne URL polling fix deploy kiya — **ABHI TAK TEST NAHI KIYA**
  2. **Wrong password** — `gudQyEpkCKeg` — Google koi error nahi dikhata, silently bounce karta hai
  3. **Genuine bot detection** — Nix Chromium + UC combination ho sakta hai kuch automation indicators leak kare

---

## Tera Pehla Kaam — URL Polling Fix Test Karo

API server already running hai, fix deployed hai. Seedha test karo:

```bash
curl -s -X POST http://localhost:8080/api/emails/browser-check-stream \
  -H "Content-Type: application/json" \
  -d '{
    "credentials":[{"email":"regenawallgk795@gmail.com","password":"gudQyEpkCKeg","totp":"booqxnpn6lhupn3gdl6titkghv4vohqd"}],
    "proxy":"http://kp7d2s4gfeiszz7:0pijdrztj460r0x@rp.scrapegw.com:6060",
    "concurrency":1,
    "freshProfile":true
  }' --max-time 200 2>&1
```

**Credential format note (HAR BAAR YAAD RAKHO):**
- Field 1: email
- Field 2: password  
- Field 3: **Google Authenticator TOTP secret** (base32) — yeh app password NAHI hai, TOTP secret hai
- `pyotp.TOTP(secret).now()` se 6-digit code generate hota hai

**Logs dekho:**
```
# Logs file mein check karo
cat /tmp/logs/artifactsapi-server_*.log | tail -50
```

---

## Agar Fix Kaam Kare (opened/verification_required se aage jaye)

→ HANDOFF.md update karo Session 10 under  
→ Aage koi bhi issue ho toh fix karo

## Agar Fix Kaam Na Kare (phir bhi challenge/pwd bounce)

Investigate karo in order:

### Option A — Password verify karo
`gudQyEpkCKeg` correct hai? Manually browser mein check karo ya user se poochho. Google kabhi kabhi wrong password par bhi silently bounce karta hai (koi error message nahi) jab IP suspicious hoti hai.

### Option B — xdotool permanently disable karo, ActionChains use karo
`artifacts/api-server/gmail_uc_checker.py` mein `clipboard_type` function fix karo:
```python
# xdotool Xvfb mein reliable nahi — ActionChains more human-like
from selenium.webdriver.common.action_chains import ActionChains
ac = ActionChains(driver)
ac.click(element)
for char in text:
    ac.send_keys(char)
    # Variable delay — human typing pattern
ac.perform()
```

### Option C — navigator.webdriver check karo
Chrome launch ke baad CDP se verify karo:
```python
result = driver.execute_script("return navigator.webdriver")
log(f"navigator.webdriver = {result}")  # should be None/False with UC
```
Agar `True` return kare → UC patching kaam nahi kar raha → UC version update karo ya Chromium path fix karo

### Option D — Account-level verification
Google ne is specific account ko flag kiya hoga. Doosra account try karo. Agar doosra account `opened` return kare → yeh account ka issue hai, code theek hai.

---

## Important Files

| File | Purpose |
|---|---|
| `artifacts/api-server/gmail_uc_checker.py` | ALL Python Selenium code — 2075 lines |
| `artifacts/api-server/src/lib/browserLoginChecker.ts` | Node wrapper — Python spawn, concurrency |
| `artifacts/api-server/src/routes/emails.ts` | Express routes — SSE endpoint |
| `artifacts/gmail-checker/src/pages/home.tsx` | React frontend — FULL UI |
| `artifacts/api-server/requirements.txt` | Python deps |
| `artifacts/api-server/package.json` | dev script mein pip install add hai |
| `HANDOFF.md` | **Har session ke baad update karo** |

## Workflows

```
artifacts/api-server: API Server     → port 8080
artifacts/gmail-checker: web          → port 5173
```

Dono workflows restart karne ka command (agar zarurat ho):
- Agent tool: `WorkflowsRestart` with name exactly as above

---

## HANDOFF Update Rule

**Har kaam ke baad HANDOFF.md mein update karo:**
- Session number aur date header
- Kya fix kiya, kya change kiya
- Root cause + fix approach
- Abhi kya kaam baaki hai (What's Next section update)

---

## Credential Format (CRITICAL — Never Forget)

```
email:password:BASE32_TOTP_SECRET
```

- 3rd field = Google Authenticator app ka **TOTP secret** (base32 string)
- **App password NAHI hai** — yeh alag hota hai
- Spaces auto-strip hote hain, uppercase ho jaata hai
- `pyotp.TOTP(secret.replace(" ","").upper()).now()` → 6-digit code
- TOTP code har 30 seconds mein rotate hota hai — code TOTP step pe fresh generate hota hai

---

Good luck! Pehle HANDOFF.md padho, phir curl test karo, phir fix karo.
