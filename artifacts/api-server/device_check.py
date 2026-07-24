#!/usr/bin/env python3
"""
Device Check — fingerprint.com audit script for Vanguard MX.

Env vars accepted:
  DEVICE_INDEX    integer index into PHONE_PROFILES (default 0 = Pixel 6)
  PROXY_OVERRIDE  full proxy URL — takes priority over the 'Proxy' secret
  Proxy           Replit secret fallback proxy URL

Line protocol (stdout):
  LOG:<message>               progress line → SSE 'log' event
  SCREENSHOT:<label>:<base64> PNG → SSE 'screenshot' event
  DONE                        finished cleanly
  ERROR:<msg>                 fatal error
"""
import os, sys, time, base64, subprocess, random, json
import socket, socketserver, threading, select
from urllib.parse import urlsplit

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

# ── Load PHONE_PROFILES from the checker ──────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
try:
    from gmail_uc_checker import (
        PHONE_PROFILES,
        make_stealth_js,
        parse_proxy,
    )
except Exception as e:
    print(f"ERROR:Cannot import PHONE_PROFILES: {e}", flush=True)
    sys.exit(1)

# ── Select device ──────────────────────────────────────────────────────────────
try:
    device_index = int(os.environ.get("DEVICE_INDEX", "0"))
    if device_index < 0 or device_index >= len(PHONE_PROFILES):
        device_index = 0
except ValueError:
    device_index = 0

profile = PHONE_PROFILES[device_index]
model   = profile["model"]
android = profile["androidVersion"]
chrome  = profile["chromeVersion"]

UA = (
    f"Mozilla/5.0 (Linux; Android {android}; {model}) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{chrome} Mobile Safari/537.36"
)

# ── Start Xvfb on :91 ─────────────────────────────────────────────────────────
DISPLAY_NUM = ":91"
log(f"Device #{device_index}: {model} / Android {android} / Chrome {chrome}")
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

try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
except ImportError as e:
    print(f"ERROR:Missing dependency: {e}. Run: pip install undetected-chromedriver selenium", flush=True)
    xvfb.terminate()
    sys.exit(1)

# ── Proxy — PROXY_OVERRIDE env takes priority over Proxy secret ───────────────
PROXY_URL = os.environ.get("PROXY_OVERRIDE", "").strip() or os.environ.get("Proxy", "").strip()
if not PROXY_URL:
    print(
        "ERROR:No proxy configured. Select Custom and enter the residential proxy URL, "
        "or configure the Proxy secret before running Device Check.",
        flush=True,
    )
    xvfb.terminate()
    sys.exit(1)

# Validate the exact proxy before launching Chrome. Without this guard Chrome can
# fall back to the Replit datacenter IP while the audit still appears successful.
proxy_info = parse_proxy(PROXY_URL)
if not proxy_info or not proxy_info.get("host"):
    print("ERROR:Invalid proxy URL. Use http://USERNAME:PASSWORD@HOST:PORT", flush=True)
    xvfb.terminate()
    sys.exit(1)

try:
    import requests
    # Ignore any ambient environment proxy so this is a genuine Replit-origin
    # baseline. The browser must not report this IP.
    direct_session = requests.Session()
    direct_session.trust_env = False
    direct_exit = direct_session.get(
        "https://api.ipify.org?format=json",
        timeout=15,
    ).json().get("ip")
    proxy_session = requests.Session()
    proxy_session.trust_env = False
    proxy_exit = proxy_session.get(
        "https://api.ipify.org?format=json",
        proxies={"http": PROXY_URL, "https": PROXY_URL},
        timeout=15,
    ).json().get("ip")
    if not proxy_exit:
        raise RuntimeError("proxy response did not include an exit IP")
    log(f"Proxy validated ✓ — proxy exit IP: {proxy_exit} (direct Replit IP: {direct_exit or 'unavailable'})")
except Exception as e:
    print(f"ERROR:Proxy validation failed — {str(e)[:300]}", flush=True)
    xvfb.terminate()
    sys.exit(1)

ext_path = None

