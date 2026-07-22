/**
 * JobStore — persistent file-backed store for background browser-check jobs.
 *
 * Each job is stored as {jobId}.json in .job-data/ (relative to process.cwd()).
 * On server startup, any jobs in "running" state are marked "interrupted" —
 * their partial results are preserved so the frontend can display them.
 *
 * In-memory: subscriber callbacks for SSE broadcasting.
 * On-disk:   full job state including all results and event log.
 */

import { readFile, writeFile, mkdir, readdir, unlink } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { randomBytes } from "node:crypto";

// ── Types ─────────────────────────────────────────────────────────────────────

export type JobStatus =
  | "running"
  | "completed"
  | "cancelled"
  | "failed"
  | "interrupted";

export interface JobEvent {
  type: string;
  timestamp: number;
  [key: string]: unknown;
}

export interface JobResult {
  email: string;
  status: string;
  reason: string;
  totpCode: string | null;
  debugScreenshot?: string;
  exitIp?: string;
  fingerprint?: string;
  proxySession?: string;
  durationMs?: number;
}

export interface Job {
  id: string;
  status: JobStatus;
  createdAt: number;
  updatedAt: number;
  completedAt?: number;

  /** Input params — preserved for display and retry */
  credentials: Array<{ email: string; password: string; totp?: string }>;
  proxy?: string;
  proxies?: string[];
  concurrency: number;
  freshProfile: boolean;

  /** Progress */
  total: number;
  results: JobResult[];
  checkingEmails: string[];

  /** SSE event log — sliced by ?since=N for efficient reconnects */
  events: JobEvent[];

  /** Human-readable message for failed/interrupted jobs */
  errorMessage?: string;
}

// ── Storage ───────────────────────────────────────────────────────────────────

const DATA_DIR = join(process.cwd(), ".job-data");
const MAX_JOBS = 50;

/** In-memory job map (primary source of truth while server is alive) */
const jobs = new Map<string, Job>();

/** In-memory SSE subscriber sets — not persisted */
const subscribers = new Map<string, Set<(event: JobEvent) => void>>();

// ── Init ──────────────────────────────────────────────────────────────────────

/**
 * Initialize the store: create the data directory, then load and recover
 * all persisted jobs. Any job marked "running" on disk was interrupted by
 * a server restart — mark it "interrupted" so the frontend can display
 * partial results with an appropriate notice.
 */
export async function initJobStore(): Promise<void> {
  await mkdir(DATA_DIR, { recursive: true });

  let files: string[] = [];
  try { files = await readdir(DATA_DIR); } catch {}

  const jsonFiles = files.filter(f => f.endsWith(".json")).sort();

  for (const file of jsonFiles) {
    try {
      const raw = await readFile(join(DATA_DIR, file), "utf8");
      const job = JSON.parse(raw) as Job;

      if (job.status === "running") {
        job.status = "interrupted";
        job.updatedAt = Date.now();
        job.completedAt = Date.now();
        job.checkingEmails = [];
        job.errorMessage =
          "Server restarted — job was interrupted. Partial results shown below.";
        job.events.push({
          type: "interrupted",
          timestamp: Date.now(),
          message: job.errorMessage,
        });
        await persistJob(job);
      }

      jobs.set(job.id, job);
    } catch (e) {
      console.error(`[JobStore] Failed to load ${file}:`, e);
    }
  }

  console.info(`[JobStore] Loaded ${jobs.size} job(s) from disk`);
}

// ── Persistence ───────────────────────────────────────────────────────────────

/** Write job state to disk (fire-and-forget; errors are logged but not thrown). */
function persistJob(job: Job): Promise<void> {
  return writeFile(
    join(DATA_DIR, `${job.id}.json`),
    JSON.stringify(job),
    "utf8",
  ).catch(e => console.error(`[JobStore] Persist error for ${job.id}:`, e));
}

// ── CRUD ──────────────────────────────────────────────────────────────────────

