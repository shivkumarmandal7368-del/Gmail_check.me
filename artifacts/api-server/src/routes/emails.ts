import { Router, type IRouter } from "express";
import { CheckEmailsBody, GetEmailStatsBody } from "@workspace/api-zod";
import { verifyEmails } from "../lib/emailVerifier.js";

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

export default router;
