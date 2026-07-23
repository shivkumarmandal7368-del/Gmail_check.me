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

export type BrowserResultCategory = "open" | "not_open" | "delete" | "unknown";

export interface IpInfo {
  ip?: string;
  city?: string;
  district?: string;
  zip?: string;
  region?: string;
  country?: string;
  continent?: string;
  continentCode?: string;
  countryCode?: string;
  isp?: string;
  org?: string;
  as?: string;
  asname?: string;
  reverse?: string;
  currency?: string;
  offset?: number;
  mobile?: boolean;
  proxy?: boolean;
  hosting?: boolean;
}

export interface BrowserLoginResult {
  email: string;
  status: BrowserLoginStatus;
  category: BrowserResultCategory;
  reason: string;
  totpCode: string | null;
  debugScreenshot?: string;
  exitIp?: string;
  ipInfo?: IpInfo;
  fingerprint?: string;
  proxySession?: string;   // unique sticky-session ID → proof of different IP per account
  durationMs?: number;     // how long this account took end-to-end (ms)
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

/**
 * Inject a unique sticky-session ID into a rotating residential proxy URL.
 *
 * Most residential proxy providers (ProxyScrape, Bright Data, Oxylabs, etc.)
 * support sticky sessions by appending  -session-<ID>  to the username.
 * Same ID = same exit IP for the entire Chrome session.
 * Different ID per account = different IPs, but each account stays on ONE IP.
 *
 * Example:
 *   http://user:pass@rp.scrapegw.com:6060
 *   → http://user-session-a3f9k2:pass@rp.scrapegw.com:6060
 */
function injectStickySession(proxyUrl: string, sessionId: string): string {
  try {
    const u = new URL(proxyUrl);
    if (u.username) {
      u.username = `${u.username}-session-${sessionId}`;
      return u.toString();
    }
  } catch {}
  // Fallback: plain  host:port:user:pass  or unrecognised format — return as-is
  return proxyUrl;
}

function randomSessionId(): string {
  return Math.random().toString(36).slice(2, 10); // e.g. "a3f9k2xb"
}

async function checkOneAccount(
  email: string,
  password: string,
  totpSecret?: string,
  proxy?: string,           // sticky-session URL — used by Chrome
  freshProfile = false,
  proxyForIpCheck?: string, // original URL (no sticky suffix) — used for pre-flight IP fetch via requests
): Promise<BrowserLoginResult> {
  let totpCode: string | null = null;
  if (totpSecret) {
    try { totpCode = generateTOTP(totpSecret); } catch {}
  }

  const python = getPython3();
  const input = JSON.stringify({
    email,
    password,
    totp: totpSecret ?? null,
    proxy: proxy ?? null,
    proxyForIpCheck: proxyForIpCheck ?? proxy ?? null,
    freshProfile,
  });

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
        category: "unknown",
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
          category: (parsed.category as BrowserResultCategory) ?? "unknown",
          reason: parsed.reason ?? "No reason returned",
          totpCode: parsed.totpCode ?? totpCode,
          debugScreenshot: parsed.debugScreenshot ?? undefined,
          exitIp: parsed.exitIp ?? undefined,
          ipInfo: parsed.ipInfo ?? undefined,
          fingerprint: parsed.fingerprint ?? undefined,
          durationMs: typeof parsed.durationMs === "number" ? parsed.durationMs : undefined,
        });
      } catch {
        const snippet = (stderr || stdout).slice(-400);
        resolve({
          email,
          status: "unknown",
          category: "unknown",
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
        category: "unknown",
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
  onAccountComplete?: (result: BrowserLoginResult) => void,
  proxies?: string[],          // rotation list — one proxy per account (round-robin)
  freshProfile = false,        // wipe Chrome profile + fingerprint before each check
  onAccountStart?: (email: string) => void,  // fires just before Python spawns (for SSE "checking" badge)
  signal?: AbortSignal,        // abort signal — checked before each new account starts
): Promise<BrowserLoginResult[]> {
  // Proxy selection: rotation list takes priority over single proxy
  const getProxy = (idx: number): string | undefined => {
    if (proxies && proxies.length > 0) return proxies[idx % proxies.length];
    return proxy;
  };

  const tasks = credentials.map(
    (cred, idx) => async () => {
      // Check abort signal before starting a new account.
      // Accounts already in-flight will run to completion.
      if (signal?.aborted) {
        const result: BrowserLoginResult = {
          email: cred.email,
          status: "unknown",
          category: "unknown",
          reason: "Job cancelled by user",
          totpCode: null,
        };
        onAccountComplete?.(result);
        return result;
      }

      const baseProxy = getProxy(idx);
      // Inject a unique sticky-session ID so the entire Chrome login for this
      // account uses ONE fixed exit IP. Without this, a rotating proxy changes
      // IP mid-session (between page loads) which Google flags as suspicious.
      const sessionId = randomSessionId();
      const assignedProxy = baseProxy ? injectStickySession(baseProxy, sessionId) : undefined;
      console.log(`[BROWSER] ${cred.email} → proxy slot ${proxies && proxies.length > 0 ? (idx % proxies.length) + 1 : "single"} | session=${sessionId} | fresh=${freshProfile}`);
      // Notify frontend that this account is now actively being checked
      onAccountStart?.(cred.email);
      const result = await checkOneAccount(cred.email, cred.password, cred.totp, assignedProxy, freshProfile, baseProxy).catch(
        (err: unknown) => ({
          email: cred.email,
          status: "unknown" as BrowserLoginStatus,
          category: "unknown" as BrowserResultCategory,
          reason: `Browser check failed: ${err instanceof Error ? err.message.slice(0, 200) : String(err).slice(0, 200)}`,
          totpCode: null,
        }),
      );
      // Attach session ID so UI can show proof of per-account IP
      const enriched = { ...result, proxySession: assignedProxy ? sessionId : undefined };
      onAccountComplete?.(enriched);
      return enriched;
    },
  );
  return runWithConcurrency(tasks, concurrency);
}
