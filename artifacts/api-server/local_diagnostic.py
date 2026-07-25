#!/usr/bin/env python3
"""
Vanguard MX — Local Browser Fingerprint Diagnostic Tool
========================================================
Checks all major bot-detection signals locally WITHOUT hitting fingerprint.com.
Covers: navigator consistency, Function.toString tampering, property descriptors,
CDP/automation leftovers, WebGL/Canvas, screen/viewport, permissions, timing.

Usage:
  python3 local_diagnostic.py [device_index]  (default: 0 = Pixel 6)

Proxy is read from PROXY_URL or Proxy env/secret.
"""

import os, sys, time, subprocess, json, random, socket, socketserver, threading, select, base64
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(line_buffering=True)

from gmail_uc_checker import (
    PHONE_PROFILES,
    get_chrome_version,
    get_chrome_version_main,
    get_chromium_path,
    make_stealth_js,
    make_plugins_override_js,
    parse_proxy,
    _find_free_port,
    _cleanup_stale_chrome_artifacts,
)

# ── Device selection ────────────────────────────────────────────────────────────
device_index = 0
try:
    device_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if device_index < 0 or device_index >= len(PHONE_PROFILES):
        device_index = 0
except ValueError:
    pass

profile = dict(PHONE_PROFILES[device_index])
chromium_path = get_chromium_path()
chrome_full   = get_chrome_version(chromium_path) or profile["chromeVersion"]
chrome_main   = get_chrome_version_main(chromium_path)
profile["chromeVersion"] = chrome_full

# Augment with per-session synthetic values (same as device_check.py does)
profile.update({
    "canvasSeed":        random.randint(1, 65535),
    "audioNoise":        round(random.uniform(0.00001, 0.00009), 8),
    "webglNoise":        round(random.uniform(0.000001, 0.000009), 8),
    "timezone":          "America/New_York",
    "language":          "en-US",
    "countryCode":       "US",
    "batteryLevel":      round(random.uniform(0.25, 0.92), 2),
    "batteryCharging":   False,
    "dischargingTime":   random.randint(2400, 28800),
    "connectionRtt":     random.randint(8, 35),
    "connectionDownlink":round(random.uniform(25.0, 120.0), 1),
    "historyLength":     random.randint(3, 14),
    "doNotTrack":        None,
    "lat":               39.8283,
    "lon":               -98.5795,
})

UA = (
    f"Mozilla/5.0 (Linux; Android {profile['androidVersion']}; {profile['model']}) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{chrome_full} Mobile Safari/537.36"
)

print(f"\n{'='*70}")
print("VANGUARD MX — LOCAL FINGERPRINT DIAGNOSTIC")
print(f"Device #{device_index}: {profile['model']} / Android {profile['androidVersion']} / Chrome {chrome_full}")
print(f"Binary: {chromium_path}")
print(f"{'='*70}\n")

# ── Proxy ───────────────────────────────────────────────────────────────────────
PROXY_URL = (
    os.environ.get("PROXY_OVERRIDE", "").strip() or
    os.environ.get("PROXY_URL",      "").strip() or
    os.environ.get("Proxy",          "").strip()
)
proxy_info = parse_proxy(PROXY_URL) if PROXY_URL else None
if proxy_info and proxy_info.get("host"):
    print(f"Proxy: {proxy_info['host']}:{proxy_info['port']}")
else:
    print("⚠  No proxy — running direct (some signals may differ from proxied audit)")

# ── Local TCP forwarder for proxy auth (same as device_check.py) ───────────────
# Chrome for Testing 151 crashes when loading MV2 proxy extensions under Xvfb.
# Use a local TCP forwarder that injects Proxy-Authorization on every request.
local_proxy_port = None
proxy_server_inst = None

if proxy_info and proxy_info.get("host"):
    upstream_host = proxy_info["host"]
    upstream_port = proxy_info["port"]
    upstream_user = proxy_info.get("username") or ""
    upstream_pass = proxy_info.get("password") or ""

    class _AuthHandler(socketserver.BaseRequestHandler):
        def handle(self):
            client = self.request
            client.settimeout(30)
            try:
                data = b""
                while b"\r\n\r\n" not in data and len(data) < 128*1024:
                    chunk = client.recv(8192)
                    if not chunk: break
                    data += chunk
                if not data: return
                head, _, rest = data.partition(b"\r\n\r\n")
                lines = head.split(b"\r\n")
                first = lines[0].decode("latin1")
                parts = first.split(" ", 2)
                if len(parts) != 3: return
                method, target, version = parts
                headers = []
                for line in lines[1:]:
                    if b":" in line:
                        k, v = line.split(b":", 1)
                        headers.append((k.decode("latin1"), v.decode("latin1").lstrip()))

                auth_val = "Basic " + base64.b64encode(f"{upstream_user}:{upstream_pass}".encode()).decode()

                if method.upper() == "CONNECT":
                    remote = socket.create_connection((upstream_host, upstream_port), timeout=20)
                    remote.sendall(
                        f"CONNECT {target} {version}\r\nHost: {target}\r\n"
                        f"Proxy-Authorization: {auth_val}\r\nProxy-Connection: Keep-Alive\r\n\r\n".encode()
                    )
                    resp = b""
                    while b"\r\n\r\n" not in resp and len(resp) < 4096:
                        chunk = remote.recv(4096)
                        if not chunk: break
                        resp += chunk
                    client.sendall(resp)
                    if not (resp.startswith(b"HTTP/1.1 200") or resp.startswith(b"HTTP/1.0 200")):
                        remote.close(); return
                else:
                    remote = socket.create_connection((upstream_host, upstream_port), timeout=20)
                    out = [first.encode("latin1")]
                    has_auth = False
                    for hk, hv in headers:
                        if hk.lower() == "proxy-authorization":
                            has_auth = True
                            out.append(f"Proxy-Authorization: {auth_val}".encode("latin1"))
                        else:
                            out.append(f"{hk}: {hv}".encode("latin1"))
                    if not has_auth:
                        out.append(f"Proxy-Authorization: {auth_val}".encode("latin1"))
                    remote.sendall(b"\r\n".join(out) + b"\r\n\r\n" + rest)
                # relay
                socks = [client, remote]
                while True:
                    r, _, ex = select.select(socks, [], socks, 30)
                    if ex: break
                    if not r: continue
                    for s in r:
                        d = s.recv(65536)
                        if not d: return
                        (remote if s is client else client).sendall(d)
            except Exception:
                pass

    class _ThreadedServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    proxy_server_inst = _ThreadedServer(("127.0.0.1", 0), _AuthHandler)
    threading.Thread(target=proxy_server_inst.serve_forever, daemon=True).start()
    local_proxy_port = proxy_server_inst.server_address[1]
    print(f"Local proxy forwarder ready on 127.0.0.1:{local_proxy_port}\n")

