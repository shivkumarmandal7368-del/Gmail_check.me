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
from selenium.webdriver.common.action_chains import ActionChains

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
        get_chrome_version,
        get_chrome_version_main,
        get_chromium_path,
        make_stealth_js,
        make_plugins_override_js,
        parse_proxy,
        DEVICE_FINGERPRINT_SNAPSHOT_PATH,
        _cleanup_stale_chrome_artifacts,
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

profile = dict(PHONE_PROFILES[device_index])
model   = profile["model"]
android = profile["androidVersion"]
chromium_path = get_chromium_path()
chrome = get_chrome_version(chromium_path) or profile["chromeVersion"]
profile["chromeVersion"] = chrome

UA = (
    f"Mozilla/5.0 (Linux; Android {android}; {model}) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{chrome} Mobile Safari/537.36"
)

# ── Pre-launch: clean stale Xvfb / Chrome artifacts ──────────────────────────
# Removes dead-PID lock files, orphaned Xvfb processes, and Chrome singleton
# files left by crashed prior sessions.  Must run before Xvfb starts so a
# stale :91 lock can't block the fresh launch.
_cleanup_stale_chrome_artifacts()
# Also kill any stale Xvfb specifically on :91 (fixed display used here)
for _p in ("/tmp/.X91-lock", "/tmp/.X11-unix/X91"):
    if os.path.exists(_p) or os.path.islink(_p):
        try:
            if _p.endswith("-lock"):
                _stale_pid = int(open(_p).read().strip())
                try:
                    os.kill(_stale_pid, 9)  # SIGKILL
                except Exception:
                    pass
            os.remove(_p)
            log(f"Removed stale artifact: {_p}")
        except Exception:
            pass

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
PROXY_URL = os.environ.get("PROXY_OVERRIDE", "").strip() or os.environ.get("PROXY_URL", "").strip() or os.environ.get("Proxy", "").strip()
if not PROXY_URL:
    print(
        "ERROR:No proxy configured. Select Custom and enter the residential proxy URL, "
        "or configure the PROXY_URL secret before running Device Check.",
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
o.add_argument("--use-angle=swiftshader")
o.add_argument("--enable-webgl")
o.add_argument("--ignore-gpu-blocklist")
o.add_argument("--disable-gpu-sandbox")
# Disable GPU-backed PAGE COMPOSITING (the source of renderer crashes under Xvfb)
# while keeping the GPU process alive for WebGL.
# --disable-gpu would kill the GPU process entirely, making WebGL unavailable
# and causing fingerprint.com's VM detector to fire (WebGL context = null is a VM signal).
o.add_argument("--disable-gpu-compositing")
o.add_argument("--disable-blink-features=AutomationControlled")
# Chrome 151 exits its renderer under Xvfb when this legacy feature bundle is
# passed, so leave it unset for Device Check stability.
o.add_argument("--disable-extensions-http-throttling")
o.add_argument("--no-default-browser-check")
o.add_argument("--no-first-run")
o.add_argument("--disable-sync")
o.add_argument("--password-store=basic")
o.add_argument(f"--proxy-server=http://127.0.0.1:{local_proxy_port}")

log("Launching Chrome...")
chromium_path = get_chromium_path()
chrome_version_main = get_chrome_version_main(chromium_path)
log(f"Chrome binary: {chromium_path} (version_main={chrome_version_main})")

try:
    driver = uc.Chrome(
        options=o,
        browser_executable_path=chromium_path,
        version_main=chrome_version_main,
        use_subprocess=True,
        log_level=1,
    )
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
    "geoLocked": True,
    "ip": proxy_exit,
})
# Keep an exact local handoff for Browser Check. This file contains only the
# selected device fingerprint; it never contains account credentials or proxy
# credentials.
try:
    with open(f"{DEVICE_FINGERPRINT_SNAPSHOT_PATH}.tmp", "w") as snapshot_file:
        json.dump({**device_fp, "deviceIndex": device_index}, snapshot_file, indent=2)
    os.replace(f"{DEVICE_FINGERPRINT_SNAPSHOT_PATH}.tmp", DEVICE_FINGERPRINT_SNAPSHOT_PATH)
    log("Exact Device Check fingerprint saved for Browser Check ✓")
