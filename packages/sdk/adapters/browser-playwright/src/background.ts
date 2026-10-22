import { execFile } from "node:child_process";
import { mkdir, readdir, readFile, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { promisify } from "node:util";
import { setTimeout as delay } from "node:timers/promises";
import {
  chromium,
  type Browser,
  type BrowserContext,
  type BrowserContextOptions,
  type CDPSession,
  type Page,
} from "playwright";

const execFileAsync = promisify(execFile);
const removeProfile = (path: string) => rm(path, {
  recursive: true, force: true, maxRetries: 20, retryDelay: 100,
});
const PROFILE_PREFIX = "mai-ui-background-chrome-";
// 建页阶段写入 worker stderr，区分 CDP 创建未响应与 Playwright 没收到 page。
const logTab = (message: string) =>
  process.stderr.write(`${new Date().toISOString()} background tab: ${message}\n`);

async function bounded<T>(work: Promise<T>, ms: number): Promise<T> {
  return await Promise.race([
    work,
    delay(ms, undefined, { ref: false }).then(() => { throw new Error(`timed out after ${ms} ms`); }),
  ]);
}

function chromeApp(channel?: string): string {
  const named: Record<string, string> = {
    chrome: "Google Chrome",
    "chrome-beta": "Google Chrome Beta",
    "chrome-dev": "Google Chrome Dev",
    "chrome-canary": "Google Chrome Canary",
  };
  if (channel && channel !== "chromium") {
    const app = named[channel];
    if (!app) throw new Error(`Background launch does not support channel ${channel}`);
    return app;
  }
  const match = chromium.executablePath().match(/^(.+\.app)\/Contents\/MacOS\//);
  if (!match) throw new Error("Playwright Chromium is not a macOS app bundle");
  return match[1];
}

async function profilePids(profileDir: string): Promise<number[]> {
  // The randomly created profile path is an ownership token: never signal the
  // user's normal Chrome or an existing attached browser.
  const { stdout } = await execFileAsync("/bin/ps", ["-axo", "pid=,command="]);
  const argument = `--user-data-dir=${profileDir}`;
  const pids: number[] = [];
  for (const line of stdout.split("\n")) {
    const match = line.trim().match(/^(\d+)\s+(.+)$/);
    if (match && match[2].split(/\s+/).includes(argument)) pids.push(Number(match[1]));
  }
  return pids;
}

async function stopOwnedChrome(profileDir: string): Promise<void> {
  for (const pid of await profilePids(profileDir)) {
    try { process.kill(pid, "SIGTERM"); } catch { /* already exited */ }
  }
}

// Chrome is started through LaunchServices (`open -n`), outside this worker's process
// group, so a SIGKILLed worker cannot close it. Every launch records a lease (same
// format as the Python web shim); the next launch adopts or stops browsers whose owner
// process is gone, so the user never accumulates automation browsers.
function leaseDirectory(): string {
  const base = process.env.GENERALIST_DATA_DIR;
  return join(base || join(homedir(), "Library/Application Support/Generalist"), "browser-leases");
}
const leasePath = (profileDir: string) => join(leaseDirectory(), `${basename(profileDir)}.json`);

async function writeLease(profileDir: string): Promise<void> {
  await mkdir(leaseDirectory(), { recursive: true });
  await writeFile(leasePath(profileDir), JSON.stringify({ profile: profileDir, owner_pid: process.pid }));
}

const dropLease = (profileDir: string) => rm(leasePath(profileDir), { force: true });

function ownerAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
}

async function orphanedProfiles(): Promise<{ lease: string; profile: string }[]> {
  let names: string[];
  try { names = (await readdir(leaseDirectory())).filter(name => name.endsWith(".json")).sort(); }
  catch { return []; }
  const tempRoot = await realpath(tmpdir());
  const orphans = [];
  for (const name of names) {
    const lease = join(leaseDirectory(), name);
    let profile: string, owner: number;
    try {
      const record = JSON.parse(await readFile(lease, "utf8"));
      profile = String(record.profile);
      owner = Number(record.owner_pid);
      if (!Number.isSafeInteger(owner)) throw new Error("invalid owner");
    } catch {
      await rm(lease, { force: true });
      continue;
    }
    if (owner === process.pid || ownerAlive(owner)) continue;
    const parent = await realpath(dirname(profile)).catch(() => "");
    if (!basename(profile).startsWith(PROFILE_PREFIX) || parent !== tempRoot) {
      await rm(lease, { force: true });  // never delete anything outside our temp profiles
      continue;
    }
    orphans.push({ lease, profile });
  }
  return orphans;
}

async function adoptOrphanedChrome(): Promise<{ browser: Browser; profileDir: string } | undefined> {
  let adopted: { browser: Browser; profileDir: string } | undefined;
  for (const { lease, profile } of await orphanedProfiles()) {
    if (!adopted && (await profilePids(profile)).length) {
      try {
        const port = Number((await readFile(join(profile, "DevToolsActivePort"), "utf8")).split("\n", 1)[0]);
        if (Number.isSafeInteger(port) && port > 0) {
          const browser = await bounded(chromium.connectOverCDP(`http://127.0.0.1:${port}`, { timeout: 3000 }), 5000);
          await writeLease(profile);
          adopted = { browser, profileDir: profile };
          continue;
        }
      } catch { /* unreachable: stopped below */ }
    }
    try {
      await stopOwnedChrome(profile);
      await removeProfile(profile);
      await rm(lease, { force: true });
    } catch { /* keep the lease and retry on the next launch */ }
  }
  return adopted;
}

function ownedBrowser(browser: Browser, profileDir: string) {
  let closed = false;
  return {
    browser,
    // Close politely, but always stop this profile's Chrome: a hung CDP call must
    // not leave another window behind.
    close: async () => {
      if (closed) return;
      closed = true;
      try {
        try {
          const session = await bounded(browser.newBrowserCDPSession(), 3000);
          await bounded(session.send("Browser.close"), 3000);
        } catch { /* Closing Chrome usually disconnects this CDP request. */ }
        await bounded(browser.close(), 3000).catch(() => {});
      } finally {
        await stopOwnedChrome(profileDir).catch(() => {});
        await dropLease(profileDir).catch(() => {});
        await removeProfile(profileDir).catch(() => {});
      }
    },
  };
}

export async function launchBackgroundChromium(channel: string | undefined, timeoutMs: number): Promise<{
  browser: Browser;
  close: () => Promise<void>;
}> {
  if (process.platform !== "darwin")
    throw new Error("Background Chrome app launch is only supported on macOS");
  const app = chromeApp(channel);
  const adopted = await adoptOrphanedChrome();
  if (adopted) return ownedBrowser(adopted.browser, adopted.profileDir);
  const profileDir = await mkdtemp(join(tmpdir(), PROFILE_PREFIX));
  let browser: Browser | undefined;
  try {
    // -j launches the app hidden: when another app (e.g. the driver) gives up the
    // foreground, macOS hands it to the next *visible* app, so a visible automation
    // Chrome would jump in front of the user. Hidden windows must not be throttled.
    await execFileAsync("/usr/bin/open", [
      "-g", "-j", "-n", "-a", app, "--args",
      "--remote-debugging-port=0", `--user-data-dir=${profileDir}`,
      "--no-first-run", "--no-default-browser-check", "--no-startup-window",
      "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
      "--disable-background-timer-throttling",
    ], { timeout: timeoutMs });
    const deadline = Date.now() + timeoutMs;
    let port = 0;
    while (Date.now() < deadline) {
      try {
        port = Number((await readFile(join(profileDir, "DevToolsActivePort"), "utf8"))
          .split("\n", 1)[0]);
        if (Number.isSafeInteger(port) && port > 0) break;
      } catch { /* Chrome has not written its endpoint yet. */ }
      await delay(50);
    }
    if (!port) throw new Error("Background Chrome did not publish a CDP endpoint");
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`, {
      timeout: Math.max(1, deadline - Date.now()),
    });
    await writeLease(profileDir);
    return ownedBrowser(browser, profileDir);
  } catch (error) {
    await browser?.close().catch(() => {});
    await stopOwnedChrome(profileDir).catch(() => {});
    await dropLease(profileDir).catch(() => {});
    await removeProfile(profileDir);
    throw error;
  }
}

export async function createIsolatedBackgroundContext(
  browser: Browser,
  options: BrowserContextOptions,
): Promise<{ context: BrowserContext; cdp: CDPSession; contextId: string }> {
  const cdp = await browser.newBrowserCDPSession();
  const before = new Set((await cdp.send("Target.getBrowserContexts")).browserContextIds);
  const context = await browser.newContext(options);
  const created = (await cdp.send("Target.getBrowserContexts")).browserContextIds
    .filter((id: string) => !before.has(id));
  if (created.length !== 1) {
    await context.close();
    await cdp.detach();
    throw new Error(`Expected one new Chromium context, got ${created.length}`);
  }
  return { context, cdp, contextId: created[0] };
}

async function targetIdOf(context: BrowserContext, page: Page): Promise<string | undefined> {
  let session: CDPSession | undefined;
  try {
    session = await context.newCDPSession(page);
    return (await session.send("Target.getTargetInfo")).targetInfo.targetId;
  } catch {
    return undefined;
  } finally {
    await session?.detach().catch(() => {});
  }
}

export async function createBackgroundPage(
  context: BrowserContext,
  cdp: Pick<CDPSession, "send">,
  contextId: string,
  timeoutMs: number,
): Promise<Page> {
  const started = Date.now();
  logTab(`sending Target.createTarget context=${contextId} budget=${timeoutMs} ms`);
  let targetId: string;
  try {
    ({ targetId } = await cdp.send("Target.createTarget", {
      url: "about:blank", browserContextId: contextId,
      background: true, focus: false,
    }));
  } catch (error) {
    logTab(`Target.createTarget failed after ${Date.now() - started} ms: ${error}`);
    throw error;
  }
  logTab(`target=${targetId} created in ${Date.now() - started} ms; waiting for Playwright page`);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const page of context.pages()) {
      if (!page.isClosed() && await targetIdOf(context, page) === targetId) {
        logTab(`target=${targetId} attached in ${Date.now() - started} ms`);
        return page;
      }
    }
    await delay(25);
  }
  logTab(`target=${targetId} attach timed out; browser_connected=${context.browser()?.isConnected()} pages=${context.pages().length}`);
  await cdp.send("Target.closeTarget", { targetId }).catch(() => {});
  throw new Error(`Background tab ${targetId} was not attached within ${timeoutMs} ms`);
}
