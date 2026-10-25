import { Router, type Response } from "express";
import { spawn } from "child_process";

const router = Router();

/**
 * Public label for the server PROXY_URL. Host, country and state only —
 * never the username or password.
 */
export function describeSavedProxy(raw: string | undefined): { configured: boolean; label: string } {
  const proxy = (raw ?? "").trim();
  if (!proxy) return { configured: false, label: "No saved proxy on this server" };
  try {
    const u = new URL(proxy.includes("://") ? proxy : `http://${proxy}`);
    const host = u.hostname;
    const port = u.port;
    const user = decodeURIComponent(u.username || "");
    const country = user.match(/country[-_]([a-z]{2})/i)?.[1]?.toUpperCase();
    const stateRaw = user.match(/state[-_]([a-z0-9]+)/i)?.[1] ?? "";
    const state = stateRaw
      .replace(/newyork/i, "New York")
      .replace(/newjersey/i, "New Jersey")
      .replace(/losangeles/i, "Los Angeles")
      .replace(/california/i, "California")
      .replace(/^(.)/, (c) => c.toUpperCase());
    const sticky = /-session-[a-z0-9]+$/i.test(user);
    const where = [country, state && state !== country ? state : ""].filter(Boolean).join(" / ");
    const endpoint = host ? (port ? `${host}:${port}` : host) : "saved proxy";
    const bits = [endpoint, where, sticky ? "sticky session" : "rotating"].filter(Boolean);
    return { configured: true, label: bits.join(" · ") };
  } catch {
    return { configured: true, label: "Saved proxy is set" };
  }
}

/** GET /api/proxy/status — whether PROXY_URL is configured, without secrets. */
router.get("/proxy/status", (_req, res) => {
  res.json(describeSavedProxy(process.env.PROXY_URL));
});

const CHECK_SCRIPT = `
import sys, json, re
from urllib.parse import urlparse, quote
import requests

def redact(s):
    return re.sub(r'(https?://)[^\\s/@]+:[^\\s/@]+@', r'\\1[redacted]@', str(s))

proxy_url = sys.stdin.readline().strip()
try:
    parsed = urlparse(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    host = parsed.hostname or ""
    port = parsed.port or 80
    user = quote(parsed.username or "", safe="")
    password = quote(parsed.password or "", safe="")
    _pu = f"http://{user}:{password}@{host}:{port}" if user else f"http://{host}:{port}"
    r = requests.get(
        "http://api.ipify.org?format=json",
        proxies={"http": _pu, "https": _pu},
        timeout=22,
    )
    r.raise_for_status()
    ip = r.json().get("ip", "unknown")
    print(json.dumps({"ok": True, "ip": ip}))
except requests.exceptions.ProxyError as e:
    msg = redact(e)
    if "407" in msg:
        print(json.dumps({"ok": False, "error": "407 — username ya password galat hai, ya plan expire ho gaya"}))
    else:
        print(json.dumps({"ok": False, "error": "Proxy connection failed: " + msg[:180]}))
except requests.exceptions.ConnectTimeout:
    print(json.dumps({"ok": False, "error": "Proxy timeout — host unreachable ya port band hai"}))
except Exception as e:
    print(json.dumps({"ok": False, "error": redact(e)[:180]}))
`;

function runProxyCheck(proxy: string, res: Response) {
  const proc = spawn("python3", ["-c", CHECK_SCRIPT], {
    stdio: ["pipe", "pipe", "pipe"],
    env: process.env,
  });

  let out = "";

  const timer = setTimeout(() => {
    try { proc.kill(); } catch { /* already gone */ }
    if (!res.headersSent) {
      res.json({ ok: false, error: "Proxy check timeout (25s) — host unreachable" });
    }
  }, 28_000);

  proc.stdout.on("data", (d: Buffer) => { out += d.toString(); });
  proc.stderr.on("data", () => { /* keep proxy URLs out of the API log */ });

  proc.stdin.write(proxy + "\n", "utf8");
  proc.stdin.end();

  proc.on("close", () => {
    clearTimeout(timer);
    if (res.headersSent) return;
    try {
      const lastLine = out.trim().split("\n").filter(Boolean).pop() ?? "";
      const parsed = JSON.parse(lastLine);
      res.json(parsed);
    } catch {
      res.json({ ok: false, error: "Unexpected response from proxy check" });
    }
  });

  proc.on("error", (err) => {
    clearTimeout(timer);
    if (!res.headersSent) {
      res.json({ ok: false, error: `Failed to spawn python3: ${err.message}` });
    }
  });
}

/**
 * POST /api/proxy/check
 * Body: { useSaved: true }  — test process.env.PROXY_URL
 *    or { proxy: string }   — test a custom URL
 *
 * Returns { ok, ip } or { ok: false, error }. Never echoes the proxy URL.
 */
router.post("/proxy/check", (req, res) => {
  const body = (req.body ?? {}) as { proxy?: unknown; useSaved?: unknown };
  let proxy = "";
  if (body.useSaved === true) {
    proxy = (process.env.PROXY_URL ?? "").trim();
    if (!proxy) {
      res.status(400).json({ ok: false, error: "Saved proxy is not configured on the server" });
      return;
    }
  } else if (typeof body.proxy === "string" && body.proxy.trim()) {
    proxy = body.proxy.trim();
  } else {
    res.status(400).json({ ok: false, error: "proxy field required" });
    return;
  }
  runProxyCheck(proxy, res);
});

export default router;
