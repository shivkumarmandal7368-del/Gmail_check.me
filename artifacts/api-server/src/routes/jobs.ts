/**
 * /api/jobs — Background job management routes.
 *
 * POST   /api/jobs                 Create and start a new background job
 * GET    /api/jobs                 List recent jobs (metadata only)
 * GET    /api/jobs/active          Get the currently running job (if any)
 * GET    /api/jobs/:id             Get full job state (results, progress, etc.)
 * GET    /api/jobs/:id/stream      SSE stream — replays past events then sends live ones
 *                                  Supports ?since=N to skip already-seen events
 * POST   /api/jobs/:id/cancel      Cancel a running job
 *
 * IMPORTANT: /api/jobs/active MUST be registered before /api/jobs/:id so
 * Express doesn't treat "active" as a job ID.
 */

import { Router, type IRouter, type Request, type Response } from "express";
import { BrowserCheckEmailsBody } from "@workspace/api-zod";
import {
  getJob,
  listJobs,
  getActiveJob,
  subscribeToJob,
  type Job,
} from "../lib/jobStore.js";
import { startJob, abortJob } from "../lib/jobRunner.js";

const router: IRouter = Router();

// ── POST /jobs — create & start a background job ──────────────────────────────

router.post("/jobs", (req: Request, res: Response) => {
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

  const concurrency =
    typeof req.body.concurrency === "number"
      ? Math.max(1, Math.min(10, Math.floor(req.body.concurrency)))
      : 3;

  const proxies: string[] = Array.isArray(req.body.proxies)
    ? (req.body.proxies as unknown[])
        .filter((p): p is string => typeof p === "string" && p.trim().length > 0)
        .map(p => p.trim())
    : [];

  const freshProfile = req.body.freshProfile === true;

  const jobId = startJob({
    credentials: credentials as Array<{ email: string; password: string; totp?: string }>,
    proxy,
    proxies: proxies.length > 0 ? proxies : undefined,
    concurrency,
    freshProfile,
  });

  res.json({ jobId });
});

// ── GET /jobs — list all jobs ─────────────────────────────────────────────────

router.get("/jobs", (_req: Request, res: Response) => {
  const jobs = listJobs().map(j => ({
    id: j.id,
    status: j.status,
    createdAt: j.createdAt,
    updatedAt: j.updatedAt,
    completedAt: j.completedAt ?? null,
    total: j.total,
    completed: j.results.length,
    concurrency: j.concurrency,
    freshProfile: j.freshProfile,
    errorMessage: j.errorMessage ?? null,
  }));
  res.json({ jobs });
});

// ── GET /jobs/active — currently running job ─────────────────────────────────
// MUST be before /jobs/:id to avoid "active" being treated as an ID

router.get("/jobs/active", (_req: Request, res: Response) => {
  const job = getActiveJob();
  res.json({ job: job ? sanitizeJob(job) : null });
});

// ── GET /jobs/:id — full job state ────────────────────────────────────────────

router.get("/jobs/:id", (req: Request, res: Response) => {
  const job = getJob(String(req.params.id));
  if (!job) {
    res.status(404).json({ error: "Job not found" });
    return;
  }
  res.json({ job: sanitizeJob(job) });
});

// ── GET /jobs/:id/stream — SSE stream ────────────────────────────────────────

router.get("/jobs/:id/stream", (req: Request, res: Response) => {
  const job = getJob(String(req.params.id));
  if (!job) {
    res.status(404).json({ error: "Job not found" });
    return;
  }

  // ?since=N: skip events the client already has (efficient reconnect).
  // Default 0 = replay all events from the beginning.
  const since = Math.max(0, parseInt((req.query.since as string) ?? "0", 10) || 0);

  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();

  const sendEvent = (data: object) => {
    try {
      res.write(`data: ${JSON.stringify(data)}\n\n`);
      if (typeof (res as any).flush === "function") (res as any).flush();
    } catch {}
  };

  // Replay past events (from `since` offset so reconnecting clients don't
  // re-process events they already handled).
  const pastEvents = job.events.slice(since);
  for (const event of pastEvents) {
    sendEvent(event);
  }

  // If job is already finished, send a final marker and close.
  if (job.status !== "running") {
    sendEvent({ type: job.status, timestamp: Date.now(), message: job.errorMessage });
    res.end();
    return;
  }

  // Subscribe to live events for running jobs.
  const unsubscribe = subscribeToJob(job.id, (event) => {
    sendEvent(event);
    // Close the SSE stream once the job reaches a terminal state.
    if (
      event.type === "done" ||
      event.type === "error" ||
      event.type === "cancelled" ||
      event.type === "interrupted"
    ) {
      setTimeout(() => { try { res.end(); } catch {} }, 150);
    }
  });

  // Heartbeat every 25 s — keeps proxy and load-balancer connections alive.
  const heartbeat = setInterval(() => {
    try {
      res.write(": heartbeat\n\n");
      if (typeof (res as any).flush === "function") (res as any).flush();
    } catch {}
  }, 25_000);

  // Clean up on client disconnect.
  req.on("close", () => {
    unsubscribe();
    clearInterval(heartbeat);
  });
});

// ── POST /jobs/:id/cancel — cancel a running job ──────────────────────────────

router.post("/jobs/:id/cancel", (req: Request, res: Response) => {
  const ok = abortJob(String(req.params.id));
  if (!ok) {
    res.status(400).json({ error: "Job is not running or not found" });
    return;
  }
  res.json({ ok: true });
});

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Sanitize a job for the REST response:
 * - Strip passwords from credentials
 * - Mask the proxy password
 * - Include eventsCount (not the full event array) — frontend uses this for ?since=N
 */
function sanitizeJob(job: Job): object {
  return {
    id: job.id,
    status: job.status,
    createdAt: job.createdAt,
    updatedAt: job.updatedAt,
    completedAt: job.completedAt ?? null,
    total: job.total,
    concurrency: job.concurrency,
    freshProfile: job.freshProfile,
    checkingEmails: job.checkingEmails,
    results: job.results,
    /** Number of events logged so far — used by the frontend for ?since=N */
    eventsCount: job.events.length,
    errorMessage: job.errorMessage ?? null,
    /** Credential emails only — no passwords */
    credentialEmails: job.credentials.map(c => c.email),
    proxy: job.proxy ? maskProxy(job.proxy) : null,
    proxiesCount: job.proxies?.length ?? 0,
  };
}

function maskProxy(url: string): string {
  try {
    const u = new URL(url);
    if (u.password) u.password = "****";
    return u.toString();
  } catch {
    return url;
  }
}

export default router;