# Chrome's MV2 proxy-auth extension is not consistently applied by Chromium
# 138 in this environment. Use a small local forwarder instead: Chrome talks
# only to localhost, and the forwarder authenticates every request upstream.
class _AuthenticatedProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(30)
        upstream = self.server.upstream  # type: ignore[attr-defined]
        try:
            initial = self._read_headers(client)
            if not initial:
                return
            first_line, headers, remainder = self._split_request(initial)
            parts = first_line.split(" ", 2)
            if len(parts) != 3:
                return
            method, target, version = parts

            if method.upper() == "CONNECT":
                host, port = self._host_port(target, 443)
                remote = socket.create_connection(
                    (upstream_host, upstream_port), timeout=20
                )
                self._send_upstream_connect(remote, target, version)
                response = self._read_headers(remote)
                client.sendall(response)
                if not response.startswith(b"HTTP/1.1 200") and not response.startswith(b"HTTP/1.0 200"):
                    remote.close()
                    return
                self._relay(client, remote)
                return

            host, port = self._host_port_from_url(target, headers)
            remote = socket.create_connection(
                (upstream_host, upstream_port), timeout=20
            )
            forwarded = self._rewrite_http_request(first_line, headers, remainder)
            remote.sendall(forwarded)
            self._relay(client, remote)
        except Exception:
            return

    @staticmethod
    def _read_headers(sock):
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 128 * 1024:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
        return data

    @staticmethod
    def _split_request(data):
        head, _, remainder = data.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        first_line = lines[0].decode("latin1")
        headers = []
        for line in lines[1:]:
            if b":" in line:
                key, value = line.split(b":", 1)
                headers.append((key.decode("latin1"), value.decode("latin1").lstrip()))
        return first_line, headers, remainder

    @staticmethod
    def _host_port(target, default_port):
        if target.startswith("["):
            end = target.find("]")
            host = target[1:end]
            port = int(target[end + 2:]) if len(target) > end + 2 else default_port
            return host, port
        if ":" in target:
            host, raw_port = target.rsplit(":", 1)
            return host, int(raw_port)
        return target, default_port

    def _host_port_from_url(self, target, headers):
        parsed = urlsplit(target)
        if parsed.hostname:
            return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        host = next((v for k, v in headers if k.lower() == "host"), "")
        return self._host_port(host, 80)

    def _auth_header(self):
        raw = f"{upstream_user}:{upstream_password}".encode()
        return "Proxy-Authorization", "Basic " + base64.b64encode(raw).decode()

    def _send_upstream_connect(self, remote, target, version):
        key, value = self._auth_header()
        remote.sendall(
            f"CONNECT {target} {version}\r\nHost: {target}\r\n"
            f"{key}: {value}\r\nProxy-Connection: Keep-Alive\r\n\r\n".encode()
        )

    def _rewrite_http_request(self, first_line, headers, remainder):
        key, value = self._auth_header()
        output = [first_line.encode("latin1")]
        has_auth = False
        for header_key, header_value in headers:
            if header_key.lower() == "proxy-authorization":
                has_auth = True
                output.append(f"{key}: {value}".encode("latin1"))
            else:
                output.append(f"{header_key}: {header_value}".encode("latin1"))
        if not has_auth:
            output.append(f"{key}: {value}".encode("latin1"))
        return b"\r\n".join(output) + b"\r\n\r\n" + remainder

    @staticmethod
    def _relay(left, right):
        sockets = [left, right]
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, 30)
            if exceptional:
                return
            if not readable:
                continue
            for source in readable:
                data = source.recv(64 * 1024)
                if not data:
                    return
                destination = right if source is left else left
                destination.sendall(data)


class _ThreadedProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


upstream_user = proxy_info.get("username") or ""
upstream_password = proxy_info.get("password") or ""
upstream_host = proxy_info["host"]
upstream_port = proxy_info["port"]
proxy_server = _ThreadedProxyServer(("127.0.0.1", 0), _AuthenticatedProxyHandler)
proxy_server.upstream = proxy_info  # type: ignore[attr-defined]
threading.Thread(target=proxy_server.serve_forever, daemon=True).start()
local_proxy_port = proxy_server.server_address[1]
log(f"Local authenticated proxy ready on 127.0.0.1:{local_proxy_port}")

# ── Chrome options ─────────────────────────────────────────────────────────────
o = uc.ChromeOptions()
o.add_argument("--no-sandbox")
o.add_argument("--disable-dev-shm-usage")
o.add_argument(f"--window-size={profile['screenW']},{profile['screenH']}")
o.add_argument(f"--user-agent={UA}")
o.add_argument("--touch-events=enabled")
o.add_argument(f"--force-device-scale-factor={profile['dpr']}")
o.add_argument("--lang=en-US,en;q=0.9")
o.add_argument("--disable-gpu")
o.add_argument(f"--proxy-server=http://127.0.0.1:{local_proxy_port}")

log("Launching Chrome...")
try:
    driver = uc.Chrome(options=o, version_main=138)
except Exception as e:
    print(f"ERROR:Chrome launch failed: {str(e)[:400]}", flush=True)
    xvfb.terminate()
    sys.exit(1)

time.sleep(5)
log("Chrome launched ✓")

