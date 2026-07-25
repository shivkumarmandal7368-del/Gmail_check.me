#!/usr/bin/env python3
"""
Manual browser session for the Vanguard MX UI.

The process receives JSON commands on stdin and emits JSON events on stdout:
  {"action":"navigate","url":"https://example.com"}
  {"action":"click","x":120,"y":240}
  {"action":"type","text":"..."}
  {"action":"key","key":"ENTER"}
  {"action":"screenshot"}
  {"action":"stop"}

Typed text is deliberately never printed.  Screenshots are sent to the UI so
the operator can control the browser visually without exposing a local Chrome
window through the Replit preview.
"""

import base64
import fcntl
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

sys.stdout.reconfigure(line_buffering=True)

sys.path.insert(0, os.path.dirname(__file__))
from gmail_uc_checker import (  # noqa: E402
    PHONE_PROFILES,
    _cleanup_stale_chrome_artifacts,
    _start_local_proxy,
    get_chrome_version,
    get_chrome_version_main,
    get_chromium_path,
    get_or_create_fingerprint,
    make_plugins_override_js,
    make_stealth_js,
    parse_proxy,
)


def event(kind, data=None):
    print(f"EVENT:{json.dumps({'type': kind, 'data': data}, separators=(',', ':'))}", flush=True)


def log(message):
    event("log", message)


def allowed_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    parsed = urlsplit(value.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc) and not parsed.username and not parsed.password


def start_xvfb():
    _cleanup_stale_chrome_artifacts()
    for number in range(120, 200):
        display = f":{number}"
        try:
            proc = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1366x900x24", "-ac"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.35)
            if proc.poll() is None:
                os.environ["DISPLAY"] = display
                return proc, display
            proc.wait(timeout=1)
        except FileNotFoundError:
            raise RuntimeError("Xvfb not found")
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    raise RuntimeError("No free Xvfb display was available")


def lock_file(path):
    fd = open(path, "w")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def make_driver(device_index, proxy):
    import undetected_chromedriver as uc

    profile = dict(PHONE_PROFILES[device_index])
    chromium_path = get_chromium_path()
    if not chromium_path:
        raise RuntimeError("Chrome for Testing binary is unavailable")
    chrome_version = get_chrome_version(chromium_path) or profile["chromeVersion"]
    profile["chromeVersion"] = chrome_version

    profile_dir = os.path.join(
        tempfile.gettempdir(), "vanguard_manual_profiles", f"session_{os.getpid()}"
    )
    os.makedirs(profile_dir, exist_ok=True)
    fingerprint = get_or_create_fingerprint(
        profile_dir,
        proxy=proxy,
        device_index=device_index,
        reuse_device_snapshot=True,
    )
    fingerprint["chromeVersion"] = chrome_version

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--window-size={fingerprint['screenW']},{fingerprint['screenH']}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions-http-throttling")
    options.add_argument("--password-store=basic")
    options.add_argument("--use-angle=swiftshader")
    options.add_argument("--enable-webgl")
    options.add_argument("--ignore-gpu-blocklist")
    options.add_argument("--disable-gpu-sandbox")
    options.add_argument("--disable-gpu-compositing")
    options.add_argument("--touch-events=enabled")
    options.add_argument(f"--force-device-scale-factor={fingerprint['dpr']}")
    options.add_argument("--lang=en-US,en;q=0.9")

    local_proxy = None
    parsed_proxy = parse_proxy(proxy) if proxy else None
    if parsed_proxy and parsed_proxy.get("host"):
        if parsed_proxy.get("username"):
            local_proxy, port = _start_local_proxy(
                parsed_proxy["host"],
                parsed_proxy["port"],
                parsed_proxy["username"],
                parsed_proxy.get("password") or "",
            )
            options.add_argument(f"--proxy-server=http://127.0.0.1:{port}")
        else:
            options.add_argument(
                f"--proxy-server=http://{parsed_proxy['host']}:{parsed_proxy['port']}"
            )

    launch_lock = lock_file("/tmp/gmail_checker_chrome_launch.lock")
    session_lock = lock_file("/tmp/gmail_checker_chrome_session.lock")
    try:
        driver = uc.Chrome(
            options=options,
            browser_executable_path=chromium_path,
            version_main=get_chrome_version_main(chromium_path),
            use_subprocess=True,
            log_level=1,
        )
    finally:
        fcntl.flock(launch_lock, fcntl.LOCK_UN)
        launch_lock.close()

    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": make_stealth_js(fingerprint)},
        )
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": make_plugins_override_js()},
        )
        ua = (
            f"Mozilla/5.0 (Linux; Android {fingerprint['androidVersion']}; {fingerprint['model']}) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{fingerprint['chromeVersion']} Mobile Safari/537.36"
        )
        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride",
            {
                "userAgent": ua,
                "acceptLanguage": fingerprint.get("language", "en-US"),
                "platform": "Linux aarch64",
                "userAgentMetadata": {
                    "brands": [
                        {"brand": "Not_A Brand", "version": "99"},
                        {"brand": "Google Chrome", "version": str(chrome_version).split(".")[0]},
                    ],
                    "fullVersion": chrome_version,
                    "platform": "Android",
                    "platformVersion": fingerprint["androidVersion"],
                    "model": fingerprint["model"],
                    "mobile": True,
                },
            },
        )
        driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": fingerprint["screenW"],
                "height": fingerprint["screenH"],
                "deviceScaleFactor": fingerprint["dpr"],
                "mobile": True,
            },
        )
        try:
            driver.execute_cdp_cmd(
                "Emulation.setTimezoneOverride",
                {"timezoneId": fingerprint.get("timezone", "America/New_York")},
            )
        except Exception:
            pass
    except Exception as exc:
        log(f"Browser fingerprint setup warning: {str(exc)[:160]}")

    return driver, local_proxy, session_lock, fingerprint