# ── Pre-launch: clean stale Xvfb / Chrome artifacts ──────────────────────────
# Removes dead-PID lock files, orphaned Xvfb processes, and Chrome singleton
# files left by crashed prior sessions.  Must run before Xvfb starts so a
# stale :92 lock can't block the fresh launch.
_cleanup_stale_chrome_artifacts()
# Also kill any stale Xvfb specifically on :92 (fixed display used here)
for _p in ("/tmp/.X92-lock", "/tmp/.X11-unix/X92"):
    if os.path.exists(_p) or os.path.islink(_p):
        try:
            if _p.endswith("-lock"):
                _stale_pid = int(open(_p).read().strip())
                try:
                    os.kill(_stale_pid, 9)  # SIGKILL
                except Exception:
                    pass
            os.remove(_p)
            print(f"[cleanup] Removed stale artifact: {_p}")
        except Exception:
            pass

# ── Xvfb ────────────────────────────────────────────────────────────────────────
DISPLAY_NUM = ":92"
print(f"Starting Xvfb on {DISPLAY_NUM}...")
xvfb = subprocess.Popen(
    ["Xvfb", DISPLAY_NUM, "-screen", "0", "1300x900x24", "-ac"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
os.environ["DISPLAY"] = DISPLAY_NUM
time.sleep(2)
print(f"Xvfb ready on {DISPLAY_NUM}\n")

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

# ── Chrome options — mirror device_check.py exactly ─────────────────────────────
o = uc.ChromeOptions()
o.add_argument("--no-sandbox")
o.add_argument("--disable-setuid-sandbox")
o.add_argument("--disable-dev-shm-usage")
o.add_argument(f"--window-size={profile['screenW']},{profile['screenH']}")
o.add_argument(f"--user-agent={UA}")
o.add_argument("--touch-events=enabled")
o.add_argument(f"--force-device-scale-factor={profile['dpr']}")
o.add_argument(f"--lang={profile['language']},en;q=0.9")
o.add_argument("--use-gl=swiftshader")
o.add_argument("--enable-webgl")
o.add_argument("--ignore-gpu-blocklist")
o.add_argument("--disable-gpu-sandbox")
# Disable GPU-backed PAGE COMPOSITING to prevent renderer crashes under Xvfb,
# while keeping the GPU process alive so WebGL remains available.
# --disable-gpu would kill the GPU process entirely, making WebGL unavailable.
o.add_argument("--disable-gpu-compositing")
o.add_argument("--disable-blink-features=AutomationControlled")
o.add_argument("--disable-extensions-http-throttling")
o.add_argument("--no-default-browser-check")
o.add_argument("--no-first-run")
o.add_argument("--disable-sync")
o.add_argument("--password-store=basic")
if local_proxy_port:
    o.add_argument(f"--proxy-server=http://127.0.0.1:{local_proxy_port}")

cd_port = _find_free_port()
print(f"Launching Chrome {chrome_full} (version_main={chrome_main}) on port {cd_port}...")
try:
    driver = uc.Chrome(
        options=o,
        browser_executable_path=chromium_path,
        version_main=chrome_main,
        use_subprocess=True,
        log_level=1,
        port=cd_port,
    )
except Exception as e:
    print(f"ERROR: Chrome launch failed: {e}")
    xvfb.terminate()
    sys.exit(1)

time.sleep(5)
print("Chrome launched ✓\n")

# ── Stealth JS ─────────────────────────────────────────────────────────────────
try:
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                           {"source": make_stealth_js(profile)})
    # Second-pass inject: plugins/mimeTypes override (must run after main stealth JS
    # because Chrome's C++ only makes Navigator.prototype.plugins configurable after
    # the first injected script finishes). See make_plugins_override_js().
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                           {"source": make_plugins_override_js()})
    print("Stealth JS injected ✓")
except Exception as e:
    print(f"ERROR: Stealth JS injection failed: {e}")
    driver.quit(); xvfb.terminate(); sys.exit(1)

