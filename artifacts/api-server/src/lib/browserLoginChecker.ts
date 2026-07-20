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
  debugScreenshot?: string;
}

const BROWSER_TIMEOUT = 30000;

function getChromiumPath(): string {
  const candidates = [
    () => execSync("which chromium",         { encoding: "utf8" }).trim(),
    () => execSync("which chromium-browser", { encoding: "utf8" }).trim(),
    () => execSync("which google-chrome",    { encoding: "utf8" }).trim(),
  ];
  for (const fn of candidates) {
    try { const p = fn(); if (p) return p; } catch {}
  }
  const termuxPaths = [
    "/data/data/com.termux/files/usr/bin/chromium",
    "/data/data/com.termux/files/usr/bin/chromium-browser",
  ];
  for (const p of termuxPaths) {
    try { execSync(`test -f "${p}"`); return p; } catch {}
  }
  return "/nix/store/qa9cnw4v5xkxyip6mb9kxqfq1z4x2dx1-chromium-138.0.7204.100/bin/chromium";
}

async function sleep(ms: number) {
  return new Promise<void>((r) => setTimeout(r, ms));
}

function rand(min: number, max: number) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function parseProxy(proxy: string) {
  try {
    const url = new URL(proxy);
    return {
      server: `${url.protocol}//${url.host}`,
      username: url.username ? decodeURIComponent(url.username) : undefined,
      password: url.password ? decodeURIComponent(url.password) : undefined,
    };
  } catch {
    return { server: proxy };
  }
}

