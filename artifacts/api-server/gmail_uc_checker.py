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

# ── Cross-process Chrome launch lock ─────────────────────────────────────────
# Multiple Python processes (one per account) can be spawned concurrently.
# Launching Chrome simultaneously from all of them causes OOM crashes.
# This file lock serializes Chrome launches so only ONE Chrome starts at a time.
# Once Chrome is stable (CDP ready), the lock is released so the next can start.
_CHROME_LAUNCH_LOCK_PATH = "/tmp/gmail_checker_chrome_launch.lock"


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[UC] {msg}", file=sys.stderr, flush=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def rand_sleep(min_ms: int, max_ms: int):
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def human_type(element, text: str):
    """Type text character by character with realistic random delays.
    Re-finds the element if a stale reference is hit."""
    from selenium.common.exceptions import StaleElementReferenceException
    for char in text:
        for _attempt in range(3):
            try:
                element.send_keys(char)
                break
            except StaleElementReferenceException:
                time.sleep(0.3)  # brief wait then retry
        delay = random.uniform(0.06, 0.16)
        if random.random() < 0.05:
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
    try:
        with open(fp_path, "w") as f:
            json.dump(fp, f, indent=2)
    except Exception:
        pass
    return fp


def make_stealth_js(fp: dict) -> str:
    """Build the CDP stealth script with values from this account's fingerprint."""
    cs  = fp["canvasSeed"]
    an  = fp["audioNoise"]
    wv  = fp["webglVendor"].replace("'", "\\'")
    wr  = fp["webglRenderer"].replace("'", "\\'")
    cv  = fp["chromeVersion"]
    av  = fp["androidVersion"]
    mdl = fp["model"].replace("'", "\\'")
    return f"""
Object.defineProperty(navigator,'webdriver',{{get:()=>undefined}});
Object.defineProperty(navigator,'plugins',{{get:()=>{{var p=[];p.length=0;return p;}}}});
Object.defineProperty(navigator,'languages',{{get:()=>['en-US','en']}});
Object.defineProperty(navigator,'hardwareConcurrency',{{get:()=>{fp['hwConcurrency']}}});
Object.defineProperty(navigator,'deviceMemory',{{get:()=>{fp['deviceMemory']}}});
Object.defineProperty(screen,'width',      {{get:()=>{fp['screenW']}}});
Object.defineProperty(screen,'height',     {{get:()=>{fp['screenH']}}});
Object.defineProperty(screen,'availWidth', {{get:()=>{fp['screenW']}}});
Object.defineProperty(screen,'availHeight',{{get:()=>{fp['availH']}}});
Object.defineProperty(screen,'colorDepth', {{get:()=>24}});
Object.defineProperty(screen,'pixelDepth', {{get:()=>24}});
Object.defineProperty(window,'devicePixelRatio',{{get:()=>{fp['dpr']}}});
Object.defineProperty(navigator,'maxTouchPoints',{{get:()=>{fp['maxTouchPoints']}}});
Object.defineProperty(navigator,'platform',{{get:()=>'{fp['platform']}'}});
Object.defineProperty(navigator,'vendor',  {{get:()=>'Google Inc.'}});
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
try{{navigator.getBattery&&navigator.getBattery().then(function(b){{Object.defineProperty(b,'charging',{{get:()=>false}});Object.defineProperty(b,'level',{{get:()=>0.72}});}});}}catch(e){{}}
window.ontouchstart=function(){{}};
try{{Object.defineProperty(screen,'orientation',{{get:()=>({{{{'type':'portrait-primary','angle':0}}}})}}); }}catch(e){{}}
try{{
  var conn={{'effectiveType':'4g','rtt':Math.floor(40+Math.random()*60),'downlink':parseFloat((8+Math.random()*6).toFixed(1)),'saveData':false,'type':'cellular','onchange':null}};
  Object.defineProperty(navigator,'connection',{{get:()=>conn}});
  Object.defineProperty(navigator,'mozConnection',{{get:()=>undefined}});
  Object.defineProperty(navigator,'webkitConnection',{{get:()=>undefined}});
}}catch(e){{}}
try{{Object.defineProperty(navigator,'keyboard',{{get:()=>undefined}});}}catch(e){{}}
(function(){{
  function patch(ctx){{
    var gp=ctx.prototype.getParameter;
    ctx.prototype.getParameter=function(p){{if(p===37445)return'{wv}';if(p===37446)return'{wr}';return gp.call(this,p);}};
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
  if(orig)AudioBuffer.prototype.getChannelData=function(){{var d=orig.apply(this,arguments);if(d&&d.length>0)d[0]=d[0]+noise;return d;}};
}})();
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

    # Auto-retry once if Google blocked automation detection ("Couldn't sign you in")
    # Fresh profile + new fingerprint almost always clears this on the second attempt.
    _blocked_reason = result.get("reason", "")
    if (
        result.get("status") == "verification_required"
        and (
            "automation detected" in _blocked_reason.lower()
            or "couldn't sign you in" in _blocked_reason.lower()
            or "blocked this browser" in _blocked_reason.lower()
        )
    ):
        log(f"{email} — automation block detected, auto-retrying with fresh profile…")
        result = check_gmail(email, password, totp_secret, proxy, True, proxy_for_ip_check)

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
    if headless:
        options.add_argument("--disable-gpu")

    log(f"Launching Chrome (UC)…")
    # Acquire cross-process lock so only ONE Chrome starts at a time.
    # Concurrent Chrome launches exhaust shared memory and cause crashes.
    _lock_fd = open(_CHROME_LAUNCH_LOCK_PATH, "w")
    log("Waiting for Chrome launch slot…")
    fcntl.flock(_lock_fd, fcntl.LOCK_EX)
    log("Chrome launch slot acquired — starting Chrome")
    try:
        driver = uc.Chrome(
            options=options,
            browser_executable_path=chromium_path,
            headless=headless,
            version_main=138,
            use_subprocess=True,
        )
        # Hold lock briefly while Chrome stabilises, then release for next account
        time.sleep(2.5)
    except Exception as e:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        _lock_fd.close()
        _cleanup(proxy_ext_path)
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
            "acceptLanguage": "en-US,en;q=0.9",
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
        _cleanup(proxy_ext_path)
    _login_result["exitIp"] = exit_ip
    _login_result["fingerprint"] = fp_summary
    return _login_result


def _cleanup(path: str | None):
    if path and os.path.exists(path):
        try:
            os.unlink(path)
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
            "find your google account"
        ]):
            return {"status": "wrong_password", "reason": "Google account not found", "totpCode": totp_code}

        if any(x in text for x in [
            "wrong password", "didn't recognize", "password you entered",
            "incorrect password", "that password is incorrect"
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
            time.sleep(0.3)
        return None

    # ── Step 0: Minimal warmup — visit Google homepage first ─────────────────
    # Without this, Google detects automation at the password step and silently
    # bounces back to challenge/pwd. A brief google.com visit warms up the
    # fingerprint and makes the session look more organic.
    try:
        log(f"{email} — Step 0: warmup visit to google.com")
        driver.get("https://www.google.com")
        rand_sleep(800, 1200)
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
        rand_sleep(1000, 1800)
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
            move_to_element(driver, email_field)
            rand_sleep(200, 400)
            email_field.click()
            break
        except Exception:
            rand_sleep(400, 700)
            email_field = wait_for_any(EMAIL_SELECTORS, timeout=6) or email_field

    rand_sleep(300, 600)
    human_type(email_field, email)
    rand_sleep(500, 900)
    # send_keys(ENTER) with stale retry
    for _attempt in range(3):
        try:
            email_field.send_keys(Keys.ENTER)
            break
        except Exception:
            rand_sleep(300, 500)
            email_field = wait_for_any(EMAIL_SELECTORS, timeout=5) or email_field
    rand_sleep(1500, 2000)

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
            rand_sleep(1500, 2500)
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

    move_to_element(driver, pw_field)
    rand_sleep(200, 400)
    pw_field.click()
    rand_sleep(300, 500)
    human_type(pw_field, password)
    rand_sleep(500, 900)
    pw_field.send_keys(Keys.ENTER)
    rand_sleep(1500, 2000)

    url, text = page_state()
    log(f"{email} — After password submit: {url[:70]}")

    # ── Quick wrong-password check (before anything else) ─────────────────────
    if any(x in text for x in [
        "wrong password", "didn't recognize", "that password is incorrect",
        "incorrect password", "password you entered"
    ]) or any(x in url for x in ["WrongPassword", "wrongpassword"]):
        return {"status": "wrong_password", "reason": "Wrong password", "totpCode": totp_code}

    # If Google returned us BACK to the password page → wrong password
    # (Google doesn't always show an error message — sometimes it just reloads the page)
    if "challenge/pwd" in url or "ServicePasswordChallenge" in url:
        shot = screenshot_b64()
        return {
            "status": "wrong_password",
            "reason": "Wrong password (Google returned to password page without error message)",
            "totpCode": totp_code,
            "debugScreenshot": shot,
        }

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
        rand_sleep(1800, 2800)

        TOTP_SELECTORS = [
            'input[name="totpPin"]', 'input[name="Pin"]', 'input[id="totpPin"]',
            'input[autocomplete="one-time-code"]', 'input[type="tel"]',
            'input[aria-label*="code"]', 'input[aria-label*="Code"]',
            'input[type="number"]',
        ]

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
                rand_sleep(1500, 2500)
                _click_authenticator()
                rand_sleep(1500, 2500)
                totp_field = wait_for_any(TOTP_SELECTORS, timeout=15)
            except Exception as e:
                log(f"Try another way error: {e}")

        url, text = page_state()
        log(f"{email} — After authenticator click: {url[:70]}, totp_field={'found' if totp_field else 'NOT found'}")

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
            time.sleep(1.0)
        if _totp_redirect_early:
            return _totp_redirect_early

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
                        rand_sleep(1500, 2500)
                    tf = wait_for_any([
                        'input[name="totpPin"]', 'input[name="Pin"]',
                        'input[autocomplete="one-time-code"]', 'input[type="tel"]',
                        'input[aria-label*="code"]',
                    ], timeout=8)
                    if tf:
                        tf.clear()
                        rand_sleep(100, 200)
                        human_type(tf, fresh_code)
                        rand_sleep(400, 600)
                        tf.send_keys(Keys.ENTER)
                        rand_sleep(2000, 3000)
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