# ── CDP overrides (mirror check_gmail + device_check.py) ───────────────────────
cv_major = chrome_full.split(".")[0]

def _cdp(cmd, params, label):
    try:
        driver.execute_cdp_cmd(cmd, params)
        print(f"  ✓ {label}")
    except Exception as e:
        # Trim the giant stacktrace — only show first line
        print(f"  ✗ {label}: {str(e).splitlines()[0]}")

print("Applying CDP overrides:")
# Network.enable + Network.setUserAgentOverride intentionally omitted.
# Activating the CDP Network domain leaves a detectable trace that fingerprint.com
# uses for "developer tools" detection (weight 8). Emulation.setUserAgentOverride
# handles both navigator.userAgent and HTTP headers including Sec-CH-UA.
_cdp("Emulation.setTimezoneOverride", {"timezoneId": profile["timezone"]}, "Emulation.setTimezoneOverride")
_cdp("Emulation.setLocaleOverride",   {"locale": profile["language"]},     "Emulation.setLocaleOverride")
_cdp("Emulation.setHardwareConcurrencyOverride",
     {"hardwareConcurrency": profile["hwConcurrency"]}, "Emulation.setHardwareConcurrencyOverride")
_cdp("Emulation.setDeviceMetricsOverride", {
    "width": profile["screenW"], "height": profile["screenH"],
    "deviceScaleFactor": profile["dpr"], "mobile": True, "hasTouch": True,
    "screenWidth": profile["screenW"], "screenHeight": profile["screenH"],
}, "Emulation.setDeviceMetricsOverride")
_cdp("Emulation.setTouchEmulationEnabled",
     {"enabled": True, "maxTouchPoints": profile["maxTouchPoints"]},
     "Emulation.setTouchEmulationEnabled")
# NOTE: Emulation.setDeviceMemoryOverride is intentionally NOT called.
# It was confirmed removed/unavailable in Chrome 151 (unknown command error).
# HANDOFF note: "Do NOT reintroduce Emulation.setDeviceMemoryOverride (confirmed causes VM regression)"
_cdp("Emulation.setUserAgentOverride", {
    "userAgent": UA,
    "acceptLanguage": f"{profile['language']},en;q=0.9",
    "platform": profile["platform"],
    # CRITICAL: must include userAgentMetadata — omitting it causes Chrome 151 to
    # clear navigator.userAgentData.brands to [], mobile→false, platform→''.
    "userAgentMetadata": {
        "brands": [
            {"brand": "Not(A;Brand",   "version": "8"},
            {"brand": "Chromium",      "version": cv_major},
            {"brand": "Google Chrome", "version": cv_major},
        ],
        "fullVersion": chrome_full,
        "platform": "Android",
        "platformVersion": profile["androidVersion"],
        "architecture": "",
        "model": profile["model"],
        "mobile": True,
        "bitness": "",
        "wow64": False,
    },
}, "Emulation.setUserAgentOverride")
print()

