import { execSync } from "child_process";
import { generateTOTP } from "./totp.js";

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
}

const BROWSER_TIMEOUT = 45000;

function getChromiumPath(): string {
  try {
    return execSync("which chromium", { encoding: "utf8" }).trim();
  } catch {
    return "/nix/store/qa9cnw4v5xkxyip6mb9kxqfq1z4x2dx1-chromium-138.0.7204.100/bin/chromium";
  }
}

async function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

function parseProxy(proxy: string): { server: string; username?: string; password?: string } {
  try {
    const url = new URL(proxy);
    // server without credentials e.g. http://host:port
    const server = `${url.protocol}//${url.host}`;
    const username = url.username ? decodeURIComponent(url.username) : undefined;
    const password = url.password ? decodeURIComponent(url.password) : undefined;
    return { server, username, password };
  } catch {
    // fallback: treat the whole string as server
    return { server: proxy };
  }
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

  const puppeteerExtra = (await import("puppeteer-extra")).default;
  const StealthPlugin = (await import("puppeteer-extra-plugin-stealth")).default;
  puppeteerExtra.use(StealthPlugin());

  const proxyParsed = proxy ? parseProxy(proxy) : null;
  const launchArgs = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--no-first-run",
    "--no-default-browser-check",
    "--window-size=1280,800",
  ];
  if (proxyParsed) {
    launchArgs.push(`--proxy-server=${proxyParsed.server}`);
  }

  const browser = await puppeteerExtra.launch({
    executablePath: getChromiumPath(),
    headless: true,
    args: launchArgs,
    defaultViewport: { width: 1280, height: 800 },
    timeout: BROWSER_TIMEOUT,
  });

  const page = await browser.newPage();

  // Authenticate with proxy if credentials provided
  if (proxyParsed?.username && proxyParsed?.password) {
    await page.authenticate({ username: proxyParsed.username, password: proxyParsed.password });
  }

  await page.setUserAgent(
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
  );

  try {
    // ── Step 1: Open Gmail login ──────────────────────────────────
    await page.goto(
      "https://accounts.google.com/v3/signin/identifier?service=mail&flowName=GlifWebSignIn&flowEntry=ServiceLogin",
      { waitUntil: "networkidle2", timeout: BROWSER_TIMEOUT }
    );

    // ── Step 2: Enter email ───────────────────────────────────────
    // Correct selector: id="identifierId", type="text"
    await page.waitForSelector("#identifierId", { timeout: 15000 });
    await sleep(400);
    await page.click("#identifierId");
    await page.type("#identifierId", email, { delay: 60 });
    await sleep(400);

    await Promise.all([
      page.waitForNavigation({ timeout: 20000, waitUntil: "networkidle2" }),
      page.click("#identifierNext"),
    ]);
    await sleep(1200);

    // Check for "couldn't find your Google Account"
    const bodyText = (await page.evaluate(() => document.body?.innerText ?? "")).toLowerCase();
    if (
      bodyText.includes("couldn't find your google account") ||
      bodyText.includes("no account found") ||
      bodyText.includes("find your google account")
    ) {
      return { email, status: "wrong_password", reason: "Google account not found", totpCode };
    }

    // ── Step 3: Enter password ────────────────────────────────────
    // Password field: input[name="Passwd"] or input[type="password"]
    const pwSelector = 'input[name="Passwd"], input[type="password"]:not([name="hiddenPassword"])';
    await page.waitForSelector(pwSelector, { timeout: 15000 });
    await sleep(400);
    await page.click(pwSelector);
    await page.type(pwSelector, password, { delay: 70 });
    await sleep(400);

    await Promise.all([
      page.waitForNavigation({ timeout: 25000, waitUntil: "networkidle2" }),
      page.click("#passwordNext"),
    ]);
    await sleep(1500);

    let url = page.url();
    let text = (await page.evaluate(() => document.body?.innerText ?? "")).toLowerCase();

    // ── Wrong password ────────────────────────────────────────────
    if (
      text.includes("wrong password") ||
      text.includes("didn't recognize") ||
      text.includes("password you entered") ||
      text.includes("incorrect password") ||
      text.includes("that password is incorrect") ||
      url.includes("WrongPassword") ||
      url.includes("wrongpassword")
    ) {
      return { email, status: "wrong_password", reason: "Wrong password — credentials are invalid", totpCode };
    }

    // ── 2FA code required ─────────────────────────────────────────
    const is2fa =
      text.includes("2-step verification") ||
      text.includes("authenticator app") ||
      text.includes("enter the code") ||
      text.includes("verification code") ||
      (await page.$('input[name="totpPin"], input[name="Pin"], input[id="totpPin"]') !== null);

    if (is2fa) {
      if (totpCode) {
        const codeInput = await page.$('input[name="totpPin"], input[name="Pin"], input[id="totpPin"], input[type="tel"]');
        if (codeInput) {
          await codeInput.click();
          await codeInput.type(totpCode, { delay: 80 });
          await sleep(300);
          try {
            await Promise.all([
              page.waitForNavigation({ timeout: 15000, waitUntil: "networkidle2" }),
              page.click('#totpNext, [jsname="LgbsSe"], button[type="submit"]'),
            ]);
          } catch { /* navigation may not happen if wrong code */ }
          await sleep(1500);
          url = page.url();
          text = (await page.evaluate(() => document.body?.innerText ?? "")).toLowerCase();
        }
      } else {
        return { email, status: "2fa_required", reason: "2FA code required — provide TOTP secret", totpCode };
      }
    }

    // ── Verification / security challenge ─────────────────────────
    if (
      text.includes("verify your identity") ||
      text.includes("verify your info") ||
      text.includes("verify it's you") ||
      text.includes("choose a way to verify") ||
      (text.includes("verify") && text.includes("phone")) ||
      text.includes("device check") ||
      text.includes("confirm it's you") ||
      url.includes("challenge") ||
      (url.includes("verify") && !url.includes("mail"))
    ) {
      return {
        email,
        status: "verification_required",
        reason: "Google is asking for phone/device verification",
        totpCode,
      };
    }

    // ── Wrong 2FA code ────────────────────────────────────────────
    if (
      text.includes("wrong code") ||
      text.includes("that code didn't work") ||
      text.includes("code is incorrect") ||
      text.includes("enter the code again")
    ) {
      return {
        email,
        status: "wrong_password",
        reason: totpCode ? `TOTP code ${totpCode} was wrong or expired` : "Wrong 2FA code",
        totpCode,
      };
    }

    // ── Mailbox opened ────────────────────────────────────────────
    if (
      url.includes("mail.google.com") ||
      url.includes("gmail.com/mail") ||
      text.includes("inbox") ||
      text.includes("compose") ||
      text.includes("primary") ||
      (await page.$('[gh="cm"], [data-tooltip="Compose"], [aria-label="Compose"]') !== null)
    ) {
      return { email, status: "opened", reason: "Mailbox opened successfully ✅", totpCode };
    }

    // ── Fallback: return what page says ──────────────────────────
    return {
      email,
      status: "unknown",
      reason: `Unexpected page (${url.slice(0, 60)})`,
      totpCode,
    };

  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    return { email, status: "unknown", reason: `Browser error: ${msg.slice(0, 120)}`, totpCode };
  } finally {
    await browser.close().catch(() => {});
  }
}

export async function browserLoginCheck(
  credentials: Array<{ email: string; password: string; totp?: string }>,
  proxy?: string,
): Promise<BrowserLoginResult[]> {
  const results: BrowserLoginResult[] = [];
  // One at a time to avoid memory pressure on Replit
  for (const cred of credentials) {
    const result = await checkOneAccount(cred.email, cred.password, cred.totp, proxy).catch(() => ({
      email: cred.email,
      status: "unknown" as BrowserLoginStatus,
      reason: "Browser check failed unexpectedly",
      totpCode: null,
    }));
    results.push(result);
  }
  return results;
}
