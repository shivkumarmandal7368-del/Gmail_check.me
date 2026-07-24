/**
 * /api/device-check — Fingerprint.com audit via browser.
 *
 * POST /api/device-check/run
 *   Body (JSON, optional):
 *     deviceIndex  number  — index into PHONE_PROFILES (0 = Pixel 6, default)
 *     proxy        string  — proxy URL; overrides the Proxy secret if provided
 *
 * Response: SSE stream
 *   event: log        data: JSON string
 *   event: screenshot data: "<label>:<base64png>"
 *   event: done       data: "{}"
 *   event: error      data: JSON string
 *   event: close      data: JSON { code: number }
 *
 * GET /api/device-check/profiles
 *   Returns JSON array of all device profiles with index + display label.
 */

import { Router } from "express";
import { spawn, execSync } from "child_process";
import { join } from "path";

const router = Router();

const SCRIPT = join(__dirname, "..", "device_check.py");

// Friendly display labels matching PHONE_PROFILES order in gmail_uc_checker.py
const DEVICE_LABELS: string[] = [
  "Pixel 6 · Android 14 · Mali-G78",
  "Pixel 6a · Android 14 · Mali-G78",
  "Pixel 7 · Android 14 · Adreno 730",
  "Pixel 7a · Android 14 · Mali-G710",
  "Pixel 8 · Android 14 · Adreno 740",
  "Pixel 8 Pro · Android 14 · Adreno 740",
  "Pixel 9 · Android 15 · Mali-G715",
  "Pixel 9 Pro · Android 15 · Mali-G715",
  "Samsung Galaxy S21 · Android 14 · Mali-G78",
  "Samsung Galaxy S22 · Android 14 · Xclipse 920",
  "Samsung Galaxy S22 Ultra · Android 14 · Xclipse 920",
  "Samsung Galaxy S23 · Android 14 · Adreno 740",
  "Samsung Galaxy S23 FE · Android 14 · Xclipse 920",
  "Samsung Galaxy S24+ · Android 14 · Xclipse 940",
  "Samsung Galaxy A53 · Android 14 · Mali-G68",
  "Samsung Galaxy A54 · Android 14 · Mali-G68",
  "Samsung Galaxy A34 · Android 14 · Mali-G68",
  "Samsung Galaxy A73 · Android 14 · Adreno 619",
  "OnePlus 11 · Android 14 · Adreno 740",
  "OnePlus 12 · Android 14 · Adreno 750",
  "OnePlus Nord 3 · Android 14 · Mali-G710",
  "Xiaomi 13 · Android 14 · Adreno 740",
  "Xiaomi 14 · Android 14 · Adreno 750",
  "Xiaomi 13T Pro · Android 14 · Dimensity 9200+",
  "Redmi Note 12 Pro · Android 13 · Mali-G68",
  "Realme GT 5 · Android 14 · Adreno 740",
  "Nothing Phone 2 · Android 14 · Adreno 730",
  "Motorola Edge 40 · Android 14 · Mali-G715",
];

// ── GET /api/device-check/profiles ─────────────────────────────────────────
router.get("/device-check/profiles", (_req, res) => {
  res.json({
    profiles: DEVICE_LABELS.map((label, index) => ({ index, label })),
  });
});

// ── POST /api/device-check/run ──────────────────────────────────────────────
router.post("/device-check/run", (req, res) => {
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();

  const send = (event: string, data: string) => {
    try { res.write(`event: ${event}\ndata: ${data}\n\n`); } catch {}
  };

  // Parse optional body params
  const body = req.body ?? {};
  const deviceIndex: number = Number.isInteger(body.deviceIndex) ? body.deviceIndex : 0;
  const proxyOverride: string = typeof body.proxy === "string" ? body.proxy.trim() : "";

  // Resolve python3
  let python3 = "python3";
  try { python3 = execSync("which python3", { encoding: "utf8" }).trim() || "python3"; } catch {}

  send("log", JSON.stringify(`Starting device check (device #${deviceIndex})…`));

  const py = spawn(python3, [SCRIPT], {
    env: {
      ...process.env,
      DEVICE_INDEX: String(deviceIndex),
      ...(proxyOverride ? { PROXY_OVERRIDE: proxyOverride } : {}),
    },
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
        const rest = line.slice("SCREENSHOT".length);
        if (rest.startsWith(":")) {
          const afterColon = rest.slice(1);
          const colonIdx   = afterColon.indexOf(":");
          if (colonIdx !== -1) {
            send("screenshot", `${afterColon.slice(0, colonIdx)}:${afterColon.slice(colonIdx + 1)}`);
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

  req.on("close", () => { try { py.kill("SIGTERM"); } catch {} });
});

export default router;