# ═══════════════════════════════════════════════════════════════════════════════
# THE DIAGNOSTIC JS — run via execute_script()
# Returns a list of {name, status, detail} objects
# ═══════════════════════════════════════════════════════════════════════════════
DIAG_JS_TEMPLATE = r"""
return (function() {
  var R = [];
  var _err = null;
  try {
  var EXP = {
    hwConcurrency:  __EXP_HWC__,
    deviceMemory:   __EXP_MEM__,
    maxTouchPoints: __EXP_MTP__,
    platform:       '__EXP_PLT__',
    screenW:        __EXP_SW__,
    screenH:        __EXP_SH__,
    dpr:            __EXP_DPR__,
    cvMajor:        '__EXP_CV__',
    wglVendor:      '__EXP_WV__',
    wglRenderer:    '__EXP_WR__'
  };

  function ok(n, d)   { R.push({name:n, status:'PASS', detail:String(d||'')}); }
  function bad(n, d)  { R.push({name:n, status:'FAIL', detail:String(d||'')}); }
  function warn(n, d) { R.push({name:n, status:'WARN', detail:String(d||'')}); }

  function nativeStr(fn) {
    try { return Function.prototype.toString.call(fn); } catch(e) { return String(e); }
  }
  function isNative(fn) {
    if (typeof fn !== 'function') return false;
    return nativeStr(fn).indexOf('[native code]') !== -1;
  }
  function getDesc(obj, k) {
    try { return Object.getOwnPropertyDescriptor(obj, k); } catch(e) { return null; }
  }

  // ────────────────────────────────────────────────────────────────────────
  // SECTION 1: Navigator property values
  // ────────────────────────────────────────────────────────────────────────

  var wd = navigator.webdriver;
  if (wd === undefined || wd === false) ok('1.nav.webdriver', 'value='+wd);
  else bad('1.nav.webdriver', '⚠ EXPOSED value='+wd);

  var pl = navigator.plugins;
  if (!pl) bad('1.nav.plugins', 'null/undefined');
  else if (pl.length === 0) ok('1.nav.plugins', 'length=0 ✓ (Android Chrome: 0 plugins)');
  else bad('1.nav.plugins', 'length='+pl.length+' (should be 0 on Android)');

  var mm = navigator.mimeTypes;
  if (!mm) bad('1.nav.mimeTypes', 'null/undefined');
  else if (mm.length === 0) ok('1.nav.mimeTypes', 'length=0 ✓');
  else bad('1.nav.mimeTypes', 'length='+mm.length+' (should be 0)');

  var langs = navigator.languages;
  if (!langs || langs.length === 0) bad('1.nav.languages', 'empty/missing');
  else if (langs.length >= 2) ok('1.nav.languages', JSON.stringify(langs));
  else warn('1.nav.languages', 'single language: '+JSON.stringify(langs));

  var plt = navigator.platform;
  if (plt === EXP.platform) ok('1.nav.platform', plt);
  else bad('1.nav.platform', 'got='+plt+' expected='+EXP.platform);

  var ua = navigator.userAgent;
  if (ua && ua.indexOf('Android') !== -1 && ua.indexOf('Mobile') !== -1)
    ok('1.nav.userAgent', ua.substring(0,90));
  else bad('1.nav.userAgent', 'Missing Android/Mobile: '+ua.substring(0,90));

  var hc = navigator.hardwareConcurrency;
  if (hc === EXP.hwConcurrency) ok('1.nav.hardwareConcurrency', hc);
  else bad('1.nav.hardwareConcurrency', 'got='+hc+' expected='+EXP.hwConcurrency);

  var dm = navigator.deviceMemory;
  if (dm === EXP.deviceMemory) ok('1.nav.deviceMemory', dm+' GiB ✓');
  else bad('1.nav.deviceMemory', 'got='+dm+' expected='+EXP.deviceMemory);

  var mtp = navigator.maxTouchPoints;
  if (mtp === EXP.maxTouchPoints) ok('1.nav.maxTouchPoints', mtp+' ✓');
  else bad('1.nav.maxTouchPoints', 'got='+mtp+' expected='+EXP.maxTouchPoints);

  var uad = navigator.userAgentData;
  if (!uad) {
    bad('1.nav.userAgentData', '⚠ MISSING — injection failed (non-extensible navigator instance?)');
  } else {
    var brands = uad.brands || [];
    var chromeBrand = brands.filter(function(b){ return b.brand === 'Google Chrome'; })[0];
    if (!chromeBrand) bad('1.nav.userAgentData.brands', 'missing Google Chrome brand — '+JSON.stringify(brands));
    else if (chromeBrand.version !== EXP.cvMajor)
      bad('1.nav.userAgentData.brands', 'version mismatch: got='+chromeBrand.version+' expected='+EXP.cvMajor);
    else ok('1.nav.userAgentData.brands', JSON.stringify(brands));
    if (uad.mobile !== true) bad('1.nav.userAgentData.mobile', 'got='+uad.mobile);
    else ok('1.nav.userAgentData.mobile', 'true ✓');
    if (uad.platform !== 'Android') bad('1.nav.userAgentData.platform', 'got='+uad.platform);
    else ok('1.nav.userAgentData.platform', 'Android ✓');
  }

  var conn = navigator.connection;
  if (!conn) bad('1.nav.connection', 'missing');
  else {
    ok('1.nav.connection', 'effectiveType='+conn.effectiveType+' type='+conn.type+' rtt='+conn.rtt);
    if (typeof conn.addEventListener !== 'function')
      bad('1.nav.connection.addEventListener', 'missing — throws on real sites');
    else ok('1.nav.connection.addEventListener', 'present ✓');
  }

  var pve = navigator.pdfViewerEnabled;
  if (pve === true) ok('1.nav.pdfViewerEnabled', 'true ✓');
  else bad('1.nav.pdfViewerEnabled', 'got='+pve+' expected=true');

  // ────────────────────────────────────────────────────────────────────────
  // SECTION 2: Function.toString() tampering
  // ────────────────────────────────────────────────────────────────────────

  var _np = Object.getPrototypeOf(navigator);
  var nativeCheckList = [
    ['2.toString:webdriver getter',          getDesc(_np, 'webdriver')],
    ['2.toString:hardwareConcurrency getter',getDesc(_np, 'hardwareConcurrency')],
    ['2.toString:deviceMemory getter',       getDesc(_np, 'deviceMemory')],
    ['2.toString:maxTouchPoints getter',     getDesc(_np, 'maxTouchPoints')],
    ['2.toString:plugins getter',            getDesc(_np, 'plugins') || getDesc(Navigator.prototype, 'plugins')],
    ['2.toString:mimeTypes getter (proto)',  getDesc(Navigator.prototype, 'mimeTypes')],
    ['2.toString:connection getter',         getDesc(Navigator.prototype, 'connection')],
    ['2.toString:userAgentData getter',      getDesc(Navigator.prototype, 'userAgentData')],
  ];
  nativeCheckList.forEach(function(item) {
    var label = item[0], desc = item[1];
    if (!desc) { warn(label, 'no descriptor on prototype'); return; }
    var fn = desc.get || desc.value;
    if (!fn) { warn(label, 'descriptor has no get/value'); return; }
    if (isNative(fn)) ok(label, nativeStr(fn).substring(0,55)+'…');
    else bad(label, '⚠ EXPOSES JS: '+nativeStr(fn).substring(0,90));
  });

  var wglCtx = document.createElement('canvas').getContext('webgl');
  if (wglCtx) {
    ['getParameter','getSupportedExtensions','getExtension','getShaderPrecisionFormat'].forEach(function(m){
      var fn = WebGLRenderingContext.prototype[m];
      if (isNative(fn)) ok('2.toString:WebGL.'+m, 'native-looking ✓');
      else bad('2.toString:WebGL.'+m, '⚠ EXPOSES JS: '+nativeStr(fn).substring(0,90));
    });
  }

  // ────────────────────────────────────────────────────────────────────────
  // SECTION 3: Property descriptor location (instance vs prototype)
  // fingerprint.com: Object.getOwnPropertyDescriptor(navigator, key) returning
  // anything means the override is on the instance — a tampering signal.
  // ────────────────────────────────────────────────────────────────────────

  ['webdriver','hardwareConcurrency','deviceMemory','maxTouchPoints',
   'platform','vendor','plugins','mimeTypes','languages','language',
   'userAgentData','connection','pdfViewerEnabled','cookieEnabled'].forEach(function(k){
    var d = getDesc(navigator, k);
    if (d) bad('3.desc.instance:nav.'+k,
      '⚠ OWN PROPERTY on navigator instance — tamper signal: '+
      JSON.stringify({configurable:d.configurable,enumerable:d.enumerable,hasGet:!!d.get}));
    else ok('3.desc.instance:nav.'+k, 'not on instance ✓ (prototype-level)');
  });

  ['width','height','availWidth','availHeight','colorDepth','pixelDepth'].forEach(function(k){
    var d = getDesc(screen, k);
    if (d) bad('3.desc.instance:screen.'+k,
      '⚠ OWN PROPERTY on screen instance: '+
      JSON.stringify({configurable:d.configurable,enumerable:d.enumerable}));
    else ok('3.desc.instance:screen.'+k, 'not on instance ✓');
  });

  // Window proto override check (devicePixelRatio, innerWidth, etc.)
  ['devicePixelRatio','innerWidth','innerHeight','outerWidth','outerHeight'].forEach(function(k){
    var d = getDesc(window, k);
    if (d) bad('3.desc.instance:window.'+k,
      '⚠ OWN PROPERTY on window instance: '+
      JSON.stringify({configurable:d.configurable,enumerable:d.enumerable,hasGet:!!d.get}));
    else ok('3.desc.instance:window.'+k, 'not on window instance ✓');
  });

  // ────────────────────────────────────────────────────────────────────────
  // SECTION 4: CDP / Automation leftover detection (HIGH PRIORITY)
  // ────────────────────────────────────────────────────────────────────────

  var wdAttr = document.documentElement.getAttribute('webdriver');
  if (wdAttr === null) ok('4.html.webdriver-attr', 'not present ✓');
  else bad('4.html.webdriver-attr', '⚠ ATTRIBUTE PRESENT value='+wdAttr);

  var badVars = [
    '__webdriver_evaluate','__selenium_evaluate','__webdriver_script_fn',
    '__webdriver_script_func','__webdriver_script_function','__fxdriver_evaluate',
    '__driver_unwrapped','__webdriver_unwrapped','__driver_evaluate',
    '__selenium_unwrapped','__fxdriver_unwrapped','_Selenium_IDE_Recorder',
    '_selenium','calledSelenium','$cdc_asdjflasutopfhvcZLmcfl_',
    '__$webdriverAsyncExecutor','domAutomation','domAutomationController',
    '__lastWatirAlert','__lastWatirConfirm','__lastWatirPrompt',
    '_WEBDRIVER_ELEM_CACHE','ChromeDriverw',
    'cdc_adoQpoasnfa76pfcZLmcfl_Array','cdc_adoQpoasnfa76pfcZLmcfl_Promise',
    'cdc_adoQpoasnfa76pfcZLmcfl_Symbol'
  ];
  var foundBad = badVars.filter(function(v){ return window[v] !== undefined; });
  try {
    Object.keys(window).forEach(function(k){
      if ((k.startsWith('cdc_')||k.startsWith('$cdc_')) && foundBad.indexOf(k)===-1)
        foundBad.push(k);
    });
  } catch(e) {}
  if (foundBad.length === 0) ok('4.automation.window-vars', 'no known vars found ✓');
  else bad('4.automation.window-vars', '⚠ FOUND: '+foundBad.join(', '));

  if (!window.chrome) {
    bad('4.chrome.object', 'window.chrome missing (real Chrome always has it)');
  } else {
    ok('4.chrome.object', 'present ✓');
    var rtId = window.chrome.runtime && window.chrome.runtime.id;
    if (rtId !== undefined) bad('4.chrome.runtime.id', '⚠ EXPOSED value='+JSON.stringify(rtId));
    else ok('4.chrome.runtime.id', 'undefined ✓');
    if (window.chrome.app) bad('4.chrome.app', '⚠ PRESENT (should be absent on Android)');
    else ok('4.chrome.app', 'absent ✓');
    if (window.chrome.webstore) bad('4.chrome.webstore', '⚠ PRESENT (should be absent)');
    else ok('4.chrome.webstore', 'absent ✓');
    if (window.chrome.cast) bad('4.chrome.cast', '⚠ PRESENT (should be absent)');
    else ok('4.chrome.cast', 'absent ✓');
    if (typeof window.chrome.loadTimes === 'function') ok('4.chrome.loadTimes', 'function ✓');
    else warn('4.chrome.loadTimes', 'missing or not a function');
    if (typeof window.chrome.csi === 'function') ok('4.chrome.csi', 'function ✓');
    else warn('4.chrome.csi', 'missing');
    if (window.chrome.runtime) {
      if (typeof window.chrome.runtime.connect === 'function') ok('4.chrome.runtime.connect', '✓');
      else warn('4.chrome.runtime.connect', 'missing');
    }
  }

  if (window.__nr !== undefined)
    bad('4.automation.__nr', '⚠ __nr left on window — enumerable via getOwnPropertyNames(window)');
  else ok('4.automation.__nr', '__nr cleaned from window ✓');

  // ────────────────────────────────────────────────────────────────────────
  // SECTION 5: Canvas / WebGL fingerprint consistency
  // ────────────────────────────────────────────────────────────────────────

  try {
    var c2d = document.createElement('canvas');
    c2d.width = 200; c2d.height = 50;
    var ctx2d = c2d.getContext('2d');
    ctx2d.fillStyle = '#f00'; ctx2d.fillRect(0,0,200,50);
    ctx2d.fillStyle = '#00f'; ctx2d.font = '16px Arial';
    ctx2d.fillText('Vanguard MX test', 5, 30);
    var dataURL = c2d.toDataURL();
    if (dataURL && dataURL.length > 100) ok('5.canvas.toDataURL', 'non-blank len='+dataURL.length);
    else bad('5.canvas.toDataURL', 'blank or missing');
  } catch(e) { bad('5.canvas.toDataURL', 'exception: '+e); }

  var wc = document.createElement('canvas');
  var gl = wc.getContext('webgl') || wc.getContext('experimental-webgl');
  if (!gl) {
    bad('5.webgl.context', 'WebGL context unavailable');
  } else {
    ok('5.webgl.context', 'available ✓');
    var ext = gl.getExtension('WEBGL_debug_renderer_info');
    if (!ext) {
      warn('5.webgl.renderer', 'WEBGL_debug_renderer_info not available');
    } else {
      var vendor   = gl.getParameter(ext.UNMASKED_VENDOR_WEBGL);
      var renderer = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
      var glVer    = gl.getParameter(gl.VERSION);
      var isSoft = (
        ((vendor||'').toLowerCase().indexOf('google') !== -1 &&
         (renderer||'').toLowerCase().indexOf('swiftshader') !== -1) ||
        (renderer||'').toLowerCase().indexOf('llvmpipe') !== -1 ||
        (renderer||'').toLowerCase().indexOf('softpipe') !== -1
      );
      if (isSoft) bad('5.webgl.renderer', '⚠ SOFTWARE RENDERER (VM signal!) vendor='+vendor+' renderer='+renderer);
      else ok('5.webgl.renderer', 'vendor='+vendor+' renderer='+renderer);

      if (vendor !== EXP.wglVendor)
        bad('5.webgl.unmasked_vendor', '⚠ got='+vendor+' expected='+EXP.wglVendor);
      else ok('5.webgl.unmasked_vendor', vendor+' ✓');
      if (renderer !== EXP.wglRenderer)
        bad('5.webgl.unmasked_renderer', '⚠ got='+renderer+' expected='+EXP.wglRenderer);
      else ok('5.webgl.unmasked_renderer', renderer+' ✓');

      ok('5.webgl.gl_version', glVer);
      var maxTex = gl.getParameter(gl.MAX_TEXTURE_SIZE);
      if (maxTex >= 16384) ok('5.webgl.MAX_TEXTURE_SIZE', maxTex+' ✓');
      else bad('5.webgl.MAX_TEXTURE_SIZE', '⚠ got='+maxTex+' (SwiftShader=8192, Mali=16384)');
      var maxVU = gl.getParameter(gl.MAX_VERTEX_UNIFORM_VECTORS);
      if (maxVU === 256) ok('5.webgl.MAX_VERTEX_UNIFORM_VECTORS', maxVU+' (Mali) ✓');
      else warn('5.webgl.MAX_VERTEX_UNIFORM_VECTORS', 'got='+maxVU+' expected=256 (Mali)');
      var maxFU = gl.getParameter(gl.MAX_FRAGMENT_UNIFORM_VECTORS);
      if (maxFU === 224) ok('5.webgl.MAX_FRAGMENT_UNIFORM_VECTORS', maxFU+' (Mali) ✓');
      else warn('5.webgl.MAX_FRAGMENT_UNIFORM_VECTORS', 'got='+maxFU+' expected=224 (Mali)');

      // Check extension list doesn't contain desktop-only S3TC
      var exts = gl.getSupportedExtensions() || [];
      var hasS3TC = exts.some(function(e){ return e.indexOf('WEBGL_compressed_texture_s3tc') !== -1; });
      if (hasS3TC) bad('5.webgl.extensions', '⚠ has S3TC (desktop-only, not on Android) — '+exts.filter(function(e){return e.indexOf('s3tc')!==-1;}).join(', '));
      else ok('5.webgl.extensions', 'no S3TC (correct for Android) — '+exts.length+' exts');
    }
    var gl2 = wc.getContext('webgl2');
    if (gl2) ok('5.webgl2.context', 'available ✓');
    else warn('5.webgl2.context', 'unavailable (minor)');
  }

  // ────────────────────────────────────────────────────────────────────────
  // SECTION 6: Screen / viewport consistency
  // ────────────────────────────────────────────────────────────────────────

  if (screen.width === EXP.screenW) ok('6.screen.width', screen.width+' ✓');
  else bad('6.screen.width', 'got='+screen.width+' expected='+EXP.screenW);
  if (screen.height === EXP.screenH) ok('6.screen.height', screen.height+' ✓');
  else bad('6.screen.height', 'got='+screen.height+' expected='+EXP.screenH);

  var dpr = window.devicePixelRatio;
  if (Math.abs(dpr - EXP.dpr) < 0.01) ok('6.window.devicePixelRatio', dpr+' ✓');
  else bad('6.window.devicePixelRatio', 'got='+dpr+' expected='+EXP.dpr);

  var iw = window.innerWidth;
  if (iw > 0 && iw <= screen.width + 20) ok('6.window.innerWidth', iw+' (screenW='+screen.width+')');
  else bad('6.window.innerWidth', 'iw='+iw+' vs screenW='+screen.width);

  var ah = screen.availHeight;
  if (ah > 0 && ah <= screen.height) ok('6.screen.availHeight', ah);
  else bad('6.screen.availHeight', 'got='+ah+' vs screenH='+screen.height);

  var so = screen.orientation;
  if (!so) bad('6.screen.orientation', 'missing');
  else if (so.type === 'portrait-primary' || so.type === 'landscape-primary') {
    ok('6.screen.orientation.type', so.type+' angle='+so.angle);
    if (typeof so.lock === 'function') ok('6.screen.orientation.lock', 'function ✓');
    else bad('6.screen.orientation.lock', 'missing');
    if (so === screen.orientation) ok('6.screen.orientation.stable', 'same object on re-access ✓');
    else bad('6.screen.orientation.stable', '⚠ returns new object each access (===false detection)');
  } else bad('6.screen.orientation', 'type='+so.type);

  // ────────────────────────────────────────────────────────────────────────
  // SECTION 7: Permissions API
  // ────────────────────────────────────────────────────────────────────────

  if (!navigator.permissions || !navigator.permissions.query)
    bad('7.permissions.query', 'navigator.permissions.query missing');
  else ok('7.permissions.query', 'present ✓');

  // ────────────────────────────────────────────────────────────────────────
  // SECTION 8: Timing / performance sanity
  // ────────────────────────────────────────────────────────────────────────

  var t1 = performance.now();
  for (var i=0;i<100000;i++){} // ~1ms loop
  var t2 = performance.now();
  var elapsed = t2 - t1;
  if (elapsed >= 0 && elapsed < 500) ok('8.performance.now', 'monotonic, elapsed='+elapsed.toFixed(3)+'ms');
  else bad('8.performance.now', 'suspicious: elapsed='+elapsed);

  var dn1 = Date.now(), dn2 = Date.now();
  if (dn2 >= dn1) ok('8.Date.now', 'monotonic ✓');
  else bad('8.Date.now', 'regression: dn1='+dn1+' dn2='+dn2);

  ok('8.timezone-offset', new Date().getTimezoneOffset()+'min');

  // ────────────────────────────────────────────────────────────────────────
  // SECTION 9: Extra tampering signals (Tampering score drivers)
  // ────────────────────────────────────────────────────────────────────────

  // IE-only properties must not be present at all
  ['userLanguage','browserLanguage','systemLanguage'].forEach(function(k){
    var val = navigator[k];
    var protoDesc = getDesc(Object.getPrototypeOf(navigator), k);
    if (val !== undefined)
      bad('9.IE-prop.'+k, '⚠ PRESENT value='+val+' (IE-only, explicit tamper signal)');
    else if (protoDesc)
      warn('9.IE-prop.'+k, 'undefined but descriptor exists on proto: '+JSON.stringify({
        configurable:protoDesc.configurable,enumerable:protoDesc.enumerable}));
    else ok('9.IE-prop.'+k, 'not present ✓');
  });

  // keyboard API: not on Android Chrome
  if (navigator.keyboard !== undefined)
    bad('9.nav.keyboard', '⚠ PRESENT (Android Chrome has no Keyboard API)');
  else ok('9.nav.keyboard', 'undefined ✓');

  // plugins instanceof PluginArray
  var isPA = navigator.plugins instanceof PluginArray;
  if (isPA) ok('9.plugins.instanceof', 'PluginArray ✓');
  else warn('9.plugins.instanceof', 'NOT PluginArray — plain object (fingerprint.com may detect)');

  // mimeTypes instanceof MimeTypeArray
  var isMA = navigator.mimeTypes instanceof MimeTypeArray;
  if (isMA) ok('9.mimeTypes.instanceof', 'MimeTypeArray ✓');
  else warn('9.mimeTypes.instanceof', 'NOT MimeTypeArray — plain object');

  // window.navigator replacement check — if we did window.navigator override,
  // bare 'navigator' and window.navigator must return same object
  var sameNav = (navigator === window.navigator);
  if (sameNav) ok('9.nav.identity', 'navigator === window.navigator ✓');
  else warn('9.nav.identity', 'navigator !== window.navigator (window.navigator replaced — some sites detect this)');

  // globalPrivacyControl should not exist on real Android Chrome
  if (navigator.globalPrivacyControl !== undefined)
    warn('9.nav.globalPrivacyControl', 'present='+navigator.globalPrivacyControl);
  else ok('9.nav.globalPrivacyControl', 'undefined ✓');

  } catch(_e) {
    _err = String(_e);
    R.push({name:'JS_RUNTIME_ERROR', status:'FAIL', detail: _err});
  }
  if (R.length === 0) R.push({name:'JS_NO_OUTPUT', status:'FAIL', detail:'JS ran but produced no checks'});
  return R;
})();
"""

