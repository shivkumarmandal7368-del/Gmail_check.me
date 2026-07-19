import dns from "dns/promises";
import net from "net";

export type EmailStatus = "valid" | "invalid" | "catch_all" | "unknown";

export interface EmailResult {
  email: string;
  status: EmailStatus;
  reason: string;
  isGmail: boolean;
  smtpCode: number | null;
}

const EMAIL_REGEX = /^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$/;
const GMAIL_DOMAINS = new Set(["gmail.com", "googlemail.com"]);
const SMTP_TIMEOUT_MS = 10000;
const FROM_ADDRESS = "check@verify.local";

function parseEmail(email: string): { local: string; domain: string } | null {
  const trimmed = email.trim().toLowerCase();
  if (!EMAIL_REGEX.test(trimmed)) return null;
  const at = trimmed.lastIndexOf("@");
  return { local: trimmed.slice(0, at), domain: trimmed.slice(at + 1) };
}

async function getMxRecord(domain: string): Promise<string | null> {
  try {
    const records = await dns.resolveMx(domain);
    if (!records || records.length === 0) return null;
    records.sort((a, b) => a.priority - b.priority);
    return records[0].exchange;
  } catch {
    return null;
  }
}

function smtpConversation(
  host: string,
  email: string,
): Promise<{ code: number; message: string }> {
  return new Promise((resolve, reject) => {
    const sock = new net.Socket();
    let data = "";
    let stage = 0;

    const done = (code: number, message: string) => {
      sock.destroy();
      resolve({ code, message });
    };

    const fail = (err: string) => {
      sock.destroy();
      reject(new Error(err));
    };

    sock.setTimeout(SMTP_TIMEOUT_MS);
    sock.on("timeout", () => fail("SMTP timeout"));
    sock.on("error", (e) => fail(e.message));

    sock.on("data", (chunk) => {
      data += chunk.toString();
      const lines = data.split("\r\n");
      data = lines.pop() ?? "";

      for (const line of lines) {
        if (!line) continue;
        const code = parseInt(line.slice(0, 3), 10);
        const continued = line[3] === "-";
        if (continued) continue; // multi-line response, wait for last line

        if (stage === 0 && code === 220) {
          // Got greeting
          stage = 1;
          sock.write(`EHLO verify.local\r\n`);
        } else if (stage === 1 && (code === 250 || code === 220)) {
          // EHLO accepted
          stage = 2;
          sock.write(`MAIL FROM:<${FROM_ADDRESS}>\r\n`);
        } else if (stage === 2 && code === 250) {
          // MAIL FROM accepted
          stage = 3;
          sock.write(`RCPT TO:<${email}>\r\n`);
        } else if (stage === 3) {
          // RCPT TO response — this is the verdict
          done(code, line.slice(4));
        } else if (code >= 400) {
          done(code, line.slice(4));
        }
      }
    });

    sock.connect(25, host);
  });
}

async function verifySingleEmail(email: string): Promise<EmailResult> {
  const parsed = parseEmail(email);
  if (!parsed) {
    return {
      email,
      status: "invalid",
      reason: "Invalid email format",
      isGmail: false,
      smtpCode: null,
    };
  }

  const { local: _local, domain } = parsed;
  const isGmail = GMAIL_DOMAINS.has(domain);

  // MX lookup
  const mx = await getMxRecord(domain);
  if (!mx) {
    return {
      email,
      status: "invalid",
      reason: "No MX records found for domain",
      isGmail,
      smtpCode: null,
    };
  }

  // SMTP check
  try {
    const { code, message } = await smtpConversation(mx, email);

    if (code === 250 || code === 251) {
      // Check for catch-all: try a random address on same domain
      const randomEmail = `no-reply-verify-xqz9k2@${domain}`;
      try {
        const { code: catchCode } = await smtpConversation(mx, randomEmail);
        if (catchCode === 250 || catchCode === 251) {
          return {
            email,
            status: "catch_all",
            reason: "Domain accepts all addresses (catch-all)",
            isGmail,
            smtpCode: code,
          };
        }
      } catch {
        // ignore catch-all check error
      }
      return {
        email,
        status: "valid",
        reason: "Mailbox exists",
        isGmail,
        smtpCode: code,
      };
    } else if (code === 550 || code === 551 || code === 552 || code === 553 || code === 554) {
      return {
        email,
        status: "invalid",
        reason: message || "Mailbox does not exist",
        isGmail,
        smtpCode: code,
      };
    } else if (code === 421 || code === 450 || code === 451 || code === 452) {
      return {
        email,
        status: "unknown",
        reason: "Server temporarily unavailable or rate-limited",
        isGmail,
        smtpCode: code,
      };
    } else {
      return {
        email,
        status: "unknown",
        reason: `Unexpected SMTP response: ${code} ${message}`,
        isGmail,
        smtpCode: code,
      };
    }
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    // Gmail and many providers block port 25 from cloud IPs
    // If connection refused / timeout, mark as unknown
    return {
      email,
      status: "unknown",
      reason: `SMTP check failed: ${msg}`,
      isGmail,
      smtpCode: null,
    };
  }
}

export async function verifyEmails(emails: string[]): Promise<EmailResult[]> {
  // Process in batches to avoid overwhelming the network
  const BATCH_SIZE = 5;
  const results: EmailResult[] = [];

  for (let i = 0; i < emails.length; i += BATCH_SIZE) {
    const batch = emails.slice(i, i + BATCH_SIZE);
    const batchResults = await Promise.all(
      batch.map((email) => verifySingleEmail(email.trim()).catch(() => ({
        email: email.trim(),
        status: "unknown" as EmailStatus,
        reason: "Verification error",
        isGmail: false,
        smtpCode: null,
      })))
    );
    results.push(...batchResults);
  }

  return results;
}
