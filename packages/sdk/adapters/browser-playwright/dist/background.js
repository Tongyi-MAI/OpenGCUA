import { execFile } from "node:child_process";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { setTimeout as delay } from "node:timers/promises";
import { chromium, } from "playwright";
const execFileAsync = promisify(execFile);
const removeProfile = (path) => rm(path, {
    recursive: true, force: true, maxRetries: 20, retryDelay: 100,
});
function chromeApp(channel) {
    const named = {
        chrome: "Google Chrome",
        "chrome-beta": "Google Chrome Beta",
        "chrome-dev": "Google Chrome Dev",
        "chrome-canary": "Google Chrome Canary",
    };
    if (channel && channel !== "chromium") {
        const app = named[channel];
        if (!app)
            throw new Error(`Background launch does not support channel ${channel}`);
        return app;
    }
    const match = chromium.executablePath().match(/^(.+\.app)\/Contents\/MacOS\//);
    if (!match)
        throw new Error("Playwright Chromium is not a macOS app bundle");
    return match[1];
}
async function stopOwnedChrome(profileDir) {
    // The randomly created profile path is an ownership token: never signal the
    // user's normal Chrome or an existing attached browser.
    const { stdout } = await execFileAsync("/bin/ps", ["-axo", "pid=,command="]);
    const argument = `--user-data-dir=${profileDir}`;
    for (const line of stdout.split("\n")) {
        const match = line.trim().match(/^(\d+)\s+(.+)$/);
        if (!match || !match[2].split(/\s+/).includes(argument))
            continue;
        try {
            process.kill(Number(match[1]), "SIGTERM");
        }
        catch { /* already exited */ }
    }
}
export async function launchBackgroundChromium(channel, timeoutMs) {
    if (process.platform !== "darwin")
        throw new Error("Background Chrome app launch is only supported on macOS");
    const profileDir = await mkdtemp(join(tmpdir(), "mai-ui-background-chrome-"));
    let browser;
    try {
        await execFileAsync("/usr/bin/open", [
            "-g", "-n", "-a", chromeApp(channel), "--args",
            "--remote-debugging-port=0", `--user-data-dir=${profileDir}`,
            "--no-first-run", "--no-default-browser-check", "--no-startup-window",
        ], { timeout: timeoutMs });
        const deadline = Date.now() + timeoutMs;
        let port = 0;
        while (Date.now() < deadline) {
            try {
                port = Number((await readFile(join(profileDir, "DevToolsActivePort"), "utf8"))
                    .split("\n", 1)[0]);
                if (Number.isSafeInteger(port) && port > 0)
                    break;
            }
            catch { /* Chrome has not written its endpoint yet. */ }
            await delay(50);
        }
        if (!port)
            throw new Error("Background Chrome did not publish a CDP endpoint");
        browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`, {
            timeout: Math.max(1, deadline - Date.now()),
        });
        const owned = browser;
        let closed = false;
        return {
            browser: owned,
            close: async () => {
                if (closed)
                    return;
                closed = true;
                try {
                    const session = await owned.newBrowserCDPSession();
                    await session.send("Browser.close");
                }
                catch { /* Closing Chrome usually disconnects this CDP request. */ }
                await owned.close().catch(() => { });
                await stopOwnedChrome(profileDir).catch(() => { });
                await removeProfile(profileDir);
            },
        };
    }
    catch (error) {
        await browser?.close().catch(() => { });
        await stopOwnedChrome(profileDir).catch(() => { });
        await removeProfile(profileDir);
        throw error;
    }
}
export async function createIsolatedBackgroundContext(browser, options) {
    const cdp = await browser.newBrowserCDPSession();
    const before = new Set((await cdp.send("Target.getBrowserContexts")).browserContextIds);
    const context = await browser.newContext(options);
    const created = (await cdp.send("Target.getBrowserContexts")).browserContextIds
        .filter((id) => !before.has(id));
    if (created.length !== 1) {
        await context.close();
        await cdp.detach();
        throw new Error(`Expected one new Chromium context, got ${created.length}`);
    }
    return { context, cdp, contextId: created[0] };
}
async function targetIdOf(context, page) {
    let session;
    try {
        session = await context.newCDPSession(page);
        return (await session.send("Target.getTargetInfo")).targetInfo.targetId;
    }
    catch {
        return undefined;
    }
    finally {
        await session?.detach().catch(() => { });
    }
}
export async function createBackgroundPage(context, cdp, contextId, timeoutMs) {
    const { targetId } = await cdp.send("Target.createTarget", {
        url: "about:blank", browserContextId: contextId,
        background: true, focus: false,
    });
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        for (const page of context.pages()) {
            if (!page.isClosed() && await targetIdOf(context, page) === targetId)
                return page;
        }
        await delay(25);
    }
    await cdp.send("Target.closeTarget", { targetId }).catch(() => { });
    throw new Error(`Background tab ${targetId} was not attached within ${timeoutMs} ms`);
}