def run_diag(driver, url, profile, chrome_full):
    print(f"\n{'─'*70}")
    print(f"PAGE: {url}")
    print(f"{'─'*70}")
    try:
        driver.get(url)
        time.sleep(5)
    except Exception as e:
        print(f"  ERROR navigating: {e}")
        return None

    js = (DIAG_JS_TEMPLATE
          .replace("__EXP_HWC__",  str(profile["hwConcurrency"]))
          .replace("__EXP_MEM__",  str(profile["deviceMemory"]))
          .replace("__EXP_MTP__",  str(profile["maxTouchPoints"]))
          .replace("__EXP_PLT__",  profile["platform"])
          .replace("__EXP_SW__",   str(profile["screenW"]))
          .replace("__EXP_SH__",   str(profile["screenH"]))
          .replace("__EXP_DPR__",  str(profile["dpr"]))
          .replace("__EXP_CV__",   chrome_full.split(".")[0])
          .replace("__EXP_WV__",   profile["webglVendor"])
          .replace("__EXP_WR__",   profile["webglRenderer"])
    )
    try:
        results = driver.execute_script(js)
    except Exception as e:
        print(f"  ERROR running JS: {str(e).splitlines()[0]}")
        return None

    if not results:
        # JS returned None/null — likely a runtime error; run a simpler probe
        try:
            probe = driver.execute_script("return {ok:true, ua: navigator.userAgent};")
            print(f"  JS runtime probe: {probe}")
        except Exception as pe:
            print(f"  Probe also failed: {pe}")
        return None

    passed = [r for r in results if r["status"] == "PASS"]
    failed = [r for r in results if r["status"] == "FAIL"]
    warned = [r for r in results if r["status"] == "WARN"]

    print(f"\n  PASS:{len(passed):3d}  FAIL:{len(failed):3d}  WARN:{len(warned):3d}  TOTAL:{len(results):3d}\n")

    if failed:
        print(f"  ❌ FAILURES ({len(failed)}):")
        for r in failed:
            print(f"     FAIL  {r['name']}")
            if r["detail"]:
                print(f"           {r['detail']}")
    if warned:
        print(f"\n  ⚠  WARNINGS ({len(warned)}):")
        for r in warned:
            print(f"     WARN  {r['name']}")
            if r["detail"]:
                print(f"           {r['detail']}")
    if not failed and not warned:
        print("  ✅ ALL CHECKS PASSED")
    elif not failed:
        print(f"\n  ✅ No failures (only {len(warned)} warnings)")

    return {"passed": len(passed), "failed": len(failed), "warned": len(warned),
            "failures": failed, "warnings": warned}

