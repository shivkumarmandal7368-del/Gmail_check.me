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
  debugScreenshot?: string; // base64 PNG when stuck
}

const BROWSER_TIMEOUT = 30000;
const ACCOUNT_TIMEOUT = 55000; // hard kill per account

function getChromiumPath(): string {
  // Try which chromium first
  try {
    const path = execSync("which chromium", { encoding: "utf8" }).trim();
    if (path) return path;
  } catch {}

  // Try which chromium-browser
  try {
    const path = execSync("which chromium-browser", { encoding: "utf8" }).trim();
    if (path) return path;
  } catch {}

  // Try which google-chrome
  try {
    const path = execSync("which google-chrome", { encoding: "utf8" }).trim();
    if (path) return path;
  } catch {}

  // Termux-specific paths
  const termuxPaths = [
    "/data/data/com.termux/files/usr/bin/chromium",
    "/data/data/com.termux/files/usr/bin/chromium-browser",
  ];
  for (const p of termuxPaths) {
    try {
      execSync(`test -f "${p}"`, { encoding: "utf8" });
      return p;
    } catch {}
  }

  // Replit/Nix fallback
  return "/nix/store/qa9cnw4v5xkxyip6mb9kxqfq1z4x2dx1-chromium-138.0.7204.100/bin/chromium";
}

async function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

// Random int between min and max
function rand(min: number, max: number) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function parseProxy(proxy: string): { server: string; username?: string; password?: string } {
  try {
    const url = new URL(proxy);
    const server = `${url.protocol}//${url.host}`;
    const username = url.username ? decodeURIComponent(url.username) : undefined;
    const password = url.password ? decodeURIComponent(url.password) : undefined;
    return { server, username, password };
  } catch {
    return { server: proxy };
  }
}

// Realistic user agents pool
const USER_AGENTS = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
];

