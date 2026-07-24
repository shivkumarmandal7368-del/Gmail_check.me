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
  "Pixel 6 · Android 14 · Mali-G78 MP20",
  "Pixel 6a · Android 14 · Mali-G78 MP20",
  "Pixel 7 · Android 14 · Adreno 730",
  "Pixel 7a · Android 14 · Mali-G710 MP7",
  "Pixel 8 · Android 14 · Adreno 740",
  "Pixel 8 Pro · Android 14 · Adreno 740",
  "Pixel 9 · Android 15 · Mali-G715 MP7",
  "Pixel 9 Pro · Android 15 · Mali-G715 MP7",
  "Samsung Galaxy S21 (SM-G991B) · Android 14 · Mali-G78 MP14",
  "Samsung Galaxy S22 (SM-S901B) · Android 14 · Xclipse 920",
  "Samsung Galaxy S22 Ultra (SM-S908B) · Android 14 · Xclipse 920",
  "Samsung Galaxy S23 (SM-S911B) · Android 14 · Adreno 740",
  "Samsung Galaxy S23 FE (SM-S711B) · Android 14 · Xclipse 920",
  "Samsung Galaxy S24+ (SM-S926B) · Android 14 · Xclipse 940",
  "Samsung Galaxy A53 (SM-A536B) · Android 14 · Mali-G68 MC4",
  "Samsung Galaxy A54 (SM-A546B) · Android 14 · Mali-G68",
  "Samsung Galaxy A34 (SM-A346B) · Android 14 · Mali-G68",
  "Samsung Galaxy A73 (SM-A736B) · Android 14 · Adreno 642L",
  "OnePlus 11 (CPH2423) · Android 14 · Adreno 740",
  "OnePlus 12 (CPH2447) · Android 14 · Adreno 750",
  "OnePlus Nord 3 (CPH2493) · Android 14 · Mali-G710 MC10",
  "Xiaomi 13 (2211133G) · Android 14 · Adreno 740",
  "Xiaomi 14 (23049PCD8G) · Android 14 · Adreno 750",
  "Xiaomi 13T Pro (23078PND5G) · Android 14 · Mali-G715 MC11",
  "Redmi Note 12 Pro (22101316G) · Android 13 · Mali-G68 MC4",
  "Realme GT 5 (RMX3706) · Android 14 · Adreno 740",
  "Nothing Phone 2 (A065) · Android 14 · Adreno 730",
  "Motorola Edge 40 (XT2303-2) · Android 14 · Mali-G77 MC9",
  "Vivo V29 (V2246) · Android 14 · Adreno 642L",
  "Oppo Find X6 (PGEM10) · Android 14 · Mali-G715 MC11",
  "Pixel 9 Pro XL · Android 15 · Mali-G715 MP7",
  "Samsung Galaxy S24 (SM-S921B) · Android 14 · Xclipse 940",
  "Samsung Galaxy S24 Ultra (SM-S928B) · Android 14 · Adreno 750",
  "Samsung Galaxy S25 (SM-S931B) · Android 15 · Adreno 830",
  "Samsung Galaxy A55 (SM-A556B) · Android 14 · Xclipse 530",
  "OnePlus 13 (CPH2655) · Android 15 · Adreno 830",
  "Xiaomi 14 Ultra (24030PN60G) · Android 14 · Adreno 750",
  "Xiaomi 14T Pro (23127PN0CG) · Android 14 · Immortalis-G720 MC12",
  "Redmi Note 13 Pro+ (23013PC75G) · Android 13 · Mali-G610 MC4",
  "ASUS ROG Phone 8 (AI2401) · Android 14 · Adreno 750",
  "Motorola Edge 50 Pro (XT2403-3) · Android 14 · Adreno 720",
  "Sony Xperia 1 VI (XQ-EC54) · Android 14 · Adreno 750",
  "Samsung Galaxy S25+ (SM-S936B) · Android 15 · Adreno 830",
  "Samsung Galaxy S25 Ultra (SM-S938B) · Android 15 · Adreno 830",
  "Pixel 8a · Android 14 · Adreno 740",
  "OnePlus Nord 4 (CPH2609) · Android 14 · Adreno 735",
  "Xiaomi 15 (24129PN74G) · Android 15 · Adreno 830",
  "Redmi Note 14 Pro+ (24117RA73G) · Android 14 · Adreno 720",
  "Nothing Phone (2a) (A142) · Android 14 · Mali-G610 MC4",
  "Realme GT 6 (RMX3851) · Android 14 · Adreno 735",
  "Oppo Reno 12 Pro (CPH2629) · Android 14 · Immortalis-G720 MC12",
  "vivo X100 Pro (V2324A) · Android 14 · Immortalis-G720 MC12",
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

  // The request socket can close as soon as the POST body has been consumed.
  // Watch the response instead so a normal POST does not terminate the
  // long-running SSE child before it can emit progress or screenshots.
  res.on("close", () => {
    if (!res.writableEnded) {
      try { py.kill("SIGTERM"); } catch {}
    }
  });
});

export default router;
