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

import { createJob, emitJobEvent, getJob, cancelJob, pauseJob as markJobPaused, resumeJob as markJobResumed } from "./jobStore.js";
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
  launchJob(jobId, params, true);
  return jobId;
}

function launchJob(jobId: string, params: StartJobParams, emitStarted: boolean): void {
  const abort = new AbortController();
  abortControllers.set(jobId, abort);

  if (emitStarted) {
    // Emit "started" event — stored in the job's event log so reconnecting
    // clients see it on replay.
    emitJobEvent(jobId, {
      type: "started",
      timestamp: Date.now(),
      total: params.credentials.length,
      concurrency: params.concurrency,
    });
  }

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

/** Pause a job. In-flight accounts may finish; pending accounts are skipped. */
export function pauseJob(jobId: string): boolean {
  const ctrl = abortControllers.get(jobId);
  if (ctrl) {
    ctrl.abort();
    abortControllers.delete(jobId);
  }
  return markJobPaused(jobId);
}

/** Resume a persisted job using only credentials without a saved result. */
export function resumeJob(jobId: string): boolean {
  const job = getJob(jobId);
  if (!job || !["paused", "interrupted", "cancelled"].includes(job.status)) return false;

  const completedEmails = new Set(job.results.map(result => result.email));
  const pendingCredentials = job.credentials.filter(credential => !completedEmails.has(credential.email));

  if (!markJobResumed(jobId)) return false;

  launchJob(jobId, {
    credentials: pendingCredentials,
    proxy: job.proxy,
    proxies: job.proxies,
    concurrency: job.concurrency,
    freshProfile: job.freshProfile,
  }, false);
  return true;
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
      // An abort pauses/cancels new work, but an account already in-flight
      // can still finish and must be persisted for a correct resume point.
      // The synthetic result for accounts skipped after abort is not persisted.
      if (result.reason === "Job cancelled by user") return;
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
  if (job.status === "paused") {
    // Keep the SSE stream open until already-running browser tabs finish and
    // their results are persisted. Resume is enabled only after this marker.
    emitJobEvent(jobId, {
      type: "paused_done",
      timestamp: Date.now(),
      message: "Paused — current browser tabs finished. Ready to resume.",
    });
    return;
  }

  emitJobEvent(jobId, { type: "done", timestamp: Date.now() });
}
