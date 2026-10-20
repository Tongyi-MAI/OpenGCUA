import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import {
  createBackgroundPage,
  createIsolatedBackgroundContext,
  launchBackgroundChromium,
} from "../dist/background.js";
import { executePlaywright } from "../dist/playwright.js";

test("new tabs are created in their own context without requesting focus", async () => {
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  try {
    const { context, cdp, contextId } =
      await createIsolatedBackgroundContext(browser, {});
    const calls = [];
    const observedCdp = {
      send: async (method, params) => {
        calls.push({ method, params });
        return cdp.send(method, params);
      },
    };

    const [first, second] = await Promise.all([
      createBackgroundPage(context, observedCdp, contextId, 10000),
      createBackgroundPage(context, observedCdp, contextId, 10000),
    ]);

    assert.notEqual(first, second);
    assert.equal(first.context(), context);
    assert.equal(second.context(), context);
    assert.equal(context.pages().length, 2);
    assert.deepEqual(calls, [
      { method: "Target.createTarget", params: {
        url: "about:blank", browserContextId: contextId,
        background: true, focus: false,
      } },
      { method: "Target.createTarget", params: {
        url: "about:blank", browserContextId: contextId,
        background: true, focus: false,
      } },
    ]);
  } finally {
    await browser.close();
  }
});

test("background search submits a blank-target GET form in the same tab", async () => {
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  try {
    const context = await browser.newContext();
    await context.route("https://shop.test/search?**", route => route.fulfill({
      contentType: "text/html", body: "<title>Search results</title>",
    }));
    const page = await context.newPage();
    await page.setContent('<form method="get" action="https://shop.test/search" target="_blank"><input id="q" name="q" value="tablet"></form>');
    await executePlaywright(page, {
      kind: "locator", method: "press", query: [
        { method: "locator", args: [{ t: "value", v: "#q" }] },
      ], args: [{ t: "value", v: "Enter" }], timeout_ms: 10000,
      background_launch: true,
    });
    await page.waitForURL("https://shop.test/search?q=tablet", { timeout: 2000 });
    assert.equal(context.pages().length, 1);
    assert.equal(await page.title(), "Search results");
  } finally {
    await browser.close();
  }
});

test("background login form refuses a blank-target popup before pressing Enter", async () => {
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.setContent('<form method="post" action="https://shop.test/login" target="_blank"><input id="username" name="username" value="someone"></form>');
    await assert.rejects(executePlaywright(page, {
      kind: "locator", method: "press", query: [
        { method: "locator", args: [{ t: "value", v: "#username" }] },
      ], args: [{ t: "value", v: "Enter" }], timeout_ms: 10000,
      background_launch: true,
    }), { code: "ACTION_REJECTED", message: /FOREGROUND_TAKEOVER_REQUIRED/ });
    assert.equal(context.pages().length, 1);
  } finally {
    await browser.close();
  }
});

test("background link click refuses a blank-target popup", async () => {
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.setContent('<a id="login" href="https://shop.test/login" target="_blank">Log in</a>');
    await assert.rejects(executePlaywright(page, {
      kind: "locator", method: "click", query: [
        { method: "locator", args: [{ t: "value", v: "#login" }] },
      ], args: [], timeout_ms: 10000, background_launch: true,
    }), { code: "ACTION_REJECTED", message: /FOREGROUND_TAKEOVER_REQUIRED/ });
    assert.equal(context.pages().length, 1);
  } finally {
    await browser.close();
  }
});

test("background search button submits a blank-target GET form in the same tab", async () => {
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  try {
    const context = await browser.newContext();
    await context.route("https://shop.test/search?**", route => route.fulfill({
      contentType: "text/html", body: "<title>Search results</title>",
    }));
    const page = await context.newPage();
    await page.setContent('<form method="get" action="https://shop.test/search" target="_blank"><input name="q" value="tablet"><button id="go" type="submit">Search</button></form>');
    await executePlaywright(page, {
      kind: "locator", method: "click", query: [
        { method: "locator", args: [{ t: "value", v: "#go" }] },
      ], args: [], timeout_ms: 10000, background_launch: true,
    });
    await page.waitForURL("https://shop.test/search?q=tablet", { timeout: 2000 });
    assert.equal(context.pages().length, 1);
  } finally {
    await browser.close();
  }
});