/** Create and persist a new job. The caller is responsible for starting it. */
export function createJob(params: {
  credentials: Job["credentials"];
  proxy?: string;
  proxies?: string[];
  concurrency: number;
  freshProfile: boolean;
}): Job {
  const id = randomBytes(8).toString("hex");
  const job: Job = {
    id,
    status: "running",
    createdAt: Date.now(),
    updatedAt: Date.now(),
    ...params,
    total: params.credentials.length,
    results: [],
    checkingEmails: [],
    events: [],
  };
  jobs.set(id, job);
  persistJob(job);
  // Prune old jobs async (non-blocking)
  pruneOldJobs().catch(() => {});
  return job;
}

/** Fetch a single job by ID. */
export function getJob(id: string): Job | undefined {
  return jobs.get(id);
}

/** Return all jobs sorted newest-first. */
export function listJobs(): Job[] {
  return Array.from(jobs.values()).sort((a, b) => b.createdAt - a.createdAt);
}

/** Return the currently running job (at most one at a time). */
export function getActiveJob(): Job | undefined {
  return Array.from(jobs.values()).find(j => j.status === "running");
}

// ── Event System ──────────────────────────────────────────────────────────────

/**
 * Emit an event for a job: append to the event log, update derived state,
 * persist to disk, and broadcast to all connected SSE subscribers.
 */
export function emitJobEvent(jobId: string, event: JobEvent): void {
  const job = jobs.get(jobId);
  if (!job) return;

  job.updatedAt = Date.now();
  job.events.push(event);

  // Update derived state
  switch (event.type) {
    case "checking": {
      const email = event.email as string;
      if (!job.checkingEmails.includes(email)) job.checkingEmails.push(email);
      break;
    }
    case "result": {
      const { type: _t, timestamp: _ts, ...rest } = event;
      job.results.push(rest as JobResult);
      job.checkingEmails = job.checkingEmails.filter(
        e => e !== (event.email as string),
      );
      break;
    }
    case "done":
      job.status = "completed";
      job.completedAt = Date.now();
      job.checkingEmails = [];
      break;
    case "error":
      job.status = "failed";
      job.errorMessage = event.message as string;
      job.completedAt = Date.now();
      job.checkingEmails = [];
      break;
    case "cancelled":
      job.status = "cancelled";
      job.completedAt = Date.now();
      job.checkingEmails = [];
      break;
  }

  persistJob(job);

  // Broadcast to SSE subscribers
  const subs = subscribers.get(jobId);
  if (subs && subs.size > 0) {
    for (const cb of subs) {
      try { cb(event); } catch {}
    }
  }
}

/**
 * Subscribe to a job's live events.
 * Returns an unsubscribe function — call it when the SSE client disconnects.
 */
export function subscribeToJob(
  jobId: string,
  callback: (event: JobEvent) => void,
): () => void {
  let subs = subscribers.get(jobId);
  if (!subs) {
    subs = new Set();
    subscribers.set(jobId, subs);
  }
  subs.add(callback);
  return () => {
    subs!.delete(callback);
    if (subs!.size === 0) subscribers.delete(jobId);
  };
}

/** Mark a running job as cancelled. Returns false if the job is not running. */
export function cancelJob(id: string): boolean {
  const job = jobs.get(id);
  if (!job || job.status !== "running") return false;
  emitJobEvent(id, {
    type: "cancelled",
    timestamp: Date.now(),
    message: "Cancelled by user",
  });
  return true;
}

// ── Pruning ───────────────────────────────────────────────────────────────────

/** Remove oldest completed/failed/interrupted jobs beyond MAX_JOBS. */
async function pruneOldJobs(): Promise<void> {
  const all = listJobs();
  if (all.length <= MAX_JOBS) return;

  const candidates = all
    .filter(j => j.status !== "running")
    .slice(MAX_JOBS);

  for (const job of candidates) {
    jobs.delete(job.id);
    try {
      await unlink(join(DATA_DIR, `${job.id}.json`));
    } catch {}
  }
}
