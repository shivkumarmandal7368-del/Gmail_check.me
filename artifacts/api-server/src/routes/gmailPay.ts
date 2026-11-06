import { Router, type IRouter } from "express";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const router: IRouter = Router();
const configPath = path.join(process.cwd(), "artifacts/data/gmail-pay/config.json");

type GmailPayConfig = {
  chatTarget: string;
  liveSaveEnabled: boolean;
};

const DEFAULTS: GmailPayConfig = {
  chatTarget: "Gmailpaybot",
  liveSaveEnabled: false,
};

function asTarget(value: unknown, fallback: string): string {
  const raw = String(value ?? "").trim().replace(/^@/, "");
  if (/^[A-Za-z][A-Za-z0-9_]{3,31}$/.test(raw)) return raw;
  return fallback;
}

async function readConfig(): Promise<GmailPayConfig> {
  try {
    const raw = await readFile(configPath, "utf8");
    const data = JSON.parse(raw) as Partial<GmailPayConfig>;
    return {
      chatTarget: asTarget(data.chatTarget, DEFAULTS.chatTarget),
      liveSaveEnabled: Boolean(data.liveSaveEnabled),
    };
  } catch {
    return { ...DEFAULTS };
  }
}

router.get("/gmail-pay/status", async (_req, res) => {
  const config = await readConfig();
  res.json({ ok: true, ...config });
});

router.post("/gmail-pay/config", async (req, res) => {
  try {
    const current = await readConfig();
    const requested = String(req.body?.chatTarget ?? "").trim();
    if (requested && !/^[A-Za-z@][A-Za-z0-9_]{3,32}$/.test(requested.replace(/^@/, ""))) {
      res.status(400).json({ ok: false, error: "Bot name sirf letters, numbers, underscore" });
      return;
    }
    const next: GmailPayConfig = {
      chatTarget: requested ? asTarget(requested, current.chatTarget) : current.chatTarget,
      liveSaveEnabled: req.body?.liveSaveEnabled != null ? Boolean(req.body.liveSaveEnabled) : current.liveSaveEnabled,
    };
    await mkdir(path.dirname(configPath), { recursive: true });
    await writeFile(configPath, `${JSON.stringify(next, null, 2)}\n`, "utf8");
    res.json({ ok: true, ...next });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    res.status(400).json({ ok: false, error: message });
  }
});

export default router;
