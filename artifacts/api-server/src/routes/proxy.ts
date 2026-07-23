import { Router } from "express";
import { spawn } from "child_process";

const router = Router();

/**
 * POST /api/proxy/check
 * Body: { proxy: string }
 *
 * Tests whether the given proxy URL actually works by fetching the exit IP
 * through it using Python requests (already installed for gmail_uc_checker).
 *
 * Returns:
 *   { ok: true,  ip: "1.2.3.4" }          — proxy works, this is the exit IP
 *   { ok: false, error: "reason..." }      — proxy failed (407, timeout, etc.)
 */
router.post("/proxy/check", (req, res) => {
  const { proxy } = req.body as { proxy?: unknown };

  if (!proxy || typeof proxy !== "string") {
    res.status(400).json({ ok: false, error: "proxy field required" });
    return;
  }

  const script = `
import sys, json, requests
proxy_url = sys.stdin.readline().strip()
try:
    r = requests.get(
        "https://api.ipify.org?format=json",
        proxies={"http": proxy_url, "https": proxy_url},
        timeout=12,
    )
    r.raise_for_status()
    ip = r.json().get("ip", "unknown")
    print(json.dumps({"ok": True, "ip": ip}))
except requests.exceptions.ProxyError as e:
    msg = str(e)
    if "407" in msg:
        print(json.dumps({"ok": False, "error": "407 — username ya password galat hai, ya plan expire ho gaya"}))
    else:
        print(json.dumps({"ok": False, "error": f"Proxy connection failed: {msg[:200]}"}))
except requests.exceptions.ConnectTimeout:
    print(json.dumps({"ok": False, "error": "Proxy timeout — host unreachable ya port band hai"}))
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)[:200]}))
`;

  const proc = spawn("python3", ["-c", script], {
    stdio: ["pipe", "pipe", "pipe"],
    env: process.env,
  });

  let out = "";

  const timer = setTimeout(() => {
    try { proc.kill(); } catch {}
    if (!res.headersSent) {
      res.json({ ok: false, error: "Proxy check timeout (15s) — host unreachable" });
    }
  }, 15_000);

  proc.stdout.on("data", (d: Buffer) => { out += d.toString(); });
  proc.stderr.on("data", () => {}); // suppress Python tracebacks from server logs

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
});

export default router;
