import tls from "tls";
import { generateTOTP, totpSecondsRemaining } from "./totp.js";

export type LoginStatus =
  | "accessible"
  | "verification_required"
  | "wrong_password"
  | "app_password_required"
  | "unknown";

export interface LoginResult {
  email: string;
  status: LoginStatus;
  reason: string;
  totpCode: string | null;      // generated TOTP code (if secret was provided)
  totpSecondsLeft: number | null; // seconds until code expires
}

const IMAP_TIMEOUT_MS = 15000;
const IMAP_HOST = "imap.gmail.com";
const IMAP_PORT = 993;

function imapLogin(
  email: string,
  password: string,
): Promise<{ code: "ok" | "no" | "bad" | "bye" | "timeout"; line: string }> {
  return new Promise((resolve) => {
    let resolved = false;

    const done = (
      code: "ok" | "no" | "bad" | "bye" | "timeout",
      line: string,
    ) => {
      if (resolved) return;
      resolved = true;
      clearTimeout(timer);
      try { socket.destroy(); } catch {}
      resolve({ code, line });
    };

    const timer = setTimeout(() => done("timeout", "Connection timed out"), IMAP_TIMEOUT_MS);

    const socket = tls.connect(
      IMAP_PORT,
      IMAP_HOST,
      { servername: IMAP_HOST, rejectUnauthorized: true },
    );

    let buffer = "";
    let sentLogin = false;

    socket.on("error", (err) => done("no", `Connection error: ${err.message}`));

    socket.on("data", (chunk) => {
      buffer += chunk.toString();
      const lines = buffer.split("\r\n");
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (!line) continue;

        if (!sentLogin && line.startsWith("* OK")) {
          sentLogin = true;
          const escaped = password.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
          socket.write(`A001 LOGIN "${email}" "${escaped}"\r\n`);
          continue;
        }
        if (!sentLogin && line.startsWith("* BYE")) {
          done("bye", "Server rejected connection immediately");
          return;
        }

        if (line.startsWith("A001 OK")) { done("ok", line); return; }
        if (line.startsWith("A001 NO")) { done("no", line); return; }
        if (line.startsWith("A001 BAD")) { done("bad", line); return; }
      }
    });
  });
}

async function checkOneAccount(
  email: string,
  password: string,
  totpSecret?: string,
): Promise<LoginResult> {
  // Generate TOTP if secret provided
  let totpCode: string | null = null;
  let totpSecondsLeft: number | null = null;

  if (totpSecret) {
    try {
      totpCode = generateTOTP(totpSecret);
      totpSecondsLeft = totpSecondsRemaining();
    } catch {
      totpCode = null;
    }
  }

  // First attempt: plain password
  let { code, line } = await imapLogin(email, password);

  // If failed and we have a TOTP code, retry with password+totpCode appended
  // (some IMAP servers accept this format, and it confirms the TOTP is valid)
  if (code !== "ok" && totpCode) {
    const combined = await imapLogin(email, password + totpCode);
    if (combined.code === "ok") {
      code = combined.code;
      line = combined.line;
    }
  }

  if (code === "ok") {
    return {
      email,
      status: "accessible",
      reason: "Login successful — mailbox is accessible",
      totpCode,
      totpSecondsLeft,
    };
  }

  if (code === "timeout") {
    return {
      email,
      status: "unknown",
      reason: "Connection timed out",
      totpCode,
      totpSecondsLeft,
    };
  }

  const lower = line.toLowerCase();

  if (
    lower.includes("application-specific password") ||
    lower.includes("app-specific") ||
    lower.includes("appspecific")
  ) {
    return {
      email,
      status: "app_password_required",
      reason: totpCode
        ? `2FA is on — generated TOTP: ${totpCode} (use App Password for IMAP)`
        : "2-step verification is on — use an App Password or provide TOTP secret",
      totpCode,
      totpSecondsLeft,
    };
  }

  if (
    lower.includes("web browser") ||
    lower.includes("weblogin") ||
    lower.includes("please log in via") ||
    lower.includes("sign in via browser")
  ) {
    return {
      email,
      status: "verification_required",
      reason: totpCode
        ? `Browser verification needed — generated TOTP: ${totpCode}`
        : "Account requires browser verification to continue",
      totpCode,
      totpSecondsLeft,
    };
  }

  if (
    lower.includes("authenticationfailed") ||
    lower.includes("invalid credentials") ||
    lower.includes("incorrect password") ||
    lower.includes("username and password") ||
    lower.includes("bad credentials")
  ) {
    return {
      email,
      status: "wrong_password",
      reason: "Wrong password — credentials are invalid",
      totpCode,
      totpSecondsLeft,
    };
  }

  const msg = line.replace(/^A001 (NO|BAD)\s*/i, "") || line;
  return {
    email,
    status: "wrong_password",
    reason: msg,
    totpCode,
    totpSecondsLeft,
  };
}

export async function checkGmailLogins(
  credentials: Array<{ email: string; password: string; totp?: string }>,
): Promise<LoginResult[]> {
  const BATCH_SIZE = 3;
  const results: LoginResult[] = [];

  for (let i = 0; i < credentials.length; i += BATCH_SIZE) {
    const batch = credentials.slice(i, i + BATCH_SIZE);
    const batchResults = await Promise.all(
      batch.map(({ email, password, totp }) =>
        checkOneAccount(email, password, totp).catch(() => ({
          email,
          status: "unknown" as LoginStatus,
          reason: "Check failed unexpectedly",
          totpCode: null,
          totpSecondsLeft: null,
        })),
      ),
    );
    results.push(...batchResults);
  }

  return results;
}
