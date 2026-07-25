import { randomUUID } from "node:crypto";
import { Router, type Response } from "express";
import { execSync, spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { join } from "node:path";

const router = Router();
const SCRIPT = join(__dirname, "..", "manual_browser.py");

const DEVICE_LABELS = [
  "Pixel 6 · Android 14 · Mali-G78 MP20", "Pixel 6a · Android 14 · Mali-G78 MP20",
  "Pixel 7 · Android 14 · Adreno 730", "Pixel 7a · Android 14 · Mali-G710 MP7",
  "Pixel 8 · Android 14 · Adreno 740", "Pixel 8 Pro · Android 14 · Adreno 740",
  "Pixel 9 · Android 15 · Mali-G715 MP7", "Pixel 9 Pro · Android 15 · Mali-G715 MP7",
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
  "Pixel 8a · Android 14 · Adreno 740", "OnePlus Nord 4 (CPH2609) · Android 14 · Adreno 735",
  "Xiaomi 15 (24129PN74G) · Android 15 · Adreno 830",
  "Redmi Note 14 Pro+ (24117RA73G) · Android 14 · Adreno 720",
  "Nothing Phone (2a) (A142) · Android 14 · Mali-G610 MC4",
  "Realme GT 6 (RMX3851) · Android 14 · Adreno 735",
  "Oppo Reno 12 Pro (CPH2629) · Android 14 · Immortalis-G720 MC12",
  "vivo X100 Pro (V2324A) · Android 14 · Immortalis-G720 MC12",
];

type ManualEvent = { event: string; data: unknown };
type ManualSession = {
  id: string;
  child: ChildProcessWithoutNullStreams;
  clients: Set<Response>;
  events: ManualEvent[];
  closed: boolean;
  cleanupTimer?: NodeJS.Timeout;
};

const sessions = new Map<string, ManualSession>();

function writeEvent(res: Response, event: string, data: unknown) {
  if (!res.writableEnded) {
    res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  }
}

function broadcast(session: ManualSession, event: string, data: unknown) {
  if (event !== "screenshot" || !session.closed) {
    session.events.push({ event, data });
    if (session.events.length > 30) session.events.shift();
  }
  for (const client of session.clients) writeEvent(client, event, data);
}

function stopSession(session: ManualSession) {
  if (session.closed) return;
  session.closed = true;
  try { session.child.stdin.write(JSON.stringify({ action: "stop" }) + "\n"); } catch {}
  setTimeout(() => {
    if (!session.child.killed) {
      try { session.child.kill("SIGTERM"); } catch {}
    }
  }, 2_000).unref();
  session.cleanupTimer = setTimeout(() => sessions.delete(session.id), 10 * 60_000);
  session.cleanupTimer.unref();
}

function getSession(id: string, res: Response) {
  const session = sessions.get(id);
  if (!session) {
    res.status(404).json({ error: "Manual browser session not found" });
    return null;
  }
  return session;
}

router.post("/manual-browser/start", (req, res) => {
  const deviceIndex = Number.isInteger(req.body?.deviceIndex) ? req.body.deviceIndex : 0;
  if (deviceIndex < 0 || deviceIndex >= DEVICE_LABELS.length) {
    return res.status(400).json({ error: "Unknown device profile" });
  }
  const proxy = typeof req.body?.proxy === "string" ? req.body.proxy.trim() : "";
  if (proxy.length > 1000) return res.status(400).json({ error: "Proxy value is too long" });

  let python3 = "python3";
  try { python3 = execSync("which python3", { encoding: "utf8" }).trim() || "python3"; } catch {}

  const child = spawn(python3, [SCRIPT], {
    env: {
      ...process.env,
      DEVICE_INDEX: String(deviceIndex),
      ...(proxy ? { PROXY_OVERRIDE: proxy } : {}),
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const session: ManualSession = {
    id: randomUUID(),
    child,
    clients: new Set(),
    events: [],
    closed: false,
  };
  sessions.set(session.id, session);

  let buffer = "";
  child.stdout.on("data", (chunk: Buffer) => {
    buffer += chunk.toString();
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.startsWith("EVENT:")) continue;
      try {
        const parsed = JSON.parse(line.slice(6)) as { type: string; data: unknown };
        if (parsed.type === "closed") {
          session.closed = true;
          broadcast(session, "closed", parsed.data ?? {});
        } else if (parsed.type === "error") {
          broadcast(session, "error", parsed.data ?? "Manual browser failed");
        } else {
          broadcast(session, parsed.type, parsed.data ?? {});
        }
      } catch {
        broadcast(session, "log", "Browser returned an unreadable event");
      }
    }
  });
  child.stderr.on("data", (chunk: Buffer) => {
    const text = chunk.toString().trim();
    if (text) broadcast(session, "log", "[browser] process started");
  });
  child.on("error", (error) => broadcast(session, "error", `Could not start browser: ${error.message}`));
  child.on("close", (code) => {
    if (!session.closed) {
      session.closed = true;
      broadcast(session, "closed", { code });
    }
  });

  return res.json({ sessionId: session.id, device: DEVICE_LABELS[deviceIndex] });
});

router.get("/manual-browser/:id/stream", (req, res) => {
  const session = getSession(req.params.id, res);
  if (!session) return;
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();
  session.clients.add(res);
  for (const item of session.events) writeEvent(res, item.event, item.data);
  req.on("close", () => session.clients.delete(res));
});

router.post("/manual-browser/:id/action", (req, res) => {
  const session = getSession(req.params.id, res);
  if (!session) return;
  if (session.closed) return res.status(409).json({ error: "Manual browser is closed" });
  const action = req.body ?? {};
  if (!["navigate", "click", "type", "key", "screenshot"].includes(action.action)) {
    return res.status(400).json({ error: "Unsupported browser action" });
  }
  if (action.action === "navigate" && (typeof action.url !== "string" || action.url.length > 2048)) {
    return res.status(400).json({ error: "Invalid URL" });
  }
  if (action.action === "type" && (typeof action.text !== "string" || action.text.length > 4000)) {
    return res.status(400).json({ error: "Text is limited to 4,000 characters" });
  }
  try {
    session.child.stdin.write(JSON.stringify(action) + "\n");
    return res.json({ ok: true });
  } catch {
    return res.status(409).json({ error: "Could not send action to browser" });
  }
});

router.post("/manual-browser/:id/stop", (req, res) => {
  const session = getSession(req.params.id, res);
  if (!session) return;
  stopSession(session);
  return res.json({ ok: true });
});

export default router;