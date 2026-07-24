#!/usr/bin/env python3
"""
Device Check — fingerprint.com audit script for Vanguard MX.

Line protocol (stdout):
  LOG:<message>          progress line forwarded to SSE as 'log' event
  SCREENSHOT:<base64>    PNG screenshot forwarded as 'screenshot' event
  DONE                   finished cleanly
  ERROR:<msg>            fatal error
"""
import os, sys, time, zipfile, base64, subprocess
from urllib.parse import urlparse, quote

# Unbuffered stdout for streaming
sys.stdout.reconfigure(line_buffering=True)

def log(msg):
    print(f"LOG:{msg}", flush=True)

def emit_screenshot(path, label=""):
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        tag = f":{label}" if label else ""
        print(f"SCREENSHOT{tag}:{b64}", flush=True)
        log(f"Screenshot sent — {label or path}")
    except Exception as e:
        log(f"Screenshot read failed ({path}): {e}")

# ── Start Xvfb on display :91 ──────────────────────────────────────────────
DISPLAY_NUM = ":91"
log("Starting Xvfb...")
try:
    xvfb = subprocess.Popen(
        ["Xvfb", DISPLAY_NUM, "-screen", "0", "1300x900x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = DISPLAY_NUM
    time.sleep(3)
    log(f"Xvfb ready on {DISPLAY_NUM}")
except FileNotFoundError:
    print("ERROR:Xvfb not found — install xorg.xorgserver in replit.nix", flush=True)
    sys.exit(1)

sys.path.insert(0, os.path.dirname(__file__))

try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
except ImportError as e:
    print(f"ERROR:Missing Python dependency: {e}. Run: pip install undetected-chromedriver selenium", flush=True)
    xvfb.terminate()
    sys.exit(1)

# ── Device: PHONE_PROFILES[0] — Pixel 6 (matches checker exactly) ─────────
UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 6) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.7204.100 Mobile Safari/537.36"
)
log("Device profile: Pixel 6 / Android 14 / Chrome 138")

# ── Proxy from Replit secret ───────────────────────────────────────────────
PROXY_URL = os.environ.get("Proxy", "")
log(f"Proxy: {'configured ✓' if PROXY_URL else 'not set — no proxy will be used'}")

ext_path = None
if PROXY_URL:
    _p = urlparse(PROXY_URL)
    PROXY_HOST = _p.hostname or ""
    PROXY_PORT = _p.port or 6060
    PROXY_USER = quote(_p.username or "", safe="")
    PROXY_PASS = quote(_p.password or "", safe="")
    _bg = (
        f'var c={{mode:"fixed_servers",rules:{{singleProxy:{{scheme:"http",'
        f'host:"{PROXY_HOST}",port:{PROXY_PORT}}}}}}};'
        f'chrome.proxy.settings.set({{value:c,scope:"regular"}},function(){{}});'
        f'function cb(d){{return{{authCredentials:{{username:"{PROXY_USER}",'
        f'password:"{PROXY_PASS}"}}}};}} '
        f'chrome.webRequest.onAuthRequired.addListener(cb,{{urls:["<all_urls>"]}},'
        f'["blocking"]);'
    )
    _mf = (
        '{"version":"1.0.0","manifest_version":2,"name":"p",'
        '"permissions":["proxy","tabs","unlimitedStorage","storage","<all_urls>",'
        '"webRequest","webRequestBlocking"],'
        '"background":{"scripts":["bg.js"]},"minimum_chrome_version":"22.0.0"}'
    )
    ext_path = "/tmp/dc_proxy_ext.zip"
    with zipfile.ZipFile(ext_path, "w") as z:
        z.writestr("bg.js", _bg)
        z.writestr("manifest.json", _mf)
    log(f"Proxy extension built → {PROXY_HOST}:{PROXY_PORT}")

# ── Chrome options (mirrors gmail_uc_checker.py launch flags) ─────────────
o = uc.ChromeOptions()
o.add_argument("--no-sandbox")
o.add_argument("--disable-dev-shm-usage")
o.add_argument("--window-size=1300,900")
o.add_argument(f"--user-agent={UA}")
o.add_argument("--touch-events=enabled")
o.add_argument("--force-device-scale-factor=1")
o.add_argument("--lang=en-US,en;q=0.9")
o.add_argument("--disable-gpu")
if ext_path:
    o.add_extension(ext_path)

log("Launching Chrome...")
try:
    driver = uc.Chrome(options=o, version_main=138)
except Exception as e:
    print(f"ERROR:Chrome launch failed: {str(e)[:400]}", flush=True)
    xvfb.terminate()
    sys.exit(1)

time.sleep(5)
log("Chrome launched ✓")

# ── CDP overrides (mirrors checker startup block) ──────────────────────────
try:
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {
        "userAgent": UA,
        "acceptLanguage": "en-US,en;q=0.9",
        "platform": "Linux armv8l",
        "userAgentMetadata": {
            "brands": [
                {"brand": "Not(A;Brand",  "version": "8"},
                {"brand": "Chromium",      "version": "138"},
                {"brand": "Google Chrome", "version": "138"},
            ],
            "fullVersion": "138.0.7204.100",
            "platform": "Android",
            "platformVersion": "14",
            "architecture": "",
            "model": "Pixel 6",
            "mobile": True,
            "bitness": "",
            "wow64": False,
        },
    })
    driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "America/New_York"})
    driver.execute_cdp_cmd("Emulation.setLocaleOverride",   {"locale": "en-US"})
    log("CDP overrides applied (UA, timezone, locale)")
except Exception as e:
    log(f"CDP override warning (non-fatal): {e}")

# ── Navigate to fingerprint.com ────────────────────────────────────────────
log("Navigating to fingerprint.com...")
try:
    driver.get("https://fingerprint.com/")
except Exception as e:
    print(f"ERROR:Navigation failed: {str(e)[:300]}", flush=True)
    driver.quit()
    xvfb.terminate()
    sys.exit(1)

log("Waiting 14s for fingerprint widget to fully render...")
time.sleep(14)

# ── Screenshot 1: homepage with suspect score ──────────────────────────────
driver.get_screenshot_as_file("/tmp/dc_1_homepage.png")
emit_screenshot("/tmp/dc_1_homepage.png", "homepage")

# ── Click "See how this is calculated" ────────────────────────────────────
calculated_els = driver.find_elements(By.XPATH, "//*[contains(text(),'calculated')]")
log(f"Found {len(calculated_els)} element(s) containing 'calculated'")

if calculated_els:
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", calculated_els[0])
    time.sleep(1)
    driver.execute_script("arguments[0].click();", calculated_els[0])
    log("Clicked 'See how this is calculated' — waiting 10s for panel...")
    time.sleep(10)
    driver.get_screenshot_as_file("/tmp/dc_2_breakdown.png")
    emit_screenshot("/tmp/dc_2_breakdown.png", "breakdown")
else:
    log("WARNING: Could not find 'calculated' element — taking fallback screenshot")
    driver.get_screenshot_as_file("/tmp/dc_2_breakdown.png")
    emit_screenshot("/tmp/dc_2_breakdown.png", "breakdown")

# ── Scroll and third screenshot ────────────────────────────────────────────
driver.execute_script("window.scrollBy(0, 300)")
time.sleep(2)
driver.get_screenshot_as_file("/tmp/dc_3_scrolled.png")
emit_screenshot("/tmp/dc_3_scrolled.png", "scrolled")

# ── Cleanup ────────────────────────────────────────────────────────────────
driver.quit()
xvfb.terminate()
log("Chrome and Xvfb closed cleanly")
print("DONE", flush=True)