def screenshot(driver, fingerprint, label="screen"):
    try:
        raw = driver.get_screenshot_as_png()
        event(
            "screenshot",
            {
                "label": label,
                "b64": base64.b64encode(raw).decode("ascii"),
                "screenW": fingerprint["screenW"],
                "screenH": fingerprint["screenH"],
                "url": driver.current_url,
                "title": driver.title[:160],
            },
        )
    except Exception as exc:
        log(f"Screenshot failed: {str(exc)[:180]}")


def click_at(driver, x, y):
    # Coordinates are CSS viewport coordinates, matching the dimensions shown
    # by the UI. elementFromPoint also works for mobile-emulated pages where a
    # Selenium mouse offset would be affected by the device scale factor.
    return driver.execute_script(
        """
        const el = document.elementFromPoint(arguments[0], arguments[1]);
        if (!el) return false;
        el.focus();
        el.click();
        return true;
        """,
        float(x),
        float(y),
    )


def handle_key(driver, key):
    from selenium.webdriver.common.keys import Keys

    mapping = {
        "ENTER": Keys.ENTER,
        "TAB": Keys.TAB,
        "ESC": Keys.ESCAPE,
        "ESCAPE": Keys.ESCAPE,
        "BACKSPACE": Keys.BACKSPACE,
        "DELETE": Keys.DELETE,
        "ARROWUP": Keys.ARROW_UP,
        "ARROWDOWN": Keys.ARROW_DOWN,
        "ARROWLEFT": Keys.ARROW_LEFT,
        "ARROWRIGHT": Keys.ARROW_RIGHT,
    }
    value = mapping.get(str(key).upper(), str(key)[:1])
    driver.switch_to.active_element.send_keys(value)


def main():
    try:
        raw_index = int(os.environ.get("DEVICE_INDEX", "0"))
    except ValueError:
        raw_index = 0
    device_index = raw_index if 0 <= raw_index < len(PHONE_PROFILES) else 0
    proxy = (
        os.environ.get("PROXY_OVERRIDE", "").strip()
        or os.environ.get("PROXY_URL", "").strip()
        or os.environ.get("Proxy", "").strip()
    )

    xvfb = None
    driver = None
    local_proxy = None
    session_lock = None
    try:
        xvfb, display = start_xvfb()
        log(f"Starting {PHONE_PROFILES[device_index]['model']} on {display}")
        driver, local_proxy, session_lock, fingerprint = make_driver(device_index, proxy)
        event(
            "ready",
            {
                "device": PHONE_PROFILES[device_index]["model"],
                "screenW": fingerprint["screenW"],
                "screenH": fingerprint["screenH"],
            },
        )
        screenshot(driver, fingerprint, "ready")

        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                command = json.loads(line)
                action = command.get("action")
                if action == "stop":
                    break
                if action == "navigate":
                    target = str(command.get("url", "")).strip()
                    if not allowed_url(target):
                        log("Navigation blocked: enter a valid HTTP(S) URL without embedded credentials")
                        continue
                    driver.get(target)
                    log(f"Opened {urlsplit(target).netloc}")
                    screenshot(driver, fingerprint, "navigate")
                elif action == "click":
                    x, y = float(command.get("x", 0)), float(command.get("y", 0))
                    if not (0 <= x <= fingerprint["screenW"] and 0 <= y <= fingerprint["screenH"]):
                        log("Click ignored: coordinates are outside the device viewport")
                        continue
                    if click_at(driver, x, y):
                        log(f"Clicked viewport coordinate ({int(x)}, {int(y)})")
                    else:
                        log("Click target not found")
                    time.sleep(0.2)
                    screenshot(driver, fingerprint, "click")
                elif action == "type":
                    text = command.get("text")
                    if not isinstance(text, str) or len(text) > 4000:
                        log("Type ignored: text must be 4,000 characters or fewer")
                        continue
                    # Never log or echo this value.
                    driver.switch_to.active_element.send_keys(text)
                    log("Typed text into the focused browser field (value not saved)")
                    screenshot(driver, fingerprint, "type")
                elif action == "key":
                    handle_key(driver, command.get("key", ""))
                    log("Key sent to the focused browser field")
                    screenshot(driver, fingerprint, "key")
                elif action == "screenshot":
                    screenshot(driver, fingerprint, "manual")
                else:
                    log("Unknown browser action")
            except Exception as exc:
                log(f"Browser action failed: {str(exc)[:220]}")
    except Exception as exc:
        event("error", str(exc)[:400])
    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass
        if local_proxy:
            try:
                local_proxy.shutdown()
                local_proxy.server_close()
            except Exception:
                pass
        if session_lock:
            try:
                fcntl.flock(session_lock, fcntl.LOCK_UN)
                session_lock.close()
            except Exception:
                pass
        if xvfb:
            try:
                xvfb.terminate()
                xvfb.wait(timeout=3)
            except Exception:
                try:
                    xvfb.kill()
                except Exception:
                    pass
        event("closed", {})


if __name__ == "__main__":
    main()