/**
 * JobRunner — starts browser-check jobs in the background, completely
 * decoupled from any HTTP connection.
 *
 * startJob() returns a jobId immediately. The actual work (spawning Python
 * processes) continues independently in the Node.js event loop via an
 * un-awaited async call. Results stream to the JobStore in real-time.
 *
 * If a job is cancelled, any NEW accounts (not yet started) will be skipped.
 * Accounts already in-flight will run to completion (or TIMEOUT_MS).
 */

import { createJob, emitJobEvent, getJob, cancelJob } from "./jobStore.js";
import type { Job } from "./jobStore.js";
import { browserLoginCheck } from "./browserLoginChecker.js";

// In-memory abort controllers — lost on server restart, but that's OK because
// restarted jobs are marked "interrupted" by initJobStore().
const abortControllers = new Map<string, AbortController>();

// ── Public API ────────────────────────────────────────────────────────────────

export interface StartJobParams {
  credentials: Job["credentials"];
  proxy?: string;
  proxies?: string[];
  concurrency: number;
  freshProfile: boolean;
}

/**
 * Create a job and start it running in the background.
 * Returns the new job ID immediately — the caller should respond to the
 * client without waiting for completion.
 */
export function startJob(params: StartJobParams): string {
  const job = createJob(params);
  const jobId = job.id;

  const abort = new AbortController();
  abortControllers.set(jobId, abort);

  // Emit "started" event — stored in the job's event log so reconnecting
  // clients see it on replay.
  emitJobEvent(jobId, {
    type: "started",
    timestamp: Date.now(),
    total: params.credentials.length,
    concurrency: params.concurrency,
  });

  // Start running — fire-and-forget (no await)
  runBackground(jobId, params, abort.signal)
    .catch(err => {
      console.error(`[JobRunner] Job ${jobId} crashed:`, err);
      emitJobEvent(jobId, {
        type: "error",
        timestamp: Date.now(),
        message: `Job crashed unexpectedly: ${err instanceof Error ? err.message : String(err)}`,
      });
    })
    .finally(() => {
      abortControllers.delete(jobId);
    });

  return jobId;
}

/**
 * Cancel a running job. In-flight accounts may still complete (up to their
 * timeout), but no NEW accounts will be started.
 */
export function abortJob(jobId: string): boolean {
  const ctrl = abortControllers.get(jobId);
  if (ctrl) {
    ctrl.abort();
    abortControllers.delete(jobId);
  }
  return cancelJob(jobId);
}

// ── Internal ──────────────────────────────────────────────────────────────────

async function runBackground(
  jobId: string,
  params: StartJobParams,
  signal: AbortSignal,
): Promise<void> {
  const { credentials, proxy, proxies, concurrency, freshProfile } = params;

  await browserLoginCheck(
    credentials,
    proxy,
    concurrency,
    // onAccountComplete — fires as each Python process exits
    (result) => {
      if (signal.aborted) return;
      emitJobEvent(jobId, {
        type: "result",
        timestamp: Date.now(),
        ...result,
      });
    },
    proxies,
    freshProfile,
    // onAccountStart — fires just before spawning the Python process
    (email) => {
      if (signal.aborted) return;
      emitJobEvent(jobId, {
        type: "checking",
        timestamp: Date.now(),
        email,
      });
    },
    signal,
  );

  // Don't emit "done" if the job was already cancelled via abort
  const job = getJob(jobId);
  if (!job || job.status === "cancelled") return;

  emitJobEvent(jobId, { type: "done", timestamp: Date.now() });
}
