/**
 * /api/device-check — Fingerprint.com audit via browser (Pixel 6 + proxy).
 *
 * POST /api/device-check/run
 *   Opens fingerprint.com exactly like the browser checker, takes 3 screenshots,
 *   streams progress + screenshots back to the caller via SSE.
 *
 * SSE event types:
 *   log        — data: JSON string (progress message)
 *   screenshot — data: "<label>:<base64png>"
 *   done       — data: "{}"
 *   error      — data: JSON string (fatal message)
 *   close      — data: JSON { code: number }
 */

import { Router } from "express";
import { spawn } from "child_process";
import { join } from "path";

const router = Router();

// Location of the Python audit script (same dir as gmail_uc_checker.py)
const SCRIPT = join(__dirname, "..", "device_check.py");

router.post("/device-check/run", (req, res) => {
  // ── SSE headers ──────────────────────────────────────────────────────────
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();

  const send = (event: string, data: string) => {
    try {
      res.write(`event: ${event}\ndata: ${data}\n\n`);
    } catch {
      // client already gone
    }
  };

  // ── Resolve python3 ───────────────────────────────────────────────────────
  let python3 = "python3";
  try {
    const { execSync } = require("child_process");
    python3 = execSync("which python3", { encoding: "utf8" }).trim() || "python3";
  } catch {}

  send("log", JSON.stringify("Starting device check…"));

  // ── Spawn Python script ───────────────────────────────────────────────────
  const py = spawn(python3, [SCRIPT], {
    env: { ...process.env },
    stdio: ["ignore", "pipe", "pipe"],
  });

  let buf = "";

  py.stdout.on("data", (chunk: Buffer) => {
    buf += chunk.toString();
    const lines = buf.split("\n");
    buf = lines.pop() ?? "";

    for (const raw of lines) {
      const line = raw.trimEnd();
      if (!line) continue;

      if (line.startsWith("LOG:")) {
        send("log", JSON.stringify(line.slice(4)));

      } else if (line.startsWith("SCREENSHOT")) {
        // Format: SCREENSHOT:<label>:<base64>  OR  SCREENSHOT:<base64>
        const rest = line.slice("SCREENSHOT".length);
        if (rest.startsWith(":")) {
          // rest = ":label:base64"  or  ":base64"
          const afterColon = rest.slice(1);
          const colonIdx = afterColon.indexOf(":");
          if (colonIdx !== -1) {
            const label   = afterColon.slice(0, colonIdx);
            const b64     = afterColon.slice(colonIdx + 1);
            send("screenshot", `${label}:${b64}`);
          } else {
            send("screenshot", `:${afterColon}`);
          }
        }

      } else if (line === "DONE") {
        send("done", "{}");

      } else if (line.startsWith("ERROR:")) {
        send("error", JSON.stringify(line.slice(6)));
      }
    }
  });

  py.stderr.on("data", (chunk: Buffer) => {
    const msg = chunk.toString().trim();
    if (msg) send("log", JSON.stringify(`[stderr] ${msg.slice(0, 500)}`));
  });

  py.on("close", (code) => {
    send("close", JSON.stringify({ code }));
    res.end();
  });

  // ── Abort when client disconnects ────────────────────────────────────────
  req.on("close", () => {
    try { py.kill("SIGTERM"); } catch {}
  });
});

export default router;