# Confirm the browser itself is using the proxy. Rotating providers may return a
# different residential IP for a second connection, so compare against the
# direct Replit baseline rather than requiring preflight and Chrome to match.
try:
    driver.get("https://api.ipify.org?format=json")
    browser_ip = json.loads(driver.find_element(By.TAG_NAME, "body").text).get("ip")
    if not browser_ip:
        raise RuntimeError("browser response did not include an exit IP")
    log(f"Chrome proxy verified ✓ — browser exit IP: {browser_ip}")
    if direct_exit and browser_ip == direct_exit:
        raise RuntimeError(
            f"browser exit IP {browser_ip} is the direct Replit IP, not the proxy"
        )
except Exception as e:
    print(f"ERROR:Chrome proxy verification failed — {str(e)[:300]}", flush=True)
    try:
        driver.quit()
    except Exception:
        pass
    if ext_path:
        try:
            os.unlink(ext_path)
        except OSError:
            pass
    proxy_server.shutdown()
    proxy_server.server_close()
    xvfb.terminate()
    sys.exit(1)

# ── Apply the selected real-phone profile before the first navigation ─────────
# Device Check must use the same browser fingerprint engine as Browser Check;
# changing only the UA leaves WebGL, navigator, canvas, and hardware signals
# identifying the Replit Chromium host.
device_fp = dict(profile)
device_fp.update({
    "canvasSeed": random.randint(1, 65535),
    "audioNoise": round(random.uniform(0.00001, 0.00009), 8),
    "webglNoise": round(random.uniform(0.000001, 0.000009), 8),
    "timezone": "America/New_York",
    "language": "en-US",
    "countryCode": "US",
    "batteryLevel": round(random.uniform(0.25, 0.92), 2),
    "batteryCharging": False,
    "dischargingTime": random.randint(2400, 28800),
    "connectionRtt": random.randint(8, 35),
    "connectionDownlink": round(random.uniform(25.0, 120.0), 1),
    "historyLength": random.randint(3, 14),
    "doNotTrack": None,
    "lat": 39.8283,
    "lon": -98.5795,
})
log(
    f"Browser profile applied → {model} | Android {android} | "
    f"{profile['webglVendor']} {profile['webglRenderer']} | "
    f"{profile['screenW']}x{profile['screenH']} dpr={profile['dpr']}"
)
try:
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": make_stealth_js(device_fp),
    })
    log("Device fingerprint patches injected ✓")
except Exception as e:
    print(f"ERROR:Device fingerprint injection failed: {str(e)[:300]}", flush=True)
    driver.quit()
    xvfb.terminate()
    sys.exit(1)

# ── CDP overrides (mirrors checker startup block) ──────────────────────────────
cv_major = chrome.split(".")[0]
try:
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {
        "userAgent": UA,
        "acceptLanguage": "en-US,en;q=0.9",
        "platform": profile["platform"],
        "userAgentMetadata": {
            "brands": [
                {"brand": "Not(A;Brand",  "version": "8"},
                {"brand": "Chromium",      "version": cv_major},
                {"brand": "Google Chrome", "version": cv_major},
            ],
            "fullVersion": chrome,
            "platform": "Android",
            "platformVersion": android,
            "architecture": "",
            "model": model,
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

# ── Navigate to fingerprint.com ────────────────────────────────────────────────
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

# Screenshot 1 — homepage with score
driver.get_screenshot_as_file("/tmp/dc_1_homepage.png")
emit_screenshot("/tmp/dc_1_homepage.png", "homepage")

# ── Click "See how this is calculated" ────────────────────────────────────────
calculated_els = driver.find_elements(By.XPATH, "//*[contains(text(),'calculated')]")
log(f"Found {len(calculated_els)} element(s) with 'calculated'")

if calculated_els:
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", calculated_els[0])
    time.sleep(1)
    driver.execute_script("arguments[0].click();", calculated_els[0])
    log("Clicked 'See how this is calculated' — waiting 10s...")
    time.sleep(10)
    driver.get_screenshot_as_file("/tmp/dc_2_breakdown.png")
    emit_screenshot("/tmp/dc_2_breakdown.png", "breakdown")
else:
    log("WARNING: 'calculated' element not found — saving fallback screenshot")
    driver.get_screenshot_as_file("/tmp/dc_2_breakdown.png")
    emit_screenshot("/tmp/dc_2_breakdown.png", "breakdown")

# ── Scroll and third screenshot ────────────────────────────────────────────────
driver.execute_script("window.scrollBy(0, 300)")
time.sleep(2)
driver.get_screenshot_as_file("/tmp/dc_3_scrolled.png")
emit_screenshot("/tmp/dc_3_scrolled.png", "scrolled")

driver.quit()
proxy_server.shutdown()
proxy_server.server_close()
xvfb.terminate()
log("Chrome and Xvfb closed cleanly")
print("DONE", flush=True)