# ── Run on about:blank (no secure context restrictions) ─────────────────────────
r1 = run_diag(driver, "about:blank", profile, chrome_full)

# ── Run on HTTPS page (some properties only available in secure contexts) ───────
r2 = run_diag(driver, "https://www.google.com", profile, chrome_full)

# ── Async permissions test ──────────────────────────────────────────────────────
print(f"\n{'─'*70}")
print("ASYNC CHECKS (on google.com)")
print(f"{'─'*70}")
try:
    perm_result = driver.execute_async_script("""
var done = arguments[0];
Promise.all([
  navigator.permissions ? navigator.permissions.query({name:'notifications'}) : Promise.resolve({state:'API_MISSING'}),
  navigator.permissions ? navigator.permissions.query({name:'geolocation'})   : Promise.resolve({state:'API_MISSING'}),
  navigator.permissions ? navigator.permissions.query({name:'camera'})        : Promise.resolve({state:'API_MISSING'}),
]).then(function(rs) {
  done({notifications:rs[0].state, geolocation:rs[1].state, camera:rs[2].state, ok:true});
}).catch(function(e) { done({error:String(e), ok:false}); });
""")
    if perm_result and perm_result.get("ok"):
        print(f"  ✓ permissions.query works: {perm_result}")
    else:
        print(f"  ✗ permissions.query failed: {perm_result}")
