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
        return pyotp.TOTP(secret).now()
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

    options = uc.ChromeOptions()
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
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1366,768")
    options.add_argument("--lang=en-US")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-save-password-bubble")
    options.add_argument("--disable-translate")
    options.add_argument("--disable-infobars")
    options.add_argument("--password-store=basic")
    options.add_argument("--metrics-recording-only")
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

    def classify(url: str, text: str) -> dict | None:
        at_mailbox = "mail.google.com" in url or "gmail.com/mail" in url

        has_compose = False
        try:
            has_compose = len(driver.find_elements(By.CSS_SELECTOR,
                '[gh="cm"],[data-tooltip="Compose"],[aria-label="Compose"]')) > 0
        except Exception:
            pass

        has_inbox_text = (
            "compose" in text
            or ("inbox" in text and "sign in" not in text)
            or ("primary" in text and "mail.google.com" in url)
        )

        if at_mailbox or has_compose or has_inbox_text:
            if not at_mailbox and not has_compose and (
                "sign in" in text or "create an account" in text
            ):
                pass  # marketing page, not logged in
            else:
                rand_sleep(1500, 2000)
                shot = screenshot_b64()
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

        if any(x in text for x in [
            "verify your identity", "verify it's you", "choose a way to verify",
            "confirm it's you", "unusual activity", "suspicious activity",
            "protect your account"
        ]) or "challenge" in url or "InterstitialConfirmation" in url or (
            "verify" in url and "mail" not in url
        ):
            return {
                "status": "verification_required",
                "reason": "Google is asking for phone/device verification",
                "totpCode": totp_code,
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
        rand_sleep(800, 1500)
        driver.execute_script("window.scrollBy(0, 100)")
        rand_sleep(400, 800)
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

    email_field.click()
    rand_sleep(150, 300)
    email_field.send_keys(email)
    rand_sleep(400, 700)
    email_field.send_keys(Keys.ENTER)
    rand_sleep(2000, 3000)

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

    pw_field.send_keys(password)
    rand_sleep(300, 600)
    pw_field.send_keys(Keys.ENTER)
    rand_sleep(2500, 3500)

    url, text = page_state()
    log(f"{email} — After password submit: {url[:70]}")

    result = classify(url, text)
    if result:
        return result

    # ── Step 4: 2FA / TOTP ────────────────────────────────────────────────────
    totp_field = None
    try:
        totp_field = driver.find_element(
            By.CSS_SELECTOR,
            'input[name="totpPin"],input[name="Pin"],input[id="totpPin"],input[type="tel"]'
        )
    except Exception:
        pass

    is_2fa = (
        totp_field is not None
        or any(x in text for x in [
            "2-step verification", "authenticator app",
            "enter the code", "verification code"
        ])
    )

    if is_2fa:
        if not totp_code:
            return {"status": "2fa_required", "reason": "2FA required — provide TOTP secret", "totpCode": None}
        if totp_field:
            totp_field.send_keys(totp_code)
            rand_sleep(300, 500)
            totp_field.send_keys(Keys.ENTER)
            rand_sleep(2000, 3000)
            url, text = page_state()
            result = classify(url, text)
            if result:
                return result

    # ── Final fallback ────────────────────────────────────────────────────────
    result = classify(url, text)
    if result:
        return result

    shot = screenshot_b64()
    return {
        "status": "unknown",
        "reason": f"Unexpected page: {url[:80]}",
        "totpCode": totp_code,
        "debugScreenshot": shot,
    }


if __name__ == "__main__":
    main()