except Exception as snapshot_error:
    log(f"Device fingerprint handoff skipped (non-fatal): {snapshot_error}")
log(
    f"Browser profile applied → {model} | Android {android} | "
    f"{profile['webglVendor']} {profile['webglRenderer']} | "
    f"{profile['screenW']}x{profile['screenH']} dpr={profile['dpr']}"
)
try:
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": make_stealth_js(device_fp),
    })
    # Second-pass inject: plugins/mimeTypes override runs after main stealth JS
    # so that Chrome's C++ has already made Navigator.prototype.plugins configurable.
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": make_plugins_override_js(),
    })
    log("Device fingerprint patches injected ✓")
except Exception as e:
    print(f"ERROR:Device fingerprint injection failed: {str(e)[:300]}", flush=True)
    driver.quit()
    xvfb.terminate()
    sys.exit(1)

# ── CDP overrides (mirrors checker startup block) ──────────────────────────────
cv_major = chrome.split(".")[0]
    # NOTE: Network.enable + Network.setUserAgentOverride intentionally removed.
    # Activating the CDP Network domain leaves a detectable trace that fingerprint.com
    # uses for its "developer tools" signal (weight 8). Emulation.setUserAgentOverride
    # (below) covers both the JS navigator.userAgent and the HTTP User-Agent header,
    # including Sec-CH-UA via userAgentMetadata, making Network.setUserAgentOverride
    # redundant. Avoid enabling any CDP domain that is not strictly necessary.
