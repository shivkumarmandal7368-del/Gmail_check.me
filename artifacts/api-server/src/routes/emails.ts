import { Router, type IRouter } from "express";
import { CheckEmailsBody, GetEmailStatsBody, LoginCheckEmailsBody, BrowserCheckEmailsBody } from "@workspace/api-zod";
import { verifyEmails } from "../lib/emailVerifier.js";
import { checkGmailLogins } from "../lib/imapChecker.js";
import { browserLoginCheck } from "../lib/browserLoginChecker.js";

const router: IRouter = Router();

router.post("/emails/check", async (req, res) => {
  const parsed = CheckEmailsBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }

  const { emails } = parsed.data;

  if (emails.length === 0) {
    res.status(400).json({ error: "No email addresses provided" });
    return;
  }

  const results = await verifyEmails(emails);

  const valid = results.filter((r) => r.status === "valid").length;
  const invalid = results.filter((r) => r.status === "invalid").length;
  const disabled = results.filter((r) => r.status === "disabled").length;
  const catchAll = results.filter((r) => r.status === "catch_all").length;
  const unknown = results.filter((r) => r.status === "unknown").length;

  res.json({
    results,
    total: results.length,
    valid,
    invalid,
    disabled,
    catchAll,
    unknown,
  });
});

router.post("/emails/stats", (req, res) => {
  const parsed = GetEmailStatsBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }

  const { results } = parsed.data;
  const total = results.length;
  const valid = results.filter((r) => r.status === "valid").length;
  const invalid = results.filter((r) => r.status === "invalid").length;
  const disabled = results.filter((r) => r.status === "disabled").length;
  const catchAll = results.filter((r) => r.status === "catch_all").length;
  const unknown = results.filter((r) => r.status === "unknown").length;

  res.json({
    total,
    valid,
    invalid,
    disabled,
    catchAll,
    unknown,
    validPercent: total > 0 ? Math.round((valid / total) * 100) : 0,
    invalidPercent: total > 0 ? Math.round((invalid / total) * 100) : 0,
  });
});

router.post("/emails/browser-check", async (req, res) => {
  const parsed = BrowserCheckEmailsBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const { credentials, proxy } = parsed.data;
  if (credentials.length === 0) {
    res.status(400).json({ error: "No credentials provided" });
    return;
  }
  // concurrency: how many accounts to check simultaneously (default 3, max 10)
  const concurrency = typeof req.body.concurrency === "number"
    ? Math.max(1, Math.min(10, Math.floor(req.body.concurrency)))
    : 3;
  const results = await browserLoginCheck(
    credentials as Array<{ email: string; password: string; totp?: string }>,
    proxy,
    concurrency,
  );
  res.json({
    results,
    total: results.length,
    opened: results.filter((r) => r.status === "opened").length,
    verificationRequired: results.filter((r) => r.status === "verification_required").length,
    wrongPassword: results.filter((r) => r.status === "wrong_password").length,
    twoFaRequired: results.filter((r) => r.status === "2fa_required").length,
    unknown: results.filter((r) => r.status === "unknown").length,
  });
});

// SSE streaming endpoint — sends each account result as it completes (real-time progress)
router.post("/emails/browser-check-stream", async (req, res) => {
  const parsed = BrowserCheckEmailsBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const { credentials, proxy } = parsed.data;
  if (credentials.length === 0) {
    res.status(400).json({ error: "No credentials provided" });
    return;
  }
  const concurrency = typeof req.body.concurrency === "number"
    ? Math.max(1, Math.min(10, Math.floor(req.body.concurrency)))
    : 3;

  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();

  const sendEvent = (data: object) => {
    try {
      res.write(`data: ${JSON.stringify(data)}\n\n`);
      // Force flush if available (Express 5 / Node streams)
      if (typeof (res as any).flush === "function") (res as any).flush();
    } catch {}
  };

  // Send "started" event so client knows total count
  sendEvent({ type: "started", total: credentials.length, concurrency });

  try {
    await browserLoginCheck(
      credentials as Array<{ email: string; password: string; totp?: string }>,
      proxy,
      concurrency,
      (result) => sendEvent({ type: "result", ...result }),
    );
  } catch (err) {
    sendEvent({ type: "error", message: String(err) });
  }

  sendEvent({ type: "done" });
  res.end();
});

router.post("/emails/login-check", async (req, res) => {
  const parsed = LoginCheckEmailsBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }

  const { credentials } = parsed.data;

  if (credentials.length === 0) {
    res.status(400).json({ error: "No credentials provided" });
    return;
  }

  const results = await checkGmailLogins(credentials);

  res.json({
    results,
    total: results.length,
    accessible: results.filter((r) => r.status === "accessible").length,
    verificationRequired: results.filter((r) => r.status === "verification_required").length,
    wrongPassword: results.filter((r) => r.status === "wrong_password").length,
    appPasswordRequired: results.filter((r) => r.status === "app_password_required").length,
    unknown: results.filter((r) => r.status === "unknown").length,
  });
});

export default router;