except Exception as e:
    print(f"  ERROR: {e}")

# ── Summary ─────────────────────────────────────────────────────────────────────
print(f"\n{'='*70}")
print("DIAGNOSTIC SUMMARY")
print(f"{'='*70}")
if r1: print(f"  about:blank  — PASS:{r1['passed']:3d}  FAIL:{r1['failed']:3d}  WARN:{r1['warned']:3d}")
if r2: print(f"  google.com   — PASS:{r2['passed']:3d}  FAIL:{r2['failed']:3d}  WARN:{r2['warned']:3d}")

all_failures = list({f["name"]: f for f in
    (r1 or {}).get("failures",[]) + (r2 or {}).get("failures",[])}.values())
total_fail = len(all_failures)

if total_fail == 0:
    print("\n  ✅ No failures — browser looks clean!")
else:
    print(f"\n  ❌ {total_fail} unique failures:")
    for f in all_failures:
        print(f"     • {f['name']}")
        print(f"       {f['detail'][:120]}")

print()

# ── Cleanup ─────────────────────────────────────────────────────────────────────
try: driver.quit()
except Exception: pass
try: xvfb.terminate()
except Exception: pass
if proxy_server_inst:
    try: proxy_server_inst.shutdown()
    except Exception: pass
print("Done.")