try:
    driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "America/New_York"})
    driver.execute_cdp_cmd("Emulation.setLocaleOverride",   {"locale": "en-US"})
    # Override hardware concurrency at the CDP level so it matches JS patches
    try:
        driver.execute_cdp_cmd("Emulation.setHardwareConcurrencyOverride",
                               {"hardwareConcurrency": profile["hwConcurrency"]})
    except Exception:
        pass  # available in Chrome 102+; non-fatal if missing
    # Set device metrics so CSS media queries see the mobile screen size,
    # not the Xvfb desktop resolution (prevents CSS-based VM signal)
    try:
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "width":             profile["screenW"],
            "height":            profile["screenH"],
            "deviceScaleFactor": profile["dpr"],
            "mobile":            True,
            "hasTouch":          True,
            "screenWidth":       profile["screenW"],
            "screenHeight":      profile["screenH"],
        })
    except Exception:
        pass
    # Touch emulation: sets navigator.maxTouchPoints at the native C++ level.
    # Without this, maxTouchPoints stays 0 even with --touch-events=enabled and
    # the JS prototype patch — fingerprint.com reads the C++-level value.
    try:
        driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {
            "enabled":        True,
            "maxTouchPoints": profile["maxTouchPoints"],
        })
    except Exception:
        pass
    # Emulation.setUserAgentOverride sets navigator.userAgent + navigator.platform
    # at the CDP emulation level (deeper than Network.setUserAgentOverride).
    # fingerprint.com compares CDP-level platform against JS-reported platform —
    # both must agree on "Linux armv8l" to avoid the VM mismatch signal.
    # CRITICAL: Must include userAgentMetadata — omitting it causes Chrome 151 to
    # clear navigator.userAgentData.brands to [], mobile→false, platform→'' (Tampering signal).
    try:
        driver.execute_cdp_cmd("Emulation.setUserAgentOverride", {
            "userAgent": UA,
            "acceptLanguage": "en-US,en;q=0.9",
            "platform": profile["platform"],
            "userAgentMetadata": {
                "brands": [
                    {"brand": "Not(A;Brand",   "version": "8"},
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
    except Exception:
        pass
    log("CDP overrides applied (UA, timezone, locale, hwConcurrency, deviceMetrics, emuUA)")
except Exception as e:
    log(f"CDP override warning (non-fatal): {e}")

# ── Simulate human-like mouse movement to defeat bot detection ────────────────
try:
    actions = ActionChains(driver)
    body = driver.find_element(By.TAG_NAME, "body")
    for _ in range(5):
        x = random.randint(20, min(profile["screenW"] - 20, 360))
        y = random.randint(20, min(profile["screenH"] - 20, 300))
        actions.move_to_element_with_offset(body, x, y)
        actions.pause(random.uniform(0.08, 0.25))
    actions.perform()
    log("Mouse movements simulated ✓")
except Exception as e:
    log(f"Mouse simulation skipped (non-fatal): {e}")

# ── Diagnostic: navigate to about:blank (triggers stealth JS), read runtime values ──
log("Running stealth JS diagnostic...")
try:
    driver.get("about:blank")
    import time as _t; _t.sleep(0.5)
    import json as _json
    diag = driver.execute_script("""
        var pa_desc = Object.getOwnPropertyDescriptor(PluginArray.prototype, 'length');
        var ma_desc = Object.getOwnPropertyDescriptor(MimeTypeArray.prototype, 'length');
        var np_own  = Object.getOwnPropertyDescriptor(navigator, 'plugins');
        var np_prot = Object.getOwnPropertyDescriptor(Navigator.prototype, 'plugins');
        return {
            plugins_length:       navigator.plugins.length,
            mimeTypes_length:     navigator.mimeTypes.length,
            maxTouchPoints:       navigator.maxTouchPoints,
            deviceMemory:         navigator.deviceMemory,
            platform:             navigator.platform,
            hardwareConcurrency:  navigator.hardwareConcurrency,
            webdriver:            navigator.webdriver,
            plugins_instanceof:   (navigator.plugins instanceof PluginArray),
            mimeTypes_instanceof: (navigator.mimeTypes instanceof MimeTypeArray),
            PluginArray_proto_length: pa_desc ? {
                configurable: pa_desc.configurable,
                is_js_fn:     typeof pa_desc.get === 'function',
                toString:     pa_desc.get ? pa_desc.get.toString().substring(0,120) : null
            } : 'no own desc',
            MimeTypeArray_proto_length: ma_desc ? {
                configurable: ma_desc.configurable,
                is_js_fn:     typeof ma_desc.get === 'function',
                toString:     ma_desc.get ? ma_desc.get.toString().substring(0,120) : null
            } : 'no own desc',
            nav_plugins_own:  np_own  ? (typeof np_own.get === 'function' ? 'own-js-getter:'+np_own.get.toString().substring(0,60) : 'own-value') : 'no own',
            nav_plugins_proto: np_prot ? (typeof np_prot.get === 'function' ? 'proto-js-getter:cfg='+np_prot.configurable+':'+np_prot.get.toString().substring(0,60) : 'non-getter') : 'not found',
            window_nav_replaced: (function(){ try{ return Object.getOwnPropertyDescriptor(window,'navigator') ? (typeof Object.getOwnPropertyDescriptor(window,'navigator').get === 'function' ? 'yes-getter' : 'value') : 'not-own'; }catch(e){return 'err:'+e;} })(),
            userAgentData: navigator.userAgentData ? JSON.stringify({
                mobile:   navigator.userAgentData.mobile,
                platform: navigator.userAgentData.platform,
                brands:   navigator.userAgentData.brands
            }) : 'null/undefined',
            deviceMemory_nav_own: (function(){
                var d=Object.getOwnPropertyDescriptor(navigator,'deviceMemory');
                return d ? JSON.stringify({configurable:d.configurable,value:d.value,hasGetter:typeof d.get==='function'}) : 'no own desc';
            })(),
            deviceMemory_proto: (function(){
                var d=Object.getOwnPropertyDescriptor(Navigator.prototype,'deviceMemory');
                return d ? JSON.stringify({configurable:d.configurable,hasGetter:typeof d.get==='function',str:d.get?d.get.toString().substring(0,80):null}) : 'no proto desc';
            })(),
            userAgentData_proto: (function(){
                var d=Object.getOwnPropertyDescriptor(Navigator.prototype,'userAgentData');
                return d ? JSON.stringify({configurable:d.configurable,hasGetter:typeof d.get==='function',str:d.get?d.get.toString().substring(0,80):null}) : 'no proto desc';
            })()
        };
    """)
    log(f"[DIAG] {_json.dumps(diag)}")
except Exception as e:
    log(f"Diagnostic skipped (non-fatal): {e}")

# ── Navigate to fingerprint.com ────────────────────────────────────────────────
log("Navigating to fingerprint.com...")
try:
    driver.get("https://fingerprint.com/")
except Exception as e:
    print(f"ERROR:Navigation failed: {str(e)[:300]}", flush=True)
    driver.quit()
    xvfb.terminate()
    sys.exit(1)

log("Waiting 16s for fingerprint widget to fully render...")
time.sleep(16)

# Screenshot 1 — homepage with score visible
driver.get_screenshot_as_file("/tmp/dc_1_homepage.png")
emit_screenshot("/tmp/dc_1_homepage.png", "homepage")

# ── Click the Suspect Score expand button to open the breakdown ───────────────
# fingerprint.com renders a score card with an expand/arrow button (↗).
# Try several selectors in priority order until one works.
clicked = False

def _try_click(driver, label, js_finder):
    """Run js_finder to get an element, scroll it into view, click it. Returns True on success."""
    try:
        el = driver.execute_script(js_finder)
        if not el:
            return False
        driver.execute_script("arguments[0].scrollIntoView({block:'center', behavior:'instant'});", el)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", el)
        log(f"Clicked via: {label}")
        return True
    except Exception as ex:
        log(f"Click attempt '{label}' failed: {ex}")
        return False

# 1. The small expand / arrow button that sits inside the Suspect Score card
clicked = _try_click(driver, "expand-svg-button",
    "var btns=document.querySelectorAll('button,a,[role=button]');"
    "for(var i=0;i<btns.length;i++){"
    "  var t=btns[i].innerText||''; var h=btns[i].innerHTML||'';"
    "  if(t.includes('Suspect')||h.includes('Suspect')){return btns[i];}"
    "} return null;")

if not clicked:
    # 2. Any element whose text contains "Suspect Score"
    clicked = _try_click(driver, "suspect-score-text",
        "var all=document.querySelectorAll('*');"
        "for(var i=0;i<all.length;i++){"
        "  if((all[i].childElementCount===0||all[i].tagName==='SPAN'||all[i].tagName==='DIV')"
        "  &&all[i].innerText&&all[i].innerText.trim().startsWith('Suspect Score')){return all[i];}"
        "} return null;")

if not clicked:
    # 3. Fallback — any element containing "calculated"
    clicked = _try_click(driver, "calculated-text",
        "var all=document.querySelectorAll('*');"
        "for(var i=0;i<all.length;i++){"
        "  if(all[i].childElementCount===0&&all[i].innerText&&"
        "  all[i].innerText.toLowerCase().includes('calculated')){return all[i];}"
        "} return null;")

if not clicked:
    log("WARNING: could not find a clickable score element — taking fallback screenshot")

log("Waiting 10s for breakdown panel to open...")
time.sleep(10)

# Screenshot 2 — after clicking the score badge (breakdown panel should be open)
driver.get_screenshot_as_file("/tmp/dc_2_breakdown.png")
emit_screenshot("/tmp/dc_2_breakdown.png", "breakdown")

# ── Screenshot 3 — scroll INSIDE the breakdown panel (not the whole page) ────
# The breakdown is a floating card / modal with its own scroll container.
# Scroll only that container so we see the individual signal rows, not the footer.
driver.execute_script("""
    // Find the deepest scroll container that is NOT the body/html/documentElement
    // and that has scrollable overflow — this is the breakdown panel.
    var best = null;
    var bestH = 0;
    var all = document.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
        var el = all[i];
        if (el === document.body || el === document.documentElement) continue;
        var st = window.getComputedStyle(el);
        var ov = st.overflowY || st.overflow;
        if ((ov === 'scroll' || ov === 'auto') && el.scrollHeight > el.clientHeight + 10) {
            // Prefer the tallest scrollable container (the panel, not tiny dropdowns)
            if (el.scrollHeight > bestH) { best = el; bestH = el.scrollHeight; }
        }
    }
    if (best) {
        best.scrollTop = best.scrollHeight;
    } else {
        // No inner scroll container found — scroll the window modestly
        // (avoid going to the footer by capping at 600px)
        window.scrollBy(0, 600);
    }
""")
time.sleep(3)
driver.get_screenshot_as_file("/tmp/dc_3_scrolled.png")
emit_screenshot("/tmp/dc_3_scrolled.png", "scrolled")

driver.quit()
proxy_server.shutdown()
proxy_server.server_close()
xvfb.terminate()
log("Chrome and Xvfb closed cleanly")
print("DONE", flush=True)
