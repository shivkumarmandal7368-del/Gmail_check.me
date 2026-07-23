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
import fcntl
import socket

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


def _find_free_display() -> int:
    """Find a free X display number by checking Xvfb lock files (/tmp/.XN-lock)."""
    for n in range(100, 300):
        if not os.path.exists(f"/tmp/.X{n}-lock"):
            return n
    return random.randint(300, 399)


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[UC] {msg}", file=sys.stderr, flush=True)


def browser_result_category(result: dict) -> str:
    """Return the stable UI category for a browser-check result."""
    status = result.get("status", "")
    if status == "opened":
        return "open"
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
        "platform": "Linux armv81",
        "webglVendor": "ARM", "webglRenderer": "Mali-G78 MP20",
    },
    {
        "model": "Pixel 6a",      "androidVersion": "14",   # Tensor G1
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 6,  "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "ARM", "webglRenderer": "Mali-G78 MP20",
    },
    {
        "model": "Pixel 7",       "androidVersion": "14",   # Tensor G2
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 892, "availH": 868, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 730",
    },
    {
        "model": "Pixel 7a",      "androidVersion": "14",   # Tensor G2
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 892, "availH": 868, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "ARM", "webglRenderer": "Mali-G710 MP7",
    },
    {
        "model": "Pixel 8",       "androidVersion": "14",   # Tensor G3
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 915, "availH": 891, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 740",
    },
    {
        "model": "Pixel 8 Pro",   "androidVersion": "14",   # Tensor G3
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 919, "availH": 895, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 740",
    },
    {
        "model": "Pixel 9",       "androidVersion": "15",   # Tensor G4
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 9, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "ARM", "webglRenderer": "Mali-G715 MP7",
    },
    {
        "model": "Pixel 9 Pro",   "androidVersion": "15",   # Tensor G4
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 892, "availH": 868, "dpr": 2.625,
        "hwConcurrency": 9, "deviceMemory": 16, "maxTouchPoints": 5,
        "platform": "Linux armv81",
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
        "model": "SM-S928B",      "androidVersion": "14",   # Samsung Galaxy S24+
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
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 740",
    },
    {
        "model": "CPH2447",       "androidVersion": "14",   # OnePlus 12
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 919, "availH": 895, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 750",
    },
    {
        "model": "CPH2493",       "androidVersion": "14",   # OnePlus Nord 3
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 919, "availH": 895, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "ARM", "webglRenderer": "Mali-G710 MC10",
    },
    # ── Xiaomi / Redmi ────────────────────────────────────────────────────────
    {
        "model": "2211133G",      "androidVersion": "14",   # Xiaomi 13
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 740",
    },
    {
        "model": "23049PCD8G",    "androidVersion": "14",   # Xiaomi 14
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 750",
    },
    {
        "model": "23078PND5G",    "androidVersion": "14",   # Xiaomi 13T Pro
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "ARM", "webglRenderer": "Mali-G715 MC11",
    },
    {
        "model": "22101316G",     "androidVersion": "13",   # Redmi Note 12 Pro
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "ARM", "webglRenderer": "Mali-G68 MC4",
    },
    # ── Others ────────────────────────────────────────────────────────────────
    {
        "model": "RMX3706",       "androidVersion": "14",   # Realme GT 5
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 740",
    },
    {
        "model": "A065",          "androidVersion": "14",   # Nothing Phone 2
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 892, "availH": 868, "dpr": 2.625,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 730",
    },
    {
        "model": "XT2303-2",      "androidVersion": "14",   # Motorola Edge 40
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "ARM", "webglRenderer": "Mali-G77 MC9",
    },
    {
        "model": "V2246",         "androidVersion": "14",   # Vivo V29
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 642L",
    },
    {
        "model": "PGEM10",        "androidVersion": "14",   # Oppo Find X6
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8, "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "ARM", "webglRenderer": "Mali-G715 MC11",
    },
    # ── Google Pixel (additional) ─────────────────────────────────────────────
    {
        "model": "Pixel 9 Pro XL", "androidVersion": "15",  # Tensor G4
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 932, "availH": 908, "dpr": 2.625,
        "hwConcurrency": 9, "deviceMemory": 16, "maxTouchPoints": 5,
        "platform": "Linux armv81",
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
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 830",
    },
    # ── Xiaomi / Redmi (additional) ───────────────────────────────────────────
    {
        "model": "24030PN60G",    "androidVersion": "14",   # Xiaomi 14 Ultra
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 16, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 750",
    },
    {
        "model": "23127PN0CG",    "androidVersion": "14",   # Xiaomi 14T Pro
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "ARM", "webglRenderer": "Immortalis-G720 MC12",
    },
    {
        "model": "23013PC75G",    "androidVersion": "13",   # Redmi Note 13 Pro+
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "ARM", "webglRenderer": "Mali-G610 MC4",
    },
    # ── Others (additional) ───────────────────────────────────────────────────
    {
        "model": "AI2401",        "androidVersion": "14",   # ASUS ROG Phone 8
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 750",
    },
    {
        "model": "XT2403-3",      "androidVersion": "14",   # Motorola Edge 50 Pro
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv81",
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
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 740",
    },
    # ── OnePlus (additional) ──────────────────────────────────────────────────
    {
        "model": "CPH2609",       "androidVersion": "14",   # OnePlus Nord 4
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 919, "availH": 895, "dpr": 2.625,
        "hwConcurrency": 8,  "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 735",
    },
    # ── Xiaomi (additional) ───────────────────────────────────────────────────
    {
        "model": "24129PN74G",    "androidVersion": "15",   # Xiaomi 15
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 16, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 830",
    },
    {
        "model": "24117RA73G",    "androidVersion": "14",   # Redmi Note 14 Pro+
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 720",
    },
    # ── Others (additional) ───────────────────────────────────────────────────
    {
        "model": "A142",          "androidVersion": "14",   # Nothing Phone (2a)
        "chromeVersion": "138.0.7204.100",
        "screenW": 412, "screenH": 892, "availH": 868, "dpr": 2.625,
        "hwConcurrency": 8,  "deviceMemory": 8,  "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "ARM", "webglRenderer": "Mali-G610 MC4",
    },
    {
        "model": "RMX3851",       "androidVersion": "14",   # Realme GT 6
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 851, "availH": 827, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "Qualcomm", "webglRenderer": "Adreno (TM) 735",
    },
    {
        "model": "CPH2629",       "androidVersion": "14",   # Oppo Reno 12 Pro
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "ARM", "webglRenderer": "Immortalis-G720 MC12",
    },
    {
        "model": "V2324A",        "androidVersion": "14",   # vivo X100 Pro
        "chromeVersion": "138.0.7204.100",
        "screenW": 393, "screenH": 873, "availH": 849, "dpr": 2.75,
        "hwConcurrency": 8,  "deviceMemory": 12, "maxTouchPoints": 5,
        "platform": "Linux armv81",
        "webglVendor": "ARM", "webglRenderer": "Immortalis-G720 MC12",
    },
]


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
    from urllib.parse import urlparse, quote
    import requests as req

    tag = f"[geo{(' ' + _label) if _label else ''}]"

    # Parse + re-encode proxy URL once — proxy usernames can contain + and other special
    # chars that Python's urllib mis-handles. We parse manually and re-encode each part.
    try:
        _parsed = urlparse(proxy_url)
        _user   = quote(_parsed.username or "", safe="")
        _pass   = quote(_parsed.password or "", safe="")
        _host   = _parsed.hostname or ""
        _port   = _parsed.port or 6060
        _scheme = _parsed.scheme or "http"
        _safe_proxy = f"{_scheme}://{_user}:{_pass}@{_host}:{_port}"
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

        # ── Service 2: ipwho.is (HTTPS, comprehensive fallback) ───────────────
        try:
            log(f"{tag} [2/3] ipwho.is attempt {_attempt}/{_retries}")
            r2 = req.get("https://ipwho.is/", proxies=proxies, timeout=15)
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

        # ── Service 3: ipinfo.io (HTTPS, minimal but very reliable) ──────────
        try:
            log(f"{tag} [3/3] ipinfo.io attempt {_attempt}/{_retries}")
            r3 = req.get("https://ipinfo.io/json", proxies=proxies, timeout=15)
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