test("background login button refuses a blank-target POST form", async () => {
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.setContent('<form method="post" action="https://shop.test/login" target="_blank"><input name="username" value="someone"><button id="go" type="submit">Log in</button></form>');
    await assert.rejects(executePlaywright(page, {
      kind: "locator", method: "click", query: [
        { method: "locator", args: [{ t: "value", v: "#go" }] },
      ], args: [], timeout_ms: 10000, background_launch: true,
    }), { code: "ACTION_REJECTED", message: /FOREGROUND_TAKEOVER_REQUIRED/ });
    assert.equal(context.pages().length, 1);
  } finally {
    await browser.close();
  }
});

test("worker uses background targets for both SDK and Playwright new-page calls", async () => {
  const child = spawn(process.execPath, [
    fileURLToPath(new URL("./background-worker.mjs", import.meta.url)),
  ], { stdio: ["pipe", "pipe", "pipe"] });
  let nextId = 0;
  let stdout = "";
  let stderr = "";
  const pending = new Map();
  child.stdout.on("data", (chunk) => {
    stdout += chunk.toString();
    while (stdout.includes("\n")) {
      const line = stdout.slice(0, stdout.indexOf("\n"));
      stdout = stdout.slice(stdout.indexOf("\n") + 1);
      const reply = JSON.parse(line);
      if (reply._request_id !== undefined) {
        pending.get(reply._request_id)?.(reply);
        pending.delete(reply._request_id);
      }
    }
  });
  child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
  const send = (request) => new Promise((resolve, reject) => {
    const id = ++nextId;
    const timeout = setTimeout(() => reject(new Error(`Worker timeout: ${stderr}`)), 15000);
    pending.set(id, (reply) => { clearTimeout(timeout); resolve(reply); });
    child.stdin.write(JSON.stringify({ ...request, _request_id: id }) + "\n");
  });
  try {
    const init = await send({ op: "init", context_id: "background-test", engine: "chromium",
      headless: true, channel: "chrome", background_launch: true, timeout_ms: 10000 });
    assert.equal(init.result?.ready, true, stderr);
    const tab = await send({ op: "browser", operation: "new_tab", timeout_ms: 10000 });
    assert.ok(tab.result?.tab?.id, JSON.stringify(tab));
    const second = await send({ op: "browser", operation: "playwright",
      kind: "context", method: "newPage", args: [], timeout_ms: 10000 });
    assert.equal(second.result?.tabs?.length, 2, JSON.stringify(second));
    await send({ op: "stop" });
  } finally {
    child.stdin.end();
    child.kill();
  }
});

test("macOS background launcher connects to a dedicated headed Chrome", {
  skip: process.platform !== "darwin" || process.env.OGCUA_TEST_DESKTOP_FOCUS !== "1",
}, async () => {
  const foregroundApp = () => execFileSync("/usr/bin/osascript", [
    "-l", "JavaScript", "-e",
    'ObjC.import("AppKit"); $.NSWorkspace.sharedWorkspace.frontmostApplication.localizedName.js',
  ], { encoding: "utf8" }).trim();
  const before = foregroundApp();
  const launched = await launchBackgroundChromium("chrome", 20000);
  try {
    assert.equal(launched.browser.isConnected(), true);
    assert.equal(launched.browser.browserType().name(), "chromium");
    const { context, cdp, contextId } = await createIsolatedBackgroundContext(
      launched.browser, {});
    const first = await createBackgroundPage(context, cdp, contextId, 10000);
    const second = await createBackgroundPage(context, cdp, contextId, 10000);
    await first.goto("data:text/html,<title>First background tab</title>");
    await second.goto("data:text/html,<title>Second background tab</title>");
    assert.equal(await first.title(), "First background tab");
    assert.equal(await second.title(), "Second background tab");
    assert.equal(foregroundApp(), before, "Chrome took focus during background launch or tab creation");
  } finally {
    await launched.close();
  }
});