const isAndroid =
  process.platform === "linux" &&
  (process.env.TERMUX_VERSION !== undefined ||
    !!process.env.PREFIX?.includes("com.termux") ||
    (() => { try { return require("fs").existsSync("/data/data/com.termux"); } catch { return false; } })());

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
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-web-security",
    "--allow-running-insecure-content",
    "--disable-notifications",
    "--disable-popup-blocking",
    "--enable-features=NetworkService,NetworkServiceInProcess",
    "--metrics-recording-only",
    "--use-mock-keychain",
    "--window-size=1280,800",
    "--lang=en-US,en",
    // Android: no-zygote removed — it breaks networking on many devices
    ...(isAndroid ? ["--disable-features=VizDisplayCompositor"] : []),
  ];

  if (proxyParsed) launchArgs.push(`--proxy-server=${proxyParsed.server}`);

  const browser = await puppeteerExtra.launch({
    executablePath: getChromiumPath(),
    headless: true,
    args: launchArgs,
    defaultViewport: { width: 1280, height: 800, deviceScaleFactor: 1 },
    timeout: BROWSER_TIMEOUT,
    ignoreHTTPSErrors: true,
  });

  const page = await browser.newPage();

  if (proxyParsed?.username && proxyParsed?.password) {
    await page.authenticate({ username: proxyParsed.username, password: proxyParsed.password });
  }

  await page.setUserAgent(
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
  );

  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, "webdriver",          { get: () => undefined });
    Object.defineProperty(navigator, "languages",          { get: () => ["en-US", "en"] });
    Object.defineProperty(navigator, "language",           { get: () => "en-US" });
    Object.defineProperty(navigator, "platform",           { get: () => "Win32" });
    Object.defineProperty(navigator, "hardwareConcurrency",{ get: () => 8 });
    Object.defineProperty(navigator, "deviceMemory",       { get: () => 8 });
    (window as any).chrome = { runtime: {} };
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
      if (p === 37445) return "Intel Inc.";
      if (p === 37446) return "Intel Iris OpenGL Engine";
      return getParam.call(this, p);
    };
  });

  await page.setExtraHTTPHeaders({
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  });

  // ── Helpers ──────────────────────────────────────────────────────

  async function pageState() {
    const url = page.url();
    const text = (await page.evaluate(() => document.body?.innerText ?? "").catch(() => "")).toLowerCase();
    return { url, text };
  }

  async function classify(url: string, text: string): Promise<BrowserLoginResult | null> {
    if (
      url.includes("mail.google.com") ||
      url.includes("gmail.com/mail") ||
      text.includes("inbox") ||
      text.includes("compose") ||
      text.includes("primary") ||
      (await page.$('[gh="cm"],[data-tooltip="Compose"],[aria-label="Compose"]').catch(() => null))
    ) {
      return { email, status: "opened", reason: "Mailbox opened successfully ✅", totpCode };
    }
    if (
      text.includes("couldn't find your google account") ||
      text.includes("no account found") ||
      text.includes("find your google account")
    ) {
      return { email, status: "wrong_password", reason: "Google account not found", totpCode };
    }
    if (
      text.includes("wrong password") ||
      text.includes("didn't recognize") ||
      text.includes("password you entered") ||
      text.includes("incorrect password") ||
      text.includes("that password is incorrect") ||
      url.includes("WrongPassword") || url.includes("wrongpassword")
    ) {
      return { email, status: "wrong_password", reason: "Wrong password", totpCode };
    }
    if (
      text.includes("wrong code") ||
      text.includes("that code didn't work") ||
      text.includes("code is incorrect") ||
      text.includes("enter the code again")
    ) {
      return {
        email, status: "wrong_password",
        reason: totpCode ? `TOTP code ${totpCode} was wrong or expired` : "Wrong 2FA code",
        totpCode,
      };
    }
    if (
      text.includes("verify your identity") || text.includes("verify it's you") ||
      text.includes("choose a way to verify") || text.includes("confirm it's you") ||
      text.includes("unusual activity") || text.includes("suspicious activity") ||
      text.includes("protect your account") ||
      url.includes("challenge") || url.includes("InterstitialConfirmation") ||
      (url.includes("verify") && !url.includes("mail"))
    ) {
      return { email, status: "verification_required", reason: "Google is asking for phone/device verification", totpCode };
    }
    return null;
  }

  // ── Click a button reliably ────────────────────────────────────
  async function clickButton(selector: string) {
    // 1. JS click
    await page.evaluate((sel) => {
      const el = document.querySelector(sel) as HTMLElement | null;
      if (el) el.click();
    }, selector).catch(() => {});
    await sleep(200);
    if (!page.url().includes("identifier") && !page.url().includes("pwd")) return;

    // 2. Puppeteer element click with bounding box
    try {
      const el = await page.$(selector);
      if (el) {
        const box = await el.boundingBox();
        if (box && box.width > 0) {
          await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
          await sleep(200);
        }
      }
    } catch {}

    // 3. Touchscreen tap (ARM Android)
    try {
      const coords = await page.$eval(selector, (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 ? { x: r.left + r.width / 2, y: r.top + r.height / 2 } : null;
      }).catch(() => null);
      if (coords) await page.touchscreen.tap(coords.x, coords.y).catch(() => {});
    } catch {}
  }

  try {
    // ── Step 1: Open Google login ──────────────────────────────
    const googleUrl =
      "https://accounts.google.com/v3/signin/identifier?service=mail&flowName=GlifWebSignIn&flowEntry=ServiceLogin";
    console.log(`[BROWSER] ${email} — Step 1: goto...`);

    try {
      await page.goto(googleUrl, { waitUntil: "domcontentloaded", timeout: BROWSER_TIMEOUT });
    } catch (e: any) {
      if (e?.message?.includes("ERR_") || e?.message?.includes("net::")) {
        console.log(`[BROWSER] ${email} — network error, retrying in 3s...`);
        await sleep(3000);
        await page.goto(googleUrl, { waitUntil: "domcontentloaded", timeout: BROWSER_TIMEOUT });
      } else throw e;
    }
    console.log(`[BROWSER] ${email} — Step 1 done. url=${page.url().slice(0, 55)}`);
    await sleep(300);

    // ── Step 2: Enter email ────────────────────────────────────
    console.log(`[BROWSER] ${email} — Step 2: typing email...`);
    await page.waitForSelector("#identifierId", { timeout: 15000 });
    await page.click("#identifierId");
    await sleep(100);
    // Use page.type — fires proper keyboard events Google listens for
    await page.type("#identifierId", email, { delay: isAndroid ? 25 : 40 });
    await sleep(500);

    // ── Step 2b: Submit email ──────────────────────────────────
    console.log(`[BROWSER] ${email} — Step 2: submitting...`);
    const nav1 = page.waitForNavigation({ timeout: 10000, waitUntil: "domcontentloaded" }).catch(() => null);
    await page.keyboard.press("Enter");
    await sleep(300);
    await clickButton("#identifierNext");
    await nav1;
    await sleep(300);
    console.log(`[BROWSER] ${email} — Step 2 done. url=${page.url().slice(0, 55)}`);

    // Check result after email step
    {
      const { url, text } = await pageState();
      if (url.includes("signin/rejected")) {
        return {
          email, status: "verification_required",
          reason: "Google rejected sign-in — headless browser detected or account needs phone verification. Try with a residential proxy or use IMAP Check instead.",
          totpCode,
        };
      }
      const early = await classify(url, text);
      if (early) return early;
    }

    // ── Step 3: Password ───────────────────────────────────────
    console.log(`[BROWSER] ${email} — Step 3: waiting for password field...`);
    const pwSelector = 'input[name="Passwd"],input[type="password"]:not([name="hiddenPassword"])';
    const pwFound = await page.waitForSelector(pwSelector, { timeout: 10000 }).catch(() => null);
    console.log(`[BROWSER] ${email} — Step 3: pwFound=${!!pwFound} url=${page.url().slice(0, 55)}`);

    if (!pwFound) {
      const { url, text } = await pageState();
      const classified = await classify(url, text);
      if (classified) return classified;
      let debugScreenshot: string | undefined;
      try {
        const buf = await (page as any).screenshot({ type: "png", fullPage: false });
        debugScreenshot = `data:image/png;base64,${Buffer.from(buf).toString("base64")}`;
      } catch {}
      return {
        email, status: "verification_required",
        reason: `Password field not found — page: ${url.slice(0, 80)}`,
        totpCode, debugScreenshot,
      };
    }

    await page.type(pwSelector, password, { delay: isAndroid ? 25 : 40 });
    await sleep(200);

    const nav2 = page.waitForNavigation({ timeout: 12000, waitUntil: "domcontentloaded" }).catch(() => null);
    await page.keyboard.press("Enter");
    await sleep(200);
    await clickButton("#passwordNext");
    await nav2;
    await sleep(400);

    let { url, text } = await pageState();
    console.log(`[BROWSER] ${email} — Step 3 done. url=${url.slice(0, 55)}`);

    {
      const classified = await classify(url, text);
      if (classified) return classified;
    }

    // ── Step 4: 2FA / TOTP ─────────────────────────────────────
    const totpInput = await page.$('input[name="totpPin"],input[name="Pin"],input[id="totpPin"]').catch(() => null);
    const is2fa =
      totpInput !== null ||
      text.includes("2-step verification") ||
      text.includes("authenticator app") ||
      text.includes("enter the code") ||
      text.includes("verification code");

    if (is2fa) {
      if (!totpCode) {
        return { email, status: "2fa_required", reason: "2FA required — provide TOTP secret", totpCode };
      }
      const codeInput = totpInput || await page.$('input[type="tel"]').catch(() => null);
      if (codeInput) {
        await codeInput.click();
        await sleep(100);
        await codeInput.type(totpCode, { delay: 25 });
        await sleep(150);
        const nav3 = page.waitForNavigation({ timeout: 10000, waitUntil: "domcontentloaded" }).catch(() => null);
        await page.keyboard.press("Enter");
        await sleep(200);
        await clickButton('#totpNext,[jsname="LgbsSe"],button[type="submit"]');
        await nav3;
        await sleep(300);
        ({ url, text } = await pageState());
      }
    }

    // ── Final ──────────────────────────────────────────────────
    const final = await classify(url, text);
    if (final) return final;

    return { email, status: "unknown", reason: `Unexpected page: ${url.slice(0, 80)}`, totpCode };

  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    console.log(`[BROWSER] ${email} — ERROR: ${msg.slice(0, 100)}`);
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
  for (const cred of credentials) {
    const result = await checkOneAccount(cred.email, cred.password, cred.totp, proxy).catch((err: unknown) => ({
      email: cred.email,
      status: "unknown" as BrowserLoginStatus,
      reason: `Browser check failed: ${err instanceof Error ? err.message.slice(0, 200) : String(err).slice(0, 200)}`,
      totpCode: null,
    }));
    results.push(result);
  }
  return results;
}