def get_or_create_fingerprint(profile_dir: str, proxy: str | None = None) -> dict:
    """Load the saved fingerprint for this profile, or generate & save a new one.
    This makes every account look like a consistent, unique device — same as
    antidetect/cloner behaviour.
    If proxy is given and geo-lookup hasn't run yet, timezone + language are
    derived from the proxy's exit IP (so they match the IP location)."""
    fp_path = os.path.join(profile_dir, "fingerprint.json")
    if os.path.exists(fp_path):
        try:
            with open(fp_path, "r") as f:
                existing = json.load(f)
            if all(k in existing for k in ("model", "screenW", "canvasSeed")):
                # If proxy is provided and geo hasn't been locked yet — OR geo was locked
                # but ip field is missing (old fingerprint from before Session 29) — do it now
                _needs_geo = proxy and (
                    not existing.get("geoLocked")
                    or (existing.get("geoLocked") and not existing.get("ip"))
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
                return existing
        except Exception:
            pass
    fp = random.choice(PHONE_PROFILES).copy()
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
        fp["timezone"] = random.choice(_RANDOM_TIMEZONES)
        # Per-account language — varies the Accept-Language header and navigator.languages
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
    fp["connectionRtt"]      = random.randint(35, 95)
    fp["connectionDownlink"] = round(random.uniform(7.5, 15.0), 1)
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
    return fp


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
    cv   = fp["chromeVersion"]
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
Object.defineProperty(navigator,'webdriver',{{get:()=>undefined}});
Object.defineProperty(navigator,'plugins',{{get:()=>{{var p=[];p.length=0;return p;}}}});
Object.defineProperty(navigator,'languages',{{get:()=>['{lg}','en']}});
try{{Object.defineProperty(navigator,'language',{{get:()=>'{lg}'}});}}catch(e){{}}
try{{Object.defineProperty(navigator,'userLanguage',{{get:()=>'{lg}'}});}}catch(e){{}}
try{{Object.defineProperty(navigator,'browserLanguage',{{get:()=>'{lg}'}});}}catch(e){{}}
try{{Object.defineProperty(navigator,'systemLanguage',{{get:()=>'{lg}'}});}}catch(e){{}} 
Object.defineProperty(navigator,'hardwareConcurrency',{{get:()=>{fp['hwConcurrency']}}});
Object.defineProperty(navigator,'deviceMemory',{{get:()=>{fp['deviceMemory']}}});
Object.defineProperty(navigator,'cookieEnabled',{{get:()=>true}});
Object.defineProperty(navigator,'doNotTrack',{{get:()=>{dnt_val}}});
try{{Object.defineProperty(navigator,'globalPrivacyControl',{{get:()=>undefined}});}}catch(e){{}}
Object.defineProperty(screen,'width',      {{get:()=>{fp['screenW']}}});
Object.defineProperty(screen,'height',     {{get:()=>{fp['screenH']}}});
Object.defineProperty(screen,'availWidth', {{get:()=>{fp['screenW']}}});
Object.defineProperty(screen,'availHeight',{{get:()=>{fp['availH']}}});
Object.defineProperty(screen,'colorDepth', {{get:()=>24}});
Object.defineProperty(screen,'pixelDepth', {{get:()=>24}});
try{{Object.defineProperty(screen,'isExtended',{{get:()=>false}});}}catch(e){{}}
Object.defineProperty(window,'devicePixelRatio',{{get:()=>{fp['dpr']}}});
Object.defineProperty(window,'innerWidth',  {{get:()=>{fp['screenW']}}});
Object.defineProperty(window,'innerHeight', {{get:()=>{fp['availH']}}});
Object.defineProperty(navigator,'maxTouchPoints',{{get:()=>{fp['maxTouchPoints']}}});
Object.defineProperty(navigator,'platform',{{get:()=>'{fp['platform']}'}});
Object.defineProperty(navigator,'vendor',  {{get:()=>'Google Inc.'}});
Object.defineProperty(navigator,'appVersion',{{get:()=>'{av_str}'}});
try{{Object.defineProperty(window.history,'length',{{get:()=>{hist},configurable:true}});}}catch(e){{}}
(function(){{
  var d={{brands:[{{brand:'Not=A?Brand',version:'24'}},{{brand:'Chromium',version:'138'}},{{brand:'Google Chrome',version:'138'}}],mobile:true,platform:'Android',
    getHighEntropyValues:function(h){{return Promise.resolve({{brands:this.brands,mobile:this.mobile,platform:this.platform,platformVersion:'{av}',architecture:'',bitness:'',model:'{mdl}',uaFullVersion:'{cv}',fullVersionList:[{{brand:'Not=A?Brand',version:'24.0.0.0'}},{{brand:'Chromium',version:'{cv}'}},{{brand:'Google Chrome',version:'{cv}'}}]}});}},
    toJSON:function(){{return{{brands:this.brands,mobile:this.mobile,platform:this.platform}};}}}};
  try{{Object.defineProperty(navigator,'userAgentData',{{get:()=>d}});}}catch(e){{}}
}})();
(function(){{
  if(!window.chrome)window.chrome={{}};
  if(!window.chrome.runtime){{
    window.chrome.runtime={{
      connect:function(){{throw new Error('Could not establish connection. Receiving end does not exist.');}},
      sendMessage:function(){{throw new Error('Could not establish connection. Receiving end does not exist.');}},
      onMessage:{{addListener:function(){{}},removeListener:function(){{}},hasListener:function(){{return false;}}}},
      onConnect:{{addListener:function(){{}},removeListener:function(){{}},hasListener:function(){{return false;}}}},
      id:undefined
    }};
  }}
  try{{delete window.chrome.app;}}catch(e){{}}
  if(!window.chrome.loadTimes)window.chrome.loadTimes=function(){{return{{requestTime:Date.now()/1000-0.5,startLoadTime:Date.now()/1000-0.5,commitLoadTime:Date.now()/1000-0.3,finishDocumentLoadTime:Date.now()/1000-0.1,finishLoadTime:Date.now()/1000,firstPaintTime:0,firstPaintAfterLoadTime:0,navigationType:'Other',wasFetchedViaSpdy:false,wasNpnNegotiated:false,npnNegotiatedProtocol:'',wasAlternateProtocolAvailable:false,connectionInfo:''}}}};
  if(!window.chrome.csi)window.chrome.csi=function(){{return{{startE:Date.now()-1000,onloadT:Date.now()-500,pageT:500,tran:15}}}};
}})();
if(window.Notification){{Object.defineProperty(Notification,'permission',{{get:()=>'default'}});}}
try{{
  if(navigator.permissions&&navigator.permissions.query){{
    var _origPQ=navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query=function(p){{
      if(p&&p.name==='notifications')return Promise.resolve({{state:Notification.permission,onchange:null}});
      return _origPQ(p);
    }};
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
try{{Object.defineProperty(screen,'orientation',{{get:()=>({{{{'type':'portrait-primary','angle':0}}}})}}); }}catch(e){{}}
try{{
  var conn={{'effectiveType':'4g','rtt':{rtt},'downlink':{dl},'downlinkMax':{dl},'saveData':false,'type':'cellular','onchange':null}};
  Object.defineProperty(navigator,'connection',{{get:()=>conn}});
  Object.defineProperty(navigator,'mozConnection',{{get:()=>undefined}});
  Object.defineProperty(navigator,'webkitConnection',{{get:()=>undefined}});
}}catch(e){{}}
try{{Object.defineProperty(navigator,'keyboard',{{get:()=>undefined}});}}catch(e){{}}
(function(){{
  var _ws={wn};
  function _phash(p){{var h=p^0xDEAD;h=((h>>16)^h)*0x45d9f3b|0;h=((h>>16)^h)*0x45d9f3b|0;return(h^(h>>16))&0xFFFF;}}
  var _wn={wn};var _wv='{wv}';var _wr='{wr}';
  function patch(ctx){{
    var gp=ctx.prototype.getParameter;
    ctx.prototype.getParameter=function(p){{
      if(p===37445)return _wv;          // UNMASKED_VENDOR_WEBGL
      if(p===37446)return _wr;          // UNMASKED_RENDERER_WEBGL
      if(p===7936) return _wv;          // GL_VENDOR  (basic — reveals server GPU on headless)
      if(p===7937) return _wr;          // GL_RENDERER (basic — reveals "ANGLE (Intel, Mesa...)")
      if(p===7938) return '{gl_ver}';  // GL_VERSION — vendor-matched (Adreno V@, Mali v1.r, Xclipse bare)
      if(p===35724)return 'OpenGL ES GLSL ES 3.20';          // SHADING_LANGUAGE_VERSION
      var v=gp.call(this,p);
      if(typeof v==='number'){{var n=(_phash(p)/65535)*_ws*4-_ws*2;return v+n*Math.sign(v||1);}}
      return v;
    }};
  }}
  patch(WebGLRenderingContext);
  if(window.WebGL2RenderingContext)patch(WebGL2RenderingContext);
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
Object.defineProperty(window,'outerWidth',{{get:()=>{fp['screenW']}}});
Object.defineProperty(window,'outerHeight',{{get:()=>{fp['screenH']}}});
try{{navigator.vibrate=function(){{return true;}};}}catch(e){{}}
try{{
  if(navigator.mediaDevices&&navigator.mediaDevices.enumerateDevices){{
    var _oED=navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
    navigator.mediaDevices.enumerateDevices=function(){{
      return _oED().then(function(r){{
        if(r&&r.length>0)return r;
        return[
          {{deviceId:'{cam_rear_id}',groupId:'{cam_group}',kind:'videoinput',label:'camera2 0, facing back'}},
          {{deviceId:'{cam_front_id}',groupId:'{cam_group}',kind:'videoinput',label:'camera2 1, facing front'}},
          {{deviceId:'{mic_id}',groupId:'{mic_group}',kind:'audioinput',label:'Default'}},
        ];
try{{Object.defineProperty(window,'outerWidth',{{get:()=>{sw}}});}}catch(e){{}}
try{{Object.defineProperty(window,'outerHeight',{{get:()=>{sh}}});}}catch(e){{}}
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
try{{Object.defineProperty(navigator,'appVersion',{{get:()=>'5.0 (Linux; Android {av}; {mdl}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv} Mobile Safari/537.36'}});}}catch(e){{}}
try{{Object.defineProperty(navigator,'onLine',{{get:()=>true}});}}catch(e){{}}
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
        {{kind:'audioinput',deviceId:'',groupId:'',label:'',toJSON:function(){{return {{kind:'audioinput',deviceId:'',groupId:'',label:''}};}} }},
        {{kind:'videoinput',deviceId:'',groupId:'',label:'',toJSON:function(){{return {{kind:'videoinput',deviceId:'',groupId:'',label:''}};}} }},
        {{kind:'audiooutput',deviceId:'default',groupId:'default',label:'',toJSON:function(){{return {{kind:'audiooutput',deviceId:'default',groupId:'default',label:''}};}} }}
      ]);
    }};
  }}
}}catch(e){{}}
try{{Object.defineProperty(screen,'availLeft',{{get:()=>0}});}}catch(e){{}}
try{{Object.defineProperty(screen,'availTop',{{get:()=>0}});}}catch(e){{}}
try{{Object.defineProperty(navigator,'javaEnabled',{{value:function(){{return false;}},writable:false,configurable:true}});}}catch(e){{}}
try{{
  var _origMimeTypes=navigator.mimeTypes;
  Object.defineProperty(navigator,'mimeTypes',{{get:()=>{{var m=Object.create(MimeTypeArray.prototype);Object.defineProperty(m,'length',{{value:0}});m.item=function(){{return null;}};m.namedItem=function(){{return null;}};return m;}}}});
}}catch(e){{}}
try{{document.hasFocus=function(){{return true;}};}}catch(e){{}}
try{{
  var _obTblob=HTMLCanvasElement.prototype.toBlob;
  if(_obTblob){{
    var _bseed={cs};
    HTMLCanvasElement.prototype.toBlob=function(cb,t,q){{
      try{{var c=this.getContext('2d');if(c){{var d=c.getImageData(0,0,this.width||1,this.height||1);d.data[0]=d.data[0]^_bseed;c.putImageData(d,0,0);}}}}catch(e){{}}
      _obTblob.call(this,cb,t,q);
    }};
  }}
}}catch(e){{}}
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
      return _omm(q);
    }};
  }}catch(e){{}}
}})();
try{{
  Date.prototype.getTimezoneOffset=function(){{return {_tz_offset};}};
}}catch(e){{}}
try{{delete window.chrome.webstore;}}catch(e){{}}
try{{delete window.chrome.cast;}}catch(e){{}}
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
    navigator.storage.estimate=function(){{
      var _dMem={fp['deviceMemory']};
      return Promise.resolve({{
        quota:Math.round(_dMem*1073741824*0.6),
        usage:Math.round((80+Math.random()*120)*1048576),
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
      this.continuous=false;this.interimResults=false;this.lang='en-IN';
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

    email             = data.get("email", "")
    password          = data.get("password", "")
    totp_secret       = data.get("totp")
    proxy             = data.get("proxy")
    proxy_for_ip_check = data.get("proxyForIpCheck") or proxy  # original URL without sticky suffix
    fresh_profile     = bool(data.get("freshProfile", False))

    _t0 = time.time()
    result = check_gmail(email, password, totp_secret, proxy, fresh_profile, proxy_for_ip_check)

    # Auto-retry up to 3 times if Google blocked automation detection.
    # CRITICAL: each retry MUST use a NEW sticky session ID → different proxy IP.
    # Retrying with the same IP always fails again (IP is already flagged).
    import re as _re
    def _new_session_proxy(proxy_url: str | None) -> str | None:
        """Replace -session-XXXX in proxy username with a fresh random ID."""
        if not proxy_url:
            return proxy_url
        new_id = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=8))
        replaced = _re.sub(r'-session-[a-z0-9]+', f'-session-{new_id}', proxy_url)
        if replaced == proxy_url:
            # No existing session tag — try to inject before the colon separator
            replaced = _re.sub(r'^(https?://[^:]+)', r'\1-session-' + new_id, proxy_url)
        return replaced

    _blocked_reason = result.get("reason", "")

    def _is_retriable(res: dict) -> bool:
        """Return True if the result is something we should auto-retry with a fresh proxy IP."""
        st  = res.get("status", "")
        rsn = res.get("reason", "").lower()
        # Automation blocks
        if st == "verification_required" and any(x in rsn for x in [
            "automation detected", "couldn't sign you in", "blocked this browser",
            "not be secure", "blocked", "rejected",
        ]):
            return True
        # Chrome crash / OOM / spawn failure — retry may succeed with fresh launch slot
        if st == "unknown" and any(x in rsn for x in [
            "chrome launch failed", "oom", "signal", "killed",
            "failed to spawn", "exit code", "timed out",
        ]):
            return True
        return False

    for _retry_n in range(3):
        if not _is_retriable(result):
            break
        _retry_proxy = _new_session_proxy(proxy)
        log(f"{email} — retriable result ({result.get('status')}) on attempt {_retry_n+1}/3, retrying with fresh profile + new proxy IP…")
        result = check_gmail(email, password, totp_secret, _retry_proxy, True, proxy_for_ip_check)
        _blocked_reason = result.get("reason", "")

    result["category"] = browser_result_category(result)
    result["durationMs"] = int((time.time() - _t0) * 1000)
    log(f"{email} — Total duration: {result['durationMs']}ms ({result['durationMs']//1000}s)")
    print(json.dumps(result), flush=True)


# ── Browser check ─────────────────────────────────────────────────────────────

def check_gmail(email: str, password: str, totp_secret: str | None, proxy: str | None, fresh_profile: bool = False, proxy_for_ip_check: str | None = None) -> dict:
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
    fp = get_or_create_fingerprint(profile_dir, proxy=proxy)
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
    proxy_ext_path: str | None = None

    # Proxy configuration
    if proxy:
        proxy_info = parse_proxy(proxy)
        if proxy_info and proxy_info["host"]:
            log(f"Proxy: {proxy_info['host']}:{proxy_info['port']} user={proxy_info.get('username')}")
            if proxy_info.get("username") and not headless:
                proxy_ext_path = make_proxy_extension(
                    proxy_info["host"], proxy_info["port"],
                    proxy_info["username"], proxy_info.get("password") or ""
                )
                options.add_extension(proxy_ext_path)
                log("Proxy auth extension loaded")
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
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-save-password-bubble")
    options.add_argument("--disable-translate")
    options.add_argument("--password-store=basic")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-features=ChromeWhatsNewUI,ChromeReporting,EnablePasswordsAccountStorage,OptimizationHints,AutofillServerCommunication,InterestFeedContentSuggestions,MediaRouter")
    options.add_argument("--disable-sync")
    options.add_argument("--no-pings")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-client-side-phishing-detection")
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-domain-reliability")
    options.add_argument("--disable-hang-monitor")
    options.add_argument("--disable-prompt-on-repost")
    options.add_argument("--mute-audio")
    options.add_argument(f"--user-agent={MOBILE_UA}")
    options.add_argument("--touch-events=enabled")
    # Match the fingerprint DPR so window.devicePixelRatio equals screen.dpr
    options.add_argument(f"--force-device-scale-factor={fp['dpr']}")
    if headless:
        options.add_argument("--disable-gpu")

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
        # NOTE: Do NOT pass driver_executable_path — UC's Patcher renames the
        # binary internally and passing the path causes [Errno 2] No such file.
        # Pre-patching above (uc.Patcher.auto()) already warmed the on-disk cache,
        # so uc.Chrome() will find the cached patched binary instantly without
        # re-downloading or re-patching — all the speed benefit, no rename conflict.
        driver = uc.Chrome(
            options=options,
            browser_executable_path=chromium_path,
            headless=headless,
            version_main=138,
            use_subprocess=True,
            port=_cd_port,
        )
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
        _cleanup(proxy_ext_path, _xvfb_proc)
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

    # Inject stealth patches on every new page (fingerprint-specific values)
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": make_stealth_js(fp)})
        log("Stealth JS injected via CDP")
    except Exception as e:
        log(f"Stealth JS warning: {e}")

    # Fix UA Client Hints in actual HTTP headers using fingerprint values
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setUserAgentOverride", {
            "userAgent": MOBILE_UA,
            "acceptLanguage": f"{fp['language']},en;q=0.9",
            "platform": fp["platform"],
            "userAgentMetadata": {
                "brands": [
                    {"brand": "Not=A?Brand",   "version": "24"},
                    {"brand": "Chromium",       "version": "138"},
                    {"brand": "Google Chrome",  "version": "138"},
                ],
                "fullVersion": fp["chromeVersion"],
                "platform": "Android",
                "platformVersion": fp["androidVersion"],
                "architecture": "",
                "model": fp["model"],
                "mobile": True,
                "bitness": "",
                "wow64": False,
            },
        })
        log(f"Network UA override applied → {fp['model']} / Android {fp['androidVersion']}")
    except Exception as e:
        log(f"Network UA override warning: {e}")

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

    # Exit IP fetch skipped — each account uses a unique sticky session ID for IP isolation
    exit_ip = None

    _login_result: dict = {}
    try:
        _login_result = _do_login(driver, email, password, totp_code, totp_secret, fresh_profile)

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
        _cleanup(proxy_ext_path, _xvfb_proc)
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

def _do_login(driver, email: str, password: str, totp_code: str | None, totp_secret: str | None = None, fresh_profile: bool = False) -> dict:
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
            "couldn't find your google account", "no account found",
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
        is_real_challenge = (
            (
                "challenge" in url
                and "challenge/pwd" not in url
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

    # ── Step 0: Warmup — visit Google homepage first ─────────────────────────
    # CRITICAL: Without this, Google detects automation at the password step
    # and silently bounces back to challenge/pwd (no error message).
    # Root cause: fingerprint needs a real page load to "warm up" before
    # Google's login flow trusts the session. The warmup MUST:
    #   1. Wait for the page to be truly interactive (not just nav start)
    #   2. Do minimal human-like interaction (scroll)
    #   3. Sleep long enough for JS fingerprinting to execute over proxy
    # 800–1200ms was too short over proxy — page incomplete → Google detects
    # automation → bounces back to challenge/pwd → wrongly tagged wrong_password.
    try:
        log(f"{email} — Step 0: warmup — building Google session cookies")
        driver.get("https://www.google.com")
        # Wait for page fully interactive — proxy latency can take 2-5s
        _w_deadline = time.time() + 8
        while time.time() < _w_deadline:
            try:
                if driver.execute_script("return document.readyState") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.35)
        rand_sleep(800, 1300)
        # Accept cookie/privacy consent if Google shows it (common outside US)
        try:
            for _btn_sel in [
                "#L2AGLb", "button.tHlp8d", "[jsname='b3VHJd']",
                "button[aria-label*='Accept all']", "button[aria-label*='accept']",
            ]:
                _btns = driver.find_elements(By.CSS_SELECTOR, _btn_sel)
                if _btns and _btns[0].is_displayed():
                    _btns[0].click()
                    rand_sleep(600, 900)
                    break
        except Exception:
            pass
        # Real Google search — builds NID, 1P_JAR, SOCS, AEC cookies that
        # make the subsequent login session look organic, not bot-fresh.
        try:
            _search_terms = ["gmail login", "google account", "inbox email", "google news", "youtube"]
            _term = random.choice(_search_terms)
            _sbox = driver.find_elements(By.NAME, "q")
            if _sbox and _sbox[0].is_displayed():
                for _c in _term:
                    _sbox[0].send_keys(_c)
                    time.sleep(random.uniform(0.07, 0.19))
                rand_sleep(450, 750)
                _sbox[0].send_keys(Keys.RETURN)
                rand_sleep(1800, 2800)
                # Scroll through results like a real user
                driver.execute_script("window.scrollTo({top: 350, behavior: 'smooth'});")
                rand_sleep(600, 1000)
                driver.execute_script("window.scrollTo({top: 0, behavior: 'smooth'});")
                rand_sleep(400, 700)
                log(f"{email} — Step 0: searched '{_term}', session cookies built")
        except Exception:
            pass
        rand_sleep(700, 1100)
        log(f"{email} — Step 0: warmup complete")
    except Exception:
        pass  # warmup failure is non-fatal — continue anyway

    # ── Step 1: Navigate to Gmail sign-in ────────────────────────────────────
    log(f"{email} — Step 1: navigating to sign-in page")
    try:
        driver.get(
            "https://accounts.google.com/v3/signin/identifier"
            "?continue=https%3A%2F%2Fmail.google.com%2Fmail%2F"
            "&service=mail&flowName=GlifWebSignIn&flowEntry=ServiceLogin"
        )
        rand_sleep(600, 1000)
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

    # Already-authenticated session in persistent profile — skip straight to Gmail
    # Do NOT fall through to Steps 2-4 (no email field on these pages)
    if "signin/continue" in url or "accounts.google.com/o/oauth2/auth" in url:
        log(f"{email} — Session still active (signin/continue), navigating to Gmail directly")
        try:
            driver.get("https://mail.google.com/mail/u/0/#inbox")
            rand_sleep(2500, 3500)
        except Exception:
            pass
        # Mini interstitial loop — dismiss recovery/uplevelingstep pages then land on Gmail
        _uplevel_hits = 0
        for _si in range(8):
            url, text = page_state()
            log(f"{email} — [shortcut loop {_si}] {url[:70]}")
            if "mail.google.com" in get_hostname(url):
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
                        driver.get("https://mail.google.com/mail/h/?zy=e")
                        rand_sleep(2000, 3000)
                        _hu = driver.current_url
                        if "mail.google.com" in get_hostname(_hu) and "uplevelingstep" not in _hu:
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
                    driver.get("https://mail.google.com/mail/u/0/#inbox")
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

    # Wait for URL to advance past the email page (proxy latency can be 2-4s).
    # Poll until URL changes or timeout — more reliable than a fixed sleep.
    _pre_email_url = driver.current_url
    _nav_deadline = time.time() + 8
    while time.time() < _nav_deadline:
        try:
            _cur = driver.current_url
            if _cur != _pre_email_url and "signin/identifier" not in _cur:
                break
        except Exception:
            pass
        time.sleep(0.25)
    rand_sleep(300, 600)  # small extra settle after URL change

    url, text = page_state()
    log(f"{email} — After email submit: {url[:70]}")

    # ── Identifier-page stall fix (Session 8) ────────────────────────────────
    # If Google never navigated past the email page, the proxy IP was detected
    # at the email step (silent CAPTCHA or block). Falls to unknown otherwise.
    # Classify as verification_required so auto-retry fires with a fresh proxy IP.
    if ("signin/identifier" in url) and "challenge" not in url and "mail.google.com" not in url:
        shot = screenshot_b64()
        log(f"{email} — Identifier stall: page did not advance past email field (automation detected at email step)")
        return {
            "status": "verification_required",
            "reason": "automation detected at email step — page did not advance past email field. Auto-retrying with fresh proxy IP.",
            "totpCode": totp_code,
            "debugScreenshot": shot,
        }

    if "signin/rejected" in url:
        shot = screenshot_b64()
        return {
            "status": "verification_required",
            "reason": "Google rejected sign-in (automation detected). Use a residential proxy.",
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
    TOTP_SELECTORS = [
        'input[name="totpPin"]', 'input[name="Pin"]', 'input[id="totpPin"]',
        'input[autocomplete="one-time-code"]', 'input[type="tel"]',
        'input[aria-label*="code"]', 'input[aria-label*="Code"]',
        'input[placeholder*="code"]', 'input[placeholder*="Code"]',
        'input[type="number"]',
    ]

    # True when Google has already landed on the TOTP input page directly
    # (e.g. "Verify that it's you — Get a verification code from Google Authenticator")
    # URL: challenge/totp, challenge/ipp, OR v3/signin/TL=... (same page, different URL format)
    _on_totp_url = (
        "challenge/totp" in url
        or "challenge/ipp" in url
        or ("v3/signin" in url and "v3/signin/identifier" not in url and "challenge" not in url)
    )

    # Detect direct TOTP-input page (input already visible).
    # If we know we're on a TOTP URL, wait briefly for the field — single
    # find_element with no wait misses the field when the page is still rendering.
    totp_field = None
    if _on_totp_url:
        totp_field = wait_for_any(TOTP_SELECTORS, timeout=8)
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
            totp_field = wait_for_any(TOTP_SELECTORS, timeout=15)
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
            totp_field = wait_for_any(TOTP_SELECTORS, timeout=18)

            # Fallback: try "Try another way" → then click authenticator again
            if totp_field is None:
                log(f"{email} — TOTP not found after first click, trying 'Try another way'")
                try:
                    driver.execute_script("""
                        var links = Array.from(document.querySelectorAll('a, button, [role="button"]'));
                        var found = links.find(function(el) {
                            var t = (el.innerText || '').toLowerCase();
                            return t.indexOf('another way') !== -1 || t.indexOf('different') !== -1
                                || t.indexOf('more options') !== -1 || t.indexOf('try again') !== -1;
                        });
                        if (found) found.click();
                    """)
                    rand_sleep(700, 1200)
                    _click_authenticator()
                    rand_sleep(700, 1200)
                    totp_field = wait_for_any(TOTP_SELECTORS, timeout=15)
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

        # CRITICAL: regenerate TOTP right before entry.
        # The original code was generated at check start (~60s ago) and may have expired.
        # TOTP codes rotate every 30 seconds — stale code = "wrong code" from Google.
        if totp_secret:
            fresh_code = generate_totp(totp_secret)
            if fresh_code:
                secs_left = 30 - (int(time.time()) % 30)
                if secs_left <= 4:
                    # Window ends in <4s — wait for next fresh window to avoid race
                    log(f"{email} — TOTP window ending in {secs_left}s, waiting for next window…")
                    time.sleep(secs_left + 1)
                    fresh_code = generate_totp(totp_secret)
                totp_code = fresh_code
                secs_remaining = 30 - (int(time.time()) % 30)
                log(f"{email} — Fresh TOTP code: {totp_code} ({secs_remaining}s left in window)")

        log(f"{email} — Entering TOTP code: {totp_code}")
        try:
            touch_click(driver, totp_field)   # TouchEvent tap to focus field
            rand_sleep(60, 120)
            totp_field.clear()
            rand_sleep(40, 80)
            clipboard_type(driver, totp_field, totp_code)  # paste TOTP code instantly
            rand_sleep(100, 200)
            totp_field.send_keys(Keys.ENTER)
        except Exception as e:
            log(f"TOTP entry error: {e}")

        rand_sleep(700, 1200)

        # Wait for Gmail to fully load (signin/continue is an auto-redirect page)
        log(f"{email} — Waiting for Gmail redirect after TOTP…")
        _totp_redirect_early = None
        _second_totp_done = False   # guard: only enter second TOTP once
        deadline = time.time() + 30
        while time.time() < deadline:
            url = driver.current_url
            if "mail.google.com" in get_hostname(url):
                break

            # ── Second TOTP: Google sometimes asks for TOTP twice ─────────────
            # After first TOTP is submitted, Google may redirect back to another
            # TOTP page (challenge/totp OR v3/signin/TL=...).  Re-enter fresh code.
            _on_second_totp = (
                "challenge/totp" in url
                or "challenge/ipp" in url
                or ("v3/signin" in url and "challenge" not in url and "v3/signin/identifier" not in url)
            )
            if _on_second_totp and not _second_totp_done:
                log(f"{email} — Second TOTP page detected ({url[:60]}), entering fresh code")
                _sec_code = generate_totp(totp_secret) if totp_secret else totp_code
                if _sec_code:
                    _sec_field = wait_for_any(TOTP_SELECTORS, timeout=6)
                    if _sec_field:
                        try:
                            _sec_field.clear()
                            rand_sleep(40, 80)
                            clipboard_type(driver, _sec_field, _sec_code)
                            rand_sleep(100, 200)
                            _sec_field.send_keys(Keys.ENTER)
                            log(f"{email} — Second TOTP entered: {_sec_code}")
                            rand_sleep(700, 1200)
                        except Exception as _ste:
                            log(f"{email} — Second TOTP entry error: {_ste}")
                else:
                    # No TOTP secret — can't enter code, mark opened per user rule
                    log(f"{email} — Second TOTP page, no secret → opened per user rule")
                    shot = screenshot_b64()
                    return {"status": "opened", "reason": "Mailbox opened successfully ✅", "totpCode": None, "debugScreenshot": shot}
                _second_totp_done = True
                continue  # restart loop with fresh URL check

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
                # Wait for next TOTP window and try once more
                secs_until_next = 30 - (int(time.time()) % 30)
                log(f"{email} — Wrong TOTP, waiting {secs_until_next}s for next window…")
                time.sleep(secs_until_next + 0.5)
                retry_code = generate_totp(totp_secret)
                if retry_code:
                    log(f"{email} — TOTP retry with fresh code: {retry_code}")
                    try:
                        totp_field_retry = wait_for_any([
                            'input[name="totpPin"]', 'input[name="Pin"]', 'input[id="totpPin"]',
                            'input[autocomplete="one-time-code"]', 'input[type="tel"]',
                            'input[aria-label*="code"]', 'input[type="number"]',
                        ], timeout=8)
                        if totp_field_retry:
                            totp_field_retry.clear()
                            rand_sleep(40, 80)
                            clipboard_type(driver, totp_field_retry, retry_code)
                            rand_sleep(100, 200)
                            totp_field_retry.send_keys(Keys.ENTER)
                            rand_sleep(700, 1200)
                            totp_code = retry_code
                            url, text = page_state()
                            log(f"{email} — After TOTP retry: {url[:70]}")
                            # Check again — if still wrong, give up
                            if any(x in text for x in _wrong_totp_phrases):
                                # v3/signin/TL=... page: user confirmed these accounts
                                # are accessible even when TOTP timing fails — mark opened.
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
                                    "reason": f"TOTP codes wrong on 2 attempts ({totp_code}, {retry_code}) — check secret",
                                    "totpCode": retry_code,
                                }
                    except Exception as _te:
                        log(f"{email} — TOTP retry error: {_te}")
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

        if "mail.google.com" in host:
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

        # gds.google.com — recovery options, home address, etc.
        # Click "Not now" / "Skip" / "Later" properly so the auth session finalises
        if "gds.google.com" in host:
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
                    driver.get("https://mail.google.com/mail/u/0/#inbox")
            except Exception as e:
                log(f"gds dismiss error: {e}")
                try:
                    driver.get("https://mail.google.com/mail/u/0/#inbox")
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
                    driver.get("https://mail.google.com/mail/h/?zy=e")
                    rand_sleep(800, 1200)
                    _html_url = driver.current_url
                    log(f"{email} — Gmail HTML URL: {_html_url[:70]}")
                    if "mail.google.com" in get_hostname(_html_url) and "uplevelingstep" not in _html_url:
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
                driver.get("https://mail.google.com/mail/u/0/#inbox")
            except Exception:
                pass
            dismissed = True

        # TOTP page reappeared — enter a fresh code and continue
        elif "challenge/totp" in url or "challenge/selection" in url:
            log(f"{email} — TOTP/selection page reappeared in interstitial loop, re-entering")
            if totp_secret:
                fresh_code = generate_totp(totp_secret)
                log(f"{email} — Fresh TOTP code: {fresh_code}")
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
                    driver.get("https://mail.google.com/mail/u/0/#inbox")
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
                driver.get("https://mail.google.com/mail/u/0/#inbox")
                dismissed = True
            except Exception:
                break

        if dismissed:
            rand_sleep(500, 800)
        else:
            break

    # Wait for Gmail to fully load
    deadline = time.time() + 12
    while time.time() < deadline:
        _cu = driver.current_url
        if "mail.google.com" in get_hostname(_cu):
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
