/**
 * /api/phone — the owner's Android phone, reached over Tailscale.
 *
 * GET  /api/phone/status
 * POST /api/phone/pair   { host, pairPort, code, connectPort }
 */

import { Router, type IRouter, type Request, type Response } from "express";
import { spawn } from "child_process";
import { readFileSync } from "fs";
import { join } from "path";

const router: IRouter = Router();
const SCRIPT = join(__dirname, "..", "phone_chrome.py");

function runPhone(args: string[], res: Response) {
  const proc = spawn("python3", [SCRIPT, ...args], {
    stdio: ["ignore", "pipe", "pipe"],
  });
  let out = "";
  let err = "";
  const timer = setTimeout(() => {
    try { proc.kill(); } catch { /* already gone */ }
    if (!res.headersSent) {
      res.status(504).json({ ready: false, message: "Phone command timed out" });
    }
  }, 30_000);
  proc.stdout.on("data", (chunk: Buffer) => { out += chunk.toString(); });
  proc.stderr.on("data", (chunk: Buffer) => { err += chunk.toString(); });
  proc.on("close", (code) => {
    clearTimeout(timer);
    if (res.headersSent) return;
    const line = out.trim().split("\n").filter(Boolean).pop() ?? "";
    try {
      const parsed = JSON.parse(line);
      res.status(code === 0 ? 200 : 400).json(parsed);
    } catch {
      res.status(500).json({
        ready: false,
        message: (err || out || "Phone command failed").slice(0, 200),
      });
    }
  });
}

router.get("/phone/status", (_req, res) => {
  runPhone(["status"], res);
});

router.get("/phone/start.sh", (_req, res) => {
  const script = readFileSync(join(__dirname, "..", "phone-browser.sh"));
  res.setHeader("Content-Type", "text/x-shellscript; charset=utf-8");
  res.send(script);
});

router.post("/phone/pair", (req: Request, res: Response) => {
  const host = typeof req.body?.host === "string" ? req.body.host.trim() : "";
  const code = typeof req.body?.code === "string" ? req.body.code.trim() : "";
  const pairPort = Number(req.body?.pairPort);
  const connectPort = Number(req.body?.connectPort);
  if (!host || !code || !Number.isInteger(pairPort) || !Number.isInteger(connectPort)) {
    res.status(400).json({
      ready: false,
      message: "Host, pairing port, pairing code, aur connect port chahiye.",
    });
    return;
  }
  runPhone([
    "pair",
    "--host", host,
    "--pair-port", String(pairPort),
    "--code", code,
    "--connect-port", String(connectPort),
  ], res);
});

export default router;
