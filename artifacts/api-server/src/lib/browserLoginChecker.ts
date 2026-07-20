import { execSync, spawn } from "child_process";
import { join } from "path";
import { generateTOTP } from "./totp.js";

// Python script lives at artifacts/api-server/gmail_uc_checker.py.
// __dirname is set by esbuild banner to the directory of the built file
// (artifacts/api-server/dist/), so one level up reaches the package root.
const PYTHON_SCRIPT = join(__dirname, "..", "gmail_uc_checker.py");

// Timeout per account (undetected-chromedriver can be slow on first run due to
// chromedriver download + patching — give it 3 minutes)
const TIMEOUT_MS = 180_000;

export type BrowserLoginStatus =
  | "opened"
  | "verification_required"
  | "wrong_password"
  | "2fa_required"
  | "unknown";

export interface BrowserLoginResult {
  email: string;
  status: BrowserLoginStatus;
  reason: string;
  totpCode: string | null;
  debugScreenshot?: string;
}

function getPython3(): string {
  for (const candidate of ["python3", "python"]) {
    try {
      const p = execSync(`which ${candidate}`, { encoding: "utf8" }).trim();
      if (p) return p;
    } catch {}
  }
  throw new Error("python3 not found — add pkgs.python3 to replit.nix");
}

async function checkOneAccount(
  email: string,
  password: string,
  totpSecret?: string,
  proxy?: string,
): Promise<BrowserLoginResult> {
  let totpCode: string | null = null;
  if (totpSecret) {
    try { totpCode = generateTOTP(totpSecret); } catch {}
  }

  const python = getPython3();
  const input = JSON.stringify({ email, password, totp: totpSecret ?? null, proxy: proxy ?? null });

  console.log(`[BROWSER] ${email} — spawning Python UC checker`);

  return new Promise((resolve) => {
    const proc = spawn(python, [PYTHON_SCRIPT], {
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";

    const timer = setTimeout(() => {
      try { proc.kill("SIGKILL"); } catch {}
      resolve({
        email,
        status: "unknown",
        reason: `Browser check timed out after ${TIMEOUT_MS / 1000}s`,
        totpCode,
      });
    }, TIMEOUT_MS);

    proc.stdout.on("data", (chunk: Buffer) => {
      stdout += chunk.toString();
    });

    proc.stderr.on("data", (chunk: Buffer) => {
      const line = chunk.toString();
      stderr += line;
      // Forward Python logs to our server stdout
      process.stdout.write(line);
    });

    proc.stdin.write(input, "utf8");
    proc.stdin.end();

    proc.on("close", (code) => {
      clearTimeout(timer);

      // The Python script prints exactly one JSON line to stdout
      const lastLine = stdout.trim().split("\n").filter(Boolean).pop() ?? "";
      try {
        const parsed = JSON.parse(lastLine);
        resolve({
          email,
          status: (parsed.status as BrowserLoginStatus) ?? "unknown",
          reason: parsed.reason ?? "No reason returned",
          totpCode: parsed.totpCode ?? totpCode,
          debugScreenshot: parsed.debugScreenshot ?? undefined,
        });
      } catch {
        const snippet = (stderr || stdout).slice(-400);
        resolve({
          email,
          status: "unknown",
          reason: `Python script exited ${code} without valid JSON.\n${snippet}`,
          totpCode,
        });
      }
    });

    proc.on("error", (err) => {
      clearTimeout(timer);
      resolve({
        email,
        status: "unknown",
        reason: `Failed to spawn Python: ${err.message}`,
        totpCode,
      });
    });
  });
}

// Run tasks with limited parallelism — like an antidetect browser opening
// N tabs at once, each with its own fingerprint/session.
async function runWithConcurrency<T>(
  tasks: Array<() => Promise<T>>,
  concurrency: number,
): Promise<T[]> {
  const results = new Array<T>(tasks.length);
  let next = 0;
  async function worker() {
    while (next < tasks.length) {
      const i = next++;
      results[i] = await tasks[i]();
    }
  }
  await Promise.all(
    Array.from({ length: Math.min(concurrency, tasks.length) }, worker),
  );
  return results;
}

export async function browserLoginCheck(
  credentials: Array<{ email: string; password: string; totp?: string }>,
  proxy?: string,
  concurrency = 3,
): Promise<BrowserLoginResult[]> {
  const tasks = credentials.map(
    (cred) => () =>
      checkOneAccount(cred.email, cred.password, cred.totp, proxy).catch(
        (err: unknown) => ({
          email: cred.email,
          status: "unknown" as BrowserLoginStatus,
          reason: `Browser check failed: ${err instanceof Error ? err.message.slice(0, 200) : String(err).slice(0, 200)}`,
          totpCode: null,
        }),
      ),
  );
  return runWithConcurrency(tasks, concurrency);
}