// Realistic screen resolutions
const RESOLUTIONS = [
  { width: 1920, height: 1080 },
  { width: 1440, height: 900 },
  { width: 1366, height: 768 },
  { width: 1536, height: 864 },
  { width: 1280, height: 800 },
];

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
  const ua = USER_AGENTS[rand(0, USER_AGENTS.length - 1)];
  const res = RESOLUTIONS[rand(0, RESOLUTIONS.length - 1)];

  // Detect if running on Android/Termux
  const isAndroid = process.platform === "linux" && (
    process.env.TERMUX_VERSION !== undefined ||
    process.env.PREFIX?.includes("com.termux") ||
    require("fs").existsSync("/data/data/com.termux")
  );

  const launchArgs = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    // Anti-detection flags
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-web-security",
    "--allow-running-insecure-content",
    "--disable-notifications",
    "--disable-popup-blocking",
    // Realistic flags
    "--enable-features=NetworkService,NetworkServiceInProcess",
    "--metrics-recording-only",
    "--use-mock-keychain",
    `--window-size=${res.width},${res.height}`,
    `--user-agent=${ua}`,
    // Language & timezone
    "--lang=en-US,en",
    "--accept-lang=en-US,en;q=0.9",
    // Android/ARM specific flags
    ...(isAndroid ? [
      "--no-zygote",
      "--disable-features=VizDisplayCompositor",
    ] : []),
  ];

  if (proxyParsed) {
    launchArgs.push(`--proxy-server=${proxyParsed.server}`);
  }

  const browser = await puppeteerExtra.launch({
    executablePath: getChromiumPath(),
    headless: true,
    args: launchArgs,
    defaultViewport: { width: res.width, height: res.height, deviceScaleFactor: 1 },
    timeout: BROWSER_TIMEOUT,
    ignoreHTTPSErrors: true,
  });

  const page = await browser.newPage();

  if (proxyParsed?.username && proxyParsed?.password) {
    await page.authenticate({ username: proxyParsed.username, password: proxyParsed.password });
  }

  // Override navigator properties to hide automation
  await page.evaluateOnNewDocument(() => {
    // Remove webdriver flag
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });

    // Override plugins to look like real browser
    Object.defineProperty(navigator, "plugins", {
      get: () => {
        const plugins = [
          { name: "Chrome PDF Plugin", filename: "internal-pdf-viewer", description: "Portable Document Format" },
          { name: "Chrome PDF Viewer", filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai", description: "" },
          { name: "Native Client", filename: "internal-nacl-plugin", description: "" },
        ];
        return Object.assign(plugins, {
          item: (i: number) => plugins[i],
          namedItem: (name: string) => plugins.find(p => p.name === name) || null,
          refresh: () => {},
          length: plugins.length,
        });
      },
    });

    // Override languages
    Object.defineProperty(navigator, "languages", { get: () => ["en-US", "en"] });
    Object.defineProperty(navigator, "language", { get: () => "en-US" });

    // Override platform
    Object.defineProperty(navigator, "platform", { get: () => "Win32" });

    // Override hardwareConcurrency
    Object.defineProperty(navigator, "hardwareConcurrency", { get: () => 8 });

    // Override deviceMemory
    Object.defineProperty(navigator, "deviceMemory", { get: () => 8 });

    // Override connection
    Object.defineProperty(navigator, "connection", {
      get: () => ({ effectiveType: "4g", rtt: 50, downlink: 10, saveData: false }),
    });

    // Fix chrome object
    (window as any).chrome = {
      app: { isInstalled: false, InstallState: { DISABLED: "disabled", INSTALLED: "installed", NOT_INSTALLED: "not_installed" }, RunningState: { CANNOT_RUN: "cannot_run", READY_TO_RUN: "ready_to_run", RUNNING: "running" } },
      runtime: {
        OnInstalledReason: { CHROME_UPDATE: "chrome_update", INSTALL: "install", SHARED_MODULE_UPDATE: "shared_module_update", UPDATE: "update" },
        OnRestartRequiredReason: { APP_UPDATE: "app_update", GC_POLICY: "gc_policy", OS_UPDATE: "os_update" },
        PlatformArch: { ARM: "arm", ARM64: "arm64", MIPS: "mips", MIPS64: "mips64", X86_32: "x86-32", X86_64: "x86-64" },
        PlatformNaclArch: { ARM: "arm", MIPS: "mips", MIPS64: "mips64", X86_32: "x86-32", X86_64: "x86-64" },
        PlatformOs: { ANDROID: "android", CROS: "cros", LINUX: "linux", MAC: "mac", OPENBSD: "openbsd", WIN: "win" },
        RequestUpdateCheckStatus: { NO_UPDATE: "no_update", THROTTLED: "throttled", UPDATE_AVAILABLE: "update_available" },
      },
    };

    // Override permission query
    const originalQuery = window.navigator.permissions?.query;
    if (originalQuery) {
      (window.navigator.permissions as any).query = (parameters: any) =>
        parameters.name === "notifications"
          ? Promise.resolve({ state: Notification.permission })
          : originalQuery(parameters);
    }

    // WebGL vendor spoofing
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (parameter) {
      if (parameter === 37445) return "Intel Inc.";
      if (parameter === 37446) return "Intel Iris OpenGL Engine";
      return getParameter.call(this, parameter);
    };
  });

  await page.setUserAgent(ua);

  // Set extra HTTP headers to look like real browser
  await page.setExtraHTTPHeaders({
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "sec-ch-ua": '"Chromium";v="138", "Google Chrome";v="138", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
  });

  // Helper: snapshot current page state
  async function pageState() {
    const url = page.url();
    const text = (await page.evaluate(() => document.body?.innerText ?? "").catch(() => "")).toLowerCase();
    return { url, text };
  }

  // Helper: fast typing (reduced delays)
  async function humanType(selector: string, text: string) {
    await page.click(selector);
    await sleep(50);
    await page.type(selector, text, { delay: isAndroid ? 20 : rand(30, 60) });
    await sleep(100);
  }

  // Helper: human-like mouse move then click
  async function humanClick(selector: string) {
    const el = await page.$(selector);
    if (!el) return;
    const box = await el.boundingBox();
    if (box) {
      // Move to a slightly random spot within the element
      await page.mouse.move(
        box.x + box.width / 2 + rand(-5, 5),
        box.y + box.height / 2 + rand(-3, 3),
        { steps: rand(5, 15) }
      );
      await sleep(rand(80, 200));
    }
    await el.click();
  }

  // Helper: classify any page state into a result (or null = not yet deterministic)
  async function classify(url: string, text: string): Promise<BrowserLoginResult | null> {
    if (
      url.includes("mail.google.com") ||
      url.includes("gmail.com/mail") ||
      text.includes("inbox") ||
      text.includes("compose") ||
      text.includes("primary") ||
      (await page.$('[gh="cm"], [data-tooltip="Compose"], [aria-label="Compose"]').catch(() => null)) !== null
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
      url.includes("WrongPassword") ||
      url.includes("wrongpassword")
    ) {
      return { email, status: "wrong_password", reason: "Wrong password — credentials are invalid", totpCode };
    }
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
    if (
      text.includes("verify your identity") ||
      text.includes("verify your info") ||
      text.includes("verify it's you") ||
      text.includes("choose a way to verify") ||
      (text.includes("verify") && text.includes("phone")) ||
      text.includes("device check") ||
      text.includes("confirm it's you") ||
      text.includes("unusual activity") ||
      text.includes("suspicious activity") ||
      text.includes("protect your account") ||
      url.includes("challenge") ||
      url.includes("InterstitialConfirmation") ||
      (url.includes("verify") && !url.includes("mail"))
    ) {
      return {
        email,
        status: "verification_required",
        reason: "Google is asking for phone/device verification",
        totpCode,
      };
    }
    return null;
  }

  try {
    // ── Step 1: Open Gmail login ──────────────────────────────────
    const googleUrl = "https://accounts.google.com/v3/signin/identifier?service=mail&flowName=GlifWebSignIn&flowEntry=ServiceLogin";
    console.log(`[BROWSER] ${email} — Step 1: Opening Google login...`);
    try {
      await page.goto(googleUrl, { waitUntil: "domcontentloaded", timeout: BROWSER_TIMEOUT });
    } catch (e: any) {
      // Retry once on network errors (ERR_SOCKET_NOT_CONNECTED etc.)
      if (e?.message?.includes("ERR_") || e?.message?.includes("net::")) {
        console.log(`[BROWSER] ${email} — Step 1 network error, retrying... (${e.message.slice(0,40)})`);
        await sleep(2000);
        await page.goto(googleUrl, { waitUntil: "domcontentloaded", timeout: BROWSER_TIMEOUT });
      } else throw e;
    }
    console.log(`[BROWSER] ${email} — Step 1 done. URL: ${page.url().slice(0,60)}`);
    await sleep(300);

    // ── Step 2: Enter email ───────────────────────────────────────
    console.log(`[BROWSER] ${email} — Step 2: Typing email...`);
    await page.waitForSelector("#identifierId", { timeout: 15000 });
    await page.evaluate((emailVal: string) => {
      const input = document.querySelector("#identifierId") as HTMLInputElement | null;
      if (!input) return;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      if (setter) setter.call(input, emailVal);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }, email);
    await sleep(600);

    console.log(`[BROWSER] ${email} — Step 2: Clicking Next...`);

    // Hard 10s timeout on entire email-submit block
    await Promise.race([
      (async () => {
        const navPromise = page.waitForNavigation({ timeout: 9000, waitUntil: "domcontentloaded" }).catch(() => null);
        await page.focus("#identifierId").catch(() => {});
        await page.keyboard.press("Enter");
        await sleep(250);
        if (page.url().includes("identifier")) {
          await page.keyboard.press("Tab");
          await page.keyboard.press("Tab");
          await page.keyboard.press("Enter");
          await sleep(200);
        }
        if (page.url().includes("identifier")) {
          await page.evaluate(() => {
            const btn = document.querySelector("#identifierNext") as HTMLElement | null;
            if (btn) btn.click();
          }).catch(() => {});
          await sleep(200);
        }
        if (page.url().includes("identifier")) {
          await Promise.race([
            page.touchscreen.tap(800, 400),
            sleep(1500),
          ]).catch(() => {});
        }
        await navPromise;
      })(),
      sleep(10000),
    ]);

    await sleep(200);
    console.log(`[BROWSER] ${email} — After email step. URL: ${page.url().slice(0,60)}`);

    // Check page after email step
    {
      const { url, text } = await pageState();
      if (url.includes("/signin/rejected") || url.includes("signin/rejected")) {
        return {
          email,
          status: "verification_required",
          reason: "Google rejected sign-in — datacenter IP detected. Use a residential proxy.",
          totpCode,
        };
      }
      const early = await classify(url, text);
      if (early) return early;
    }

    // ── Step 3: Enter password ────────────────────────────────────
    console.log(`[BROWSER] ${email} — Step 3: Waiting for password field...`);
    const pwSelector = 'input[name="Passwd"], input[type="password"]:not([name="hiddenPassword"])';
    const pwFound = await page.waitForSelector(pwSelector, { timeout: 12000 }).catch(() => null);
    console.log(`[BROWSER] ${email} — Step 3: pwFound=${!!pwFound} url=${page.url().slice(0,60)}`);

    if (!pwFound) {
      const { url, text } = await pageState();
      const classified = await classify(url, text);
      if (classified) return classified;
      // Capture screenshot so user can see what Google is showing
      let debugScreenshot: string | undefined;
      try {
        const buf = await (page as any).screenshot({ type: "png", fullPage: false });
        debugScreenshot = `data:image/png;base64,${Buffer.from(buf).toString("base64")}`;
      } catch {}
      return {
        email,
        status: "verification_required",
        reason: `Google did not show password field — page: ${url.slice(0, 80)}`,
        totpCode,
        debugScreenshot,
      };
    }

    await humanType(pwSelector, password);
    await sleep(150);

    try {
      await Promise.all([
        page.waitForNavigation({ timeout: 12000, waitUntil: "domcontentloaded" }),
        (async () => {
          await page.evaluate(() => {
            const btn = document.querySelector("#passwordNext") as HTMLElement | null;
            if (btn) { btn.focus(); btn.click(); }
          });
          await sleep(300);
          await page.keyboard.press("Enter");
        })(),
      ]);
    } catch { /* navigation may not happen */ }
    await sleep(500);

    let { url, text } = await pageState();

    {
      const classified = await classify(url, text);
      if (classified) return classified;
    }

    // ── Step 4: 2FA / TOTP ────────────────────────────────────────
    const is2fa =
      text.includes("2-step verification") ||
      text.includes("authenticator app") ||
      text.includes("enter the code") ||
      text.includes("verification code") ||
      (await page.$('input[name="totpPin"], input[name="Pin"], input[id="totpPin"]').catch(() => null)) !== null;

    if (is2fa) {
      if (totpCode) {
        const codeInput = await page.$('input[name="totpPin"], input[name="Pin"], input[id="totpPin"], input[type="tel"]').catch(() => null);
        if (codeInput) {
          await codeInput.click();
          await sleep(100);
          await codeInput.type(totpCode, { delay: 20 });
          await sleep(150);
          try {
            await Promise.all([
              page.waitForNavigation({ timeout: 10000, waitUntil: "domcontentloaded" }),
              (async () => {
                await page.evaluate(() => {
                  const btn = document.querySelector('#totpNext, [jsname="LgbsSe"], button[type="submit"]') as HTMLElement | null;
                  if (btn) btn.click();
                });
                await sleep(200);
                await page.keyboard.press("Enter");
              })(),
            ]);
          } catch { /* ignore */ }
          await sleep(400);
          ({ url, text } = await pageState());
        }
      } else {
        return { email, status: "2fa_required", reason: "2FA code required — provide TOTP secret", totpCode };
      }
    }

    // ── Final classification ──────────────────────────────────────
    const final = await classify(url, text);
    if (final) return final;

    return {
      email,
      status: "unknown",
      reason: `Unexpected page after login (${url.slice(0, 80)})`,
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
