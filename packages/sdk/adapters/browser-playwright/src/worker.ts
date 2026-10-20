import {
  chromium,
  firefox,
  webkit,
  type Browser,
  type BrowserContext,
  type Page,
  type Dialog,
  type Route,
  type Download,
} from "playwright";
import { createInterface } from "node:readline";
import { mkdtemp, open, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { Identity } from "./identity.js";
import { ProviderError, requireValue } from "./errors.js";
import { executePlaywright, encodeValue } from "./playwright.js";
import { browserInput, releaseInput, buttonHeld, pause } from "./input.js";
import {
  createBackgroundPage,
  createIsolatedBackgroundContext,
  launchBackgroundChromium,
} from "./background.js";
import type { CDPSession } from "playwright";

type Request = Record<string, any>;
type Tab = {
  id: string;
  page: Page;
  identity: Identity;
  dialogs: Request[];
  downloads: Map<string, Request>;
  revision: number;
};
let browser: Browser | undefined;
let context: BrowserContext | undefined;
let backgroundCdp: CDPSession | undefined;
let backgroundContextId: string | undefined;
let closeBackgroundBrowser: (() => Promise<void>) | undefined;
let contextId = "";
let artifacts = "";
let networkRules: Request[] = [];
const tabs = new Map<string, Tab>();
const operations = new Map<string, AbortController>();
const downloadTasks = new Set<Promise<void>>();
let ownsArtifactDirectory = false;
let stopping = false;
const send = (value: unknown) =>
  process.stdout.write(JSON.stringify(value) + "\n");
const event = (type: string, data: unknown, tab?: Tab) =>
  send({
    event: type,
    resource_id: tab ? `${contextId}/${tab.id}` : contextId,
    data,
  });
const complete = (extra: Request = {}) => ({
  execution: "completed",
  effect: "unknown",
  mechanism: "playwright",
  delivery: { mode: "background", scope: "tab" },
  ...extra,
});
function getContext(): BrowserContext {
  if (!context) throw new ProviderError("DEVICE_OFFLINE");
  return context;
}
async function newPage(timeoutMs = 30000): Promise<Page> {
  const ctx = getContext();
  if (backgroundCdp && backgroundContextId)
    return createBackgroundPage(ctx, backgroundCdp, backgroundContextId, timeoutMs);
  return ctx.newPage();
}
function getTab(id: unknown): Tab {
  const tab = typeof id === "string" ? tabs.get(id) : undefined;
  if (!tab || tab.page.isClosed())
    throw new ProviderError("STALE_TARGET", "Tab is closed or unknown");
  return tab;
}
const inventory = async (tab: Tab) => ({
  id: tab.id,
  resource_id: `${contextId}/${tab.id}`,
  title: await tab.page.title().catch(() => ""),
  url: tab.page.url(),
  kind: "tab",
  viewport: tab.page.viewportSize(),
});

function addTab(page: Page): Tab {
  const old = [...tabs.values()].find((t) => t.page === page);
  if (old) return old;
  const id = `tab:${randomUUID()}`;
  const tab: Tab = {
    id,
    page,
    identity: new Identity(page, id),
    dialogs: [],
    downloads: new Map(),
    revision: 0,
  };
  tabs.set(id, tab);
  page.on("dialog", (dialog) => void handleDialog(tab, dialog));
  page.on("download", (download) => {
    const task = handleDownload(tab, download);
    downloadTasks.add(task);
    void task.finally(() => downloadTasks.delete(task));
  });
  page.on("close", () => {
    void tab.identity.close();
    event("tab_closed", { id }, tab);
  });
  page.on("framenavigated", (frame) => {
    event(
      "frame_navigated",
      { url: frame.url(), main_frame: frame === page.mainFrame() },
      tab,
    );
  });
  page.on("crash", () =>
    event("resource_offline", { reason: "page_crash" }, tab),
  );
  event("tab_created", { id, url: page.url() }, tab);
  return tab;
}
async function handleDialog(tab: Tab, dialog: Dialog) {
  const rule = tab.dialogs.find(
    (r) =>
      (!r.type || r.type === dialog.type()) &&
      (r.message === undefined || r.message === dialog.message()),
  );
  const action = rule?.action ?? "dismiss";
  try {
    if (action === "accept") await dialog.accept(rule?.prompt_text);
    else await dialog.dismiss();
    event(
      "dialog_handled",
      {
        type: dialog.type(),
        message: dialog.message(),
        action,
        matched: !!rule,
      },
      tab,
    );
  } catch (error) {
    event("dialog_failed", { message: String(error) }, tab);
  }
}
async function handleDownload(tab: Tab, download: Download) {
  const id = `download:${randomUUID()}`;
  const entry: Request = {
    id,
    suggested_filename: download.suggestedFilename(),
    url: download.url(),
    state: "in_progress",
    artifact_id: id,
  };
  tab.downloads.set(id, entry);
  event("download_started", entry, tab);
  try {
    const path = join(artifacts, id.replace(":", "-"));
    await download.saveAs(path);
    const failure = await download.failure();
    if (failure) throw new Error(failure);
    entry.path = path;
    entry.state = "completed";
    event("download_completed", publicDownload(entry), tab);
  } catch (e) {
    entry.state = "failed";
    entry.error = String(e);
    event("download_failed", publicDownload(entry), tab);
  }
}
function publicDownload(entry: Request) {
  const { path, ...result } = entry;
  return result;
}
function glob(pattern: string): RegExp {
  let out = "^";
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i];
    if (c === "*") {
      if (pattern[i + 1] === "*") {
        out += ".*";
        i++;
      } else out += "[^/]*";
    } else if (c === "?") out += ".";
    else out += c.replace(/[\\^$.*+?()[\]{}|]/g, "\\$&");
  }
  return new RegExp(out + "$");
}
async function routeRequest(route: Route) {
  const request = route.request();
  const rules = networkRules;
  const rule = rules.find(
    (r) =>
      (!r.url || glob(r.url).test(request.url())) &&
      (!r.method || r.method === request.method()) &&
      (!r.resource_type || r.resource_type === request.resourceType()),
  );
  try {
    if (rule?.action === "abort")
      await route.abort(rule.error_code ?? "failed");
    else if (rule?.action === "fulfill")
      await route.fulfill({
        status: rule.status ?? 200,
        headers: rule.headers,
        contentType: rule.content_type,
        body: rule.body_base64
          ? Buffer.from(rule.body_base64, "base64")
          : (rule.body ?? ""),
      });
    else
      await route.continue(
        rule?.overrides
          ? {
              url: rule.overrides.url,
              method: rule.overrides.method,
              headers: rule.overrides.headers,
              postData: rule.overrides.body,
            }
          : undefined,
      );
  } catch (error) {
    event("network_rule_failed", { url: request.url(), error: String(error) });
    await route.abort("failed").catch(() => {});
  }
}
const caps = [
  "ui.observe.tree",
  "ui.observe.screenshot",
  "ui.click.primary",
  "ui.click.coordinates",
  "ui.set_text",
  "ui.select_text",
  "ui.perform",
  "ui.scroll.viewport",
  "ui.scroll.coordinates",
  "ui.input_text",
  "ui.press_key",
  "ui.move",
  "ui.drag",
  "ui.key_state", "ui.button_state", "ui.release_all", "ui.wheel",
  "browser.playwright",
  "browser.list_tabs",
  "browser.get_tab",
  "browser.new_tab",
  "browser.goto",
  "browser.go_back",
  "browser.go_forward",
  "browser.reload",
  "browser.close",
  "browser.destroy",
  "browser.evaluate",
  "browser.dialogs_set_rules",
  "browser.upload",
  "browser.downloads_list",
  "browser.downloads_get",
  "browser.artifact_read",
  "browser.cookies_read",
  "browser.cookies_set",
  "browser.cookies_clear",
  "browser.storage_state",
  "browser.storage_read",
  "browser.storage_set",
  "browser.storage_remove",
  "browser.storage_clear",
  "browser.network_set_rules",
];
function describe() {
  return {
    protocol_major: 1,
    platform: "browser",
    provider: "browser.playwright",
    playwright_version: "1.63.0",
    adapter_version: "0.3.0",
    engine_version: browser?.version(),
    capabilities: Object.fromEntries(
      caps.map((k) => [k, { state: "ready", provider: "browser.playwright" }]),
    ),
  };
}

async function capture(tab: Tab, q: Request) {
  const page = tab.page;
  const viewport = page.viewportSize();
  if (!viewport)
    throw new ProviderError("OBSERVATION_FAILED", "Missing viewport");
  const dpr = await page.evaluate(() => devicePixelRatio);
  const document = await tab.identity.document(page.mainFrame());
  const nodes: Request[] = [];
  let truncated = false;
  if (q.tree) {
    await tab.identity.prune();
    const snapshot: any = await page.ariaSnapshotJSON({
      mode: "ai",
      boxes: true,
      timeout: q.timeout_ms,
      signal: q.signal,
    });
    let counter = 0;
    const visit = async (raw: any, parent: string | null, ordinal: number) => {
      if (typeof raw === "string") raw = { role: "text", text: raw };
      let target;
      if (raw.ref) {
        try {
          target = await tab.identity.acquire(raw.ref);
        } catch {
          truncated = true;
        }
      }
      const id = target?.id ?? `${parent ?? document.id}:readonly:${ordinal}`;
      let metadata: any = {};
      if (target) {
        try {
          metadata = await target.handle.evaluate((el: any) => ({
            tag: el.tagName,
            password: el.type === "password",
            text:
              el.type === "password"
                ? null
                : typeof el.value === "string"
                  ? el.value
                  : el.isContentEditable
                    ? el.textContent
                    : undefined,
            editable:
              !el.readOnly &&
              !el.disabled &&
              (el.isContentEditable ||
                ["INPUT", "TEXTAREA"].includes(el.tagName)),
            focused: el.ownerDocument.activeElement === el,
            scrollable:
              el.scrollHeight > el.clientHeight ||
              el.scrollWidth > el.clientWidth,
            checkable: ["checkbox", "radio"].includes(el.type),
            selectable: el.tagName === "SELECT",
          }));
        } catch {
          truncated = true;
          target = undefined;
        }
      }
      const box = target
        ? await target.handle.boundingBox().catch(() => null)
        : null;
      const actions: Request[] = target ? [{ id: "click" }] : [];
      if (metadata.checkable) actions.push({ id: "check" }, { id: "uncheck" });
      const text = metadata.password
        ? null
        : (metadata.text ?? raw.text ?? null);
      nodes.push({
        id,
        parent_id: parent,
        window_id: tab.id,
        app_id: null,
        class_name: metadata.tag ?? null,
        automation_id: null,
        role: raw.role ?? "unknown",
        name: raw.name ?? "",
        text,
        hint: raw.placeholder ?? null,
        text_status: metadata.password ? "redacted" : "readable",
        bounds: box
          ? { x: box.x, y: box.y, width: box.width, height: box.height }
          : null,
        enabled: !raw.disabled,
        visible: box ? box.width > 0 && box.height > 0 : null,
        focused: metadata.focused ?? false,
        editable: metadata.editable ?? false,
        password: metadata.password ?? false,
        checked: typeof raw.checked === "boolean" ? raw.checked : null,
        actions,
        targetable: !!target,
        target: target
          ? { tab_id: tab.id, document_id: target.document.id }
          : undefined,
      });
      counter++;
      for (let i = 0; i < (raw.children ?? []).length; i++)
        await visit(raw.children[i], id, i);
    };
    const roots = Array.isArray(snapshot) ? snapshot : [snapshot];
    for (let i = 0; i < roots.length; i++) await visit(roots[i], null, i);
  }
  const png = q.screenshot
    ? await page.screenshot({
        type: "png",
        fullPage: false,
        scale: "device",
        timeout: q.timeout_ms,
        signal: q.signal,
      })
    : undefined;
  const width = png ? png.readUInt32BE(16) : Math.round(viewport.width * dpr);
  const height = png ? png.readUInt32BE(20) : Math.round(viewport.height * dpr);
  return {
    nodes,
    width,
    height,
    revision: String(++tab.revision),
    captured_at: Date.now(),
    age_ms: 0,
    source: "playwright.aria",
    truncated,
    pending_invalidations: 0,
    identity_mode: "native_lifetime",
    identity_epoch: document.id,
    png: png?.toString("base64"),
    target: {
      tab_id: tab.id,
      viewport_width: viewport.width,
      viewport_height: viewport.height,
      device_scale_factor: dpr,
    },
    label: `${await page.title()} (${tab.id})`,
    geometry: { viewport, device_scale_factor: dpr, coordinates: "css_pixels" },
  };
}
function point(tab: Tab, q: Request) {
  const viewport = tab.page.viewportSize()!;
  if (
    q.viewport_width !== viewport.width ||
    q.viewport_height !== viewport.height
  )
    throw new ProviderError("GEOMETRY_CHANGED");
  requireValue(
    typeof q.x === "number" &&
      typeof q.y === "number" &&
      q.x >= 0 &&
      q.x < q.frame_width &&
      q.y >= 0 &&
      q.y < q.frame_height,
    "Point outside image",
  );
  return {
    x: (q.x * viewport.width) / q.frame_width,
    y: (q.y * viewport.height) / q.frame_height,
  };
}
async function action(tab: Tab, q: Request) {
  const page = tab.page;
  const target = q.node_id ? await tab.identity.resolve(q.node_id) : undefined;
  const handle = target?.handle;
  const options = { timeout: q.timeout_ms, signal: q.signal };
  const mouseButton =
    ({ primary: "left", secondary: "right", middle: "middle" } as const)[
      q.button as "primary"
    ] ?? "left";
  if (q.delivery === "foreground") await page.bringToFront();
  switch (q.action) {
    case "key_down": case "key_up":
      await browserInput(page, "keyboard", q.action === "key_down" ? "down" : "up", [q.key]);
      break;
    case "button_down":
      { const p = point(tab, q); await browserInput(page, "mouse", "move", [p.x, p.y]); }
      await browserInput(page, "mouse", "down", [{ button: mouseButton }]);
      break;
    case "button_up":
      await browserInput(page, "mouse", "up", [{ button: mouseButton }]);
      break;
    case "release_all": await releaseInput(page); break;
    case "wheel": {
      requireValue(q.unit === "pixels", "Browser wheel requires pixels");
      const p = point(tab, q);
      await browserInput(page, "mouse", "move", [p.x, p.y]);
      await browserInput(page, "mouse", "wheel", [q.delta_x, q.delta_y]);
      break;
    }
    case "click":
      requireValue(!buttonHeld(page, mouseButton), "Release held button before clicking");
      requireValue(handle, "Node required");
      await handle.click({
        ...options,
        button: mouseButton,
        clickCount: q.count ?? 1,
      });
      break;
    case "set_text":
      requireValue(
        handle && typeof q.text === "string",
        "Editable node and text required",
      );
      await handle.fill(q.text, options);
      break;
    case "select_text": {
      requireValue(
        handle && Number.isInteger(q.start) && Number.isInteger(q.end),
        "Selection target and offsets required",
      );
      await handle.waitForElementState("visible", options);
      const result = await handle.evaluate(
        (el: any, range: any) => {
          const text =
            typeof el.value === "string" ? el.value : (el.textContent ?? "");
          if (
            el.type === "password" ||
            text !== range.text ||
            range.start < 0 ||
            range.end < range.start ||
            range.end > [...text].length
          )
            return false;
          const offset = (n: number) => [...text].slice(0, n).join("").length;
          el.focus();
          if (typeof el.setSelectionRange === "function")
            el.setSelectionRange(offset(range.start), offset(range.end));
          else if (el.isContentEditable) {
            const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
            const ns: Text[] = [];
            let node;
            while ((node = walk.nextNode())) ns.push(node as Text);
            const locate = (n: number): [Node, number] => {
              for (const t of ns) {
                if (n <= t.length) return [t, n];
                n -= t.length;
              }
              return [el, el.childNodes.length];
            };
            const rangeObj = document.createRange();
            rangeObj.setStart(...locate(offset(range.start)));
            rangeObj.setEnd(...locate(offset(range.end)));
            const selection = window.getSelection()!;
            selection.removeAllRanges();
            selection.addRange(rangeObj);
          } else return false;
          return true;
        },
        { start: q.start, end: q.end, text: q.selection_text },
      );
      if (!result)
        throw new ProviderError(
          "STALE_TARGET",
          "Selection text changed or is not selectable",
        );
      break;
    }
    case "perform":
      requireValue(handle, "Node required");
      if (q.node_action === "check") await handle.check(options);
      else if (q.node_action === "uncheck") await handle.uncheck(options);
      else if (q.node_action === "click") await handle.click(options);
      else throw new ProviderError("UNSUPPORTED_CAPABILITY");
      break;
    case "node_scroll":
      requireValue(
        handle &&
          ["up", "down", "left", "right"].includes(q.direction) &&
          q.amount > 0 &&
          q.amount <= 1,
        "Invalid scroll",
      );
      await handle.evaluate((el: any, q) => {
        const h = ["left", "right"].includes(q.direction);
        const amount =
          0.8 *
          q.amount *
          (h ? el.clientWidth : el.clientHeight) *
          (["up", "left"].includes(q.direction) ? -1 : 1);
        el.scrollBy({
          left: h ? amount : 0,
          top: h ? 0 : amount,
          behavior: "instant",
        });
      }, q);
      break;
    case "point_click": {
      const p = point(tab, q);
      await browserInput(page, "mouse", "click", [p.x, p.y, {
        button: mouseButton,
        clickCount: q.count ?? 1,
      }]);
      break;
    }
    case "move": {
      const p = point(tab, q);
      await browserInput(page, "mouse", "move", [p.x, p.y, { steps: q.steps ?? 1 }]);
      break;
    }
    case "wheel_scroll": {
      requireValue(
        q.unit === "viewport" &&
          q.amount > 0 &&
          q.amount <= 1 &&
          ["up", "down", "left", "right"].includes(q.direction),
        "Invalid scroll",
      );
      const p = point(tab, q);
      await page.mouse.move(p.x, p.y);
      const horizontal = ["left", "right"].includes(q.direction);
      const delta =
        0.8 *
        q.amount *
        (horizontal ? q.viewport_width : q.viewport_height) *
        (["up", "left"].includes(q.direction) ? -1 : 1);
      await page.mouse.wheel(horizontal ? delta : 0, horizontal ? 0 : delta);
      break;
    }
    case "drag": {
      const p = point(tab, q),
        end = point(tab, { ...q, x: q.end_x, y: q.end_y });
      requireValue((q.hold_ms ?? 0) + q.duration_ms + 100 < q.timeout_ms, "Drag must leave time for release");
      q.signal?.throwIfAborted();
      if (buttonHeld(page)) throw new ProviderError("INPUT_REFUSED", "Release the held button before dragging");
      await browserInput(page, "mouse", "move", [p.x, p.y]);
      await browserInput(page, "mouse", "down", []);
      try {
        if (q.hold_ms) await pause(q.hold_ms, q.signal);
        const began = performance.now();
        const count = Math.max(2, Math.ceil(q.duration_ms / 16));
        for (let i = 1; i <= count; i++) {
          q.signal?.throwIfAborted();
          await pause(Math.max(0, began + q.duration_ms * i / count - performance.now()), q.signal);
          await page.mouse.move(
            p.x + ((end.x - p.x) * i) / count,
            p.y + ((end.y - p.y) * i) / count,
          );
        }
      } finally {
        await browserInput(page, "mouse", "up", []);
      }
      break;
    }
    case "input_text":
      if (handle) await handle.focus();
      else if (q.x !== undefined) {
        const p = point(tab, q);
        await browserInput(page, "mouse", "click", [p.x, p.y]);
      }
      requireValue(typeof q.text === "string", "Text required");
      // Follow focus through frames and open shadow roots; never type into an
      // unrelated document when there is no editable focused control.
      let editable = false;
      let focusFrame: import("playwright").Frame | null = page.mainFrame();
      while (focusFrame) {
        const focused: import("playwright").JSHandle =
          await focusFrame.evaluateHandle(() => {
            let el = document.activeElement;
            while (el?.shadowRoot?.activeElement)
              el = el.shadowRoot.activeElement;
            return el;
          });
        const el: import("playwright").ElementHandle | null =
          focused.asElement();
        if (!el) {
          await focused.dispose();
          break;
        }
        editable = await el.evaluate(
          (el: any) =>
            el.isContentEditable ||
            (el instanceof HTMLTextAreaElement &&
              !el.disabled &&
              !el.readOnly) ||
            (el instanceof HTMLInputElement &&
              !el.disabled &&
              !el.readOnly &&
              [
                "text",
                "search",
                "email",
                "url",
                "tel",
                "password",
                "number",
              ].includes(el.type)),
        );
        focusFrame = editable ? null : await el.contentFrame();
        await focused.dispose();
      }
      if (!editable)
        throw new ProviderError(
          "INPUT_REFUSED",
          "Focus an editable control first",
        );
      await page.keyboard.insertText(q.text);
      break;
    case "press_key": {
      const keys: Record<string, string> = {
        enter: "Enter",
        tab: "Tab",
        escape: "Escape",
        backspace: "Backspace",
        delete: "Delete",
        space: "Space",
        arrowleft: "ArrowLeft",
        arrowright: "ArrowRight",
        arrowup: "ArrowUp",
        arrowdown: "ArrowDown",
        home: "Home",
        end: "End",
        pageup: "PageUp",
        pagedown: "PageDown",
      };
      const modifiers: Record<string, string> = {
        ctrl: "Control",
        control: "Control",
        alt: "Alt",
        shift: "Shift",
        meta: "Meta",
        cmd: "Meta",
      };
      requireValue(
        typeof q.key === "string" &&
          (q.modifiers ?? []).every((m: string) => m in modifiers),
        "Invalid key",
      );
      await browserInput(page, "keyboard", "press", [
        [
          ...(q.modifiers ?? []).map((m: string) => modifiers[m]),
          keys[q.key] ?? q.key,
        ].join("+"),
      ]);
      break;
    }
    default:
      throw new ProviderError(
        "UNSUPPORTED_CAPABILITY",
        `Unsupported action ${q.action}`,
      );
  }
  return complete({
    delivery: {
      mode: q.delivery === "foreground" ? "foreground" : "background",
      scope: "tab",
    },
  });
}
async function browserOperation(q: Request) {
  const ctx = getContext();
  const operation = q.operation;
  if (operation === "playwright") {
    if (q.kind === "context") {
      requireValue(!q.tab_id && Array.isArray(q.args) && q.args.length === 0, "Invalid context request");
      if (q.method === "close") { await ctx.close(); return { ...complete(), value: encodeValue(undefined), tabs: [] }; }
      let created;
      if (q.method === "newPage") created = addTab(await newPage(q.timeout_ms)).id;
      else requireValue(q.method === "pages", "Unsupported context method");
      return { ...complete(), value: encodeValue(created), tabs: await Promise.all([...tabs.values()].filter(t => !t.page.isClosed()).map(async t => ({ ...await inventory(t), viewport: t.page.viewportSize(), closed: false }))) };
    }
    const bound = getTab(q.tab_id);
    const value = await executePlaywright(bound.page, q);
    return { ...complete(), value, page: { id: bound.id, url: bound.page.url(), viewport: bound.page.viewportSize(), closed: bound.page.isClosed() } };
  }
  const tab = q.tab_id
    ? operation.startsWith("downloads_") || operation === "artifact_read"
      ? tabs.get(q.tab_id)
      : getTab(q.tab_id)
    : undefined;
  if (operation === "list_tabs")
    return {
      tabs: await Promise.all(
        [...tabs.values()].filter((t) => !t.page.isClosed()).map(inventory),
      ),
      complete: true,
    };
  if (operation === "get_tab") return inventory(getTab(q.id));
  if (operation === "new_tab") {
    const newTab = addTab(await newPage(q.timeout_ms));
    try {
      if (q.url && q.url !== "about:blank")
        await newTab.page.goto(q.url, {
          timeout: q.timeout_ms,
          waitUntil: q.wait_until ?? "domcontentloaded",
          signal: q.signal,
        });
      return { ...complete(), tab: await inventory(newTab) };
    } catch (e) {
      await newTab.page.close();
      throw e;
    }
  }
  if (operation === "destroy") {
    await Promise.all([...downloadTasks]);
    await ctx.close();
    if (closeBackgroundBrowser) await closeBackgroundBrowser();
    else await browser?.close();
    return complete();
  }
  if (operation === "cookies_read")
    return { cookies: await ctx.cookies(q.urls) };
  if (operation === "cookies_set") {
    await ctx.addCookies(q.cookies);
    return complete();
  }
  if (operation === "cookies_clear") {
    await ctx.clearCookies(q.filter ?? {});
    return complete();
  }
  if (operation === "storage_state")
    return await ctx.storageState({ indexedDB: true });
  if (operation === "network_set_rules") {
    requireValue(Array.isArray(q.rules), "Rules must be an array");
    for (const r of q.rules) {
      requireValue(
        ["continue", "abort", "fulfill", "override"].includes(r.action),
        "Invalid rule action",
      );
      if (r.url !== undefined) {
        requireValue(typeof r.url === "string", "Invalid URL glob");
        glob(r.url);
      }
      if (r.action === "override")
        requireValue(
          r.overrides && typeof r.overrides === "object",
          "Overrides required",
        );
    }
    networkRules = structuredClone(q.rules);
    return complete();
  }
  requireValue(tab, "A tab is required");
  const page = tab.page;
  if (["goto", "go_back", "go_forward", "reload"].includes(operation)) {
    const options = {
      timeout: q.timeout_ms,
      waitUntil: q.wait_until ?? "domcontentloaded",
      signal: q.signal,
    };
    if (operation === "goto") {
      requireValue(typeof q.url === "string", "URL required");
      await page.goto(q.url, options);
    } else if (operation === "go_back") await page.goBack(options);
    else if (operation === "go_forward") await page.goForward(options);
    else await page.reload(options);
    return complete({ url: page.url() });
  }
  if (operation === "close") {
    await page.close();
    return complete();
  }
  if (operation === "evaluate") {
    requireValue(
      typeof q.source === "string",
      "Function-expression string required",
    );
    // Evaluation is intentionally not raced with a timer: Rust owns the deadline
    // and keeps this operation fenced until the evaluation actually settles.
    const result = await page.evaluate(
      ({ source, arg }) => {
        const fn = (0, eval)(`(${source})`);
        if (typeof fn !== "function")
          throw new Error("Expected function expression");
        return fn(arg);
      },
      { source: q.source, arg: q.arg ?? null },
    );
    const assertJSON = (v: any): void => {
      if (v === null || typeof v === "string" || typeof v === "boolean") return;
      if (typeof v === "number" && Number.isFinite(v)) return;
      if (Array.isArray(v)) {
        v.forEach(assertJSON);
        return;
      }
      if (
        v &&
        typeof v === "object" &&
        Object.getPrototypeOf(v) === Object.prototype
      ) {
        Object.values(v).forEach(assertJSON);
        return;
      }
      throw new ProviderError(
        "ACTION_OUTCOME_UNKNOWN",
        "Evaluation finished but its result is not JSON-safe; effects may have occurred",
      );
    };
    assertJSON(result);
    return { value: result, ...complete() };
  }
  if (operation === "dialogs_set_rules") {
    requireValue(Array.isArray(q.rules), "Rules must be an array");
    for (const r of q.rules) {
      requireValue(
        ["accept", "dismiss"].includes(r.action) &&
          (!r.type ||
            ["alert", "confirm", "prompt", "beforeunload"].includes(r.type)),
        "Invalid dialog rule",
      );
      if (r.message !== undefined)
        requireValue(typeof r.message === "string", "Message must be a string");
    }
    tab.dialogs = structuredClone(q.rules);
    return complete();
  }
  if (operation === "upload") {
    const target = await tab.identity.resolve(q.node_id);
    requireValue(Array.isArray(q.files), "Files must be an array");
    const files = q.files.map((f: Request) => {
      requireValue(
        typeof f.name === "string" && typeof f.data === "string",
        "Named byte payload required",
      );
      return {
        name: f.name,
        mimeType: f.mime_type ?? "application/octet-stream",
        buffer: Buffer.from(f.data, "base64"),
      };
    });
    await target.handle.setInputFiles(files, {
      timeout: q.timeout_ms,
      signal: q.signal,
    });
    return complete();
  }
  if (operation === "downloads_list")
    return { downloads: [...tab.downloads.values()].map(publicDownload) };
  if (operation === "downloads_get") {
    const entry = tab.downloads.get(q.id);
    if (!entry) throw new ProviderError("TARGET_NOT_FOUND");
    return publicDownload(entry);
  }
  if (operation === "artifact_read") {
    const entry = tab.downloads.get(q.id);
    if (!entry || entry.state !== "completed")
      throw new ProviderError("ARTIFACT_EXPIRED");
    requireValue(
      Number.isInteger(q.offset ?? 0) && (q.offset ?? 0) >= 0,
      "Invalid offset",
    );
    const offset = q.offset ?? 0;
    const max = q.max_bytes ?? 1048576;
    requireValue(
      Number.isInteger(max) && max > 0 && max <= 1048576,
      "Read limit is 1 MiB",
    );
    const file = await open(entry.path, "r");
    try {
      const { size } = await file.stat();
      const data = Buffer.alloc(Math.min(max, Math.max(0, size - offset)));
      const { bytesRead } = await file.read(data, 0, data.length, offset);
      return {
        data: data.subarray(0, bytesRead).toString("base64"),
        eof: offset + bytesRead >= size,
        size,
      };
    } finally {
      await file.close();
    }
  }
  if (operation.startsWith("storage_")) {
    requireValue(
      ["local", "session"].includes(q.area),
      "Storage area must be local or session",
    );
    const value = await page.evaluate((q) => {
      const store = q.area === "local" ? localStorage : sessionStorage;
      if (q.operation === "storage_read")
        return q.key == null
          ? Object.fromEntries(
              Array.from({ length: store.length }, (_, i) => store.key(i)!).map(
                (k) => [k, store.getItem(k)],
              ),
            )
          : store.getItem(q.key);
      if (q.operation === "storage_set") {
        if (typeof q.key !== "string" || typeof q.value !== "string")
          throw new Error("Key and value must be strings");
        store.setItem(q.key, q.value);
      } else if (q.operation === "storage_remove") store.removeItem(q.key);
      else if (q.operation === "storage_clear") store.clear();
      return null;
    }, q);
    return operation === "storage_read" ? { value } : { ...complete() };
  }
  throw new ProviderError(
    "UNSUPPORTED_CAPABILITY",
    `Unknown browser operation ${operation}`,
  );
}
async function request(q: Request) {
  if (q.op === "init") {
    requireValue(!browser, "Already initialized");
    const engine = ({ chromium, firefox, webkit } as const)[
      q.engine as "chromium"
    ];
    requireValue(engine, "Unknown engine");
    contextId = q.context_id;
    ownsArtifactDirectory = !q.artifact_directory;
    artifacts =
      q.artifact_directory ??
      (await mkdtemp(join(tmpdir(), "mai-ui-browser-")));
    const contextOptions = {
      viewport: q.viewport ?? { width: 1280, height: 720 },
      deviceScaleFactor: q.device_scale_factor ?? 1,
      serviceWorkers: q.service_workers ?? "block",
      proxy: q.proxy ?? undefined,
      locale: q.locale ?? undefined,
      userAgent: q.user_agent ?? undefined,
      acceptDownloads: true,
      storageState: q.storage_state ?? undefined,
    };
    const backgroundTabs = q.background_launch === true && q.engine === "chromium";
    try {
      if (backgroundTabs && process.platform === "darwin" && q.headless === false) {
        const launched = await launchBackgroundChromium(q.channel, q.timeout_ms ?? 30000);
        browser = launched.browser;
        closeBackgroundBrowser = launched.close;
      } else {
        browser = await engine.launch({
          headless: q.headless ?? true,
          channel: q.channel ?? undefined,
          timeout: q.timeout_ms ?? 30000,
        });
      }
      if (backgroundTabs) {
        const isolated = await createIsolatedBackgroundContext(browser, contextOptions);
        context = isolated.context;
        backgroundCdp = isolated.cdp;
        backgroundContextId = isolated.contextId;
      } else {
        context = await browser.newContext(contextOptions);
      }
    } catch (error) {
      if (closeBackgroundBrowser) await closeBackgroundBrowser().catch(() => {});
      else await browser?.close().catch(() => {});
      throw error;
    }
    context.on("page", (page) => addTab(page));
    context.on("request", (r) =>
      event("network_request", {
        url: r.url(),
        method: r.method(),
        resource_type: r.resourceType(),
      }),
    );
    context.on("response", (r) =>
      event("network_response", { url: r.url(), status: r.status() }),
    );
    context.on("requestfailed", (r) =>
      event("network_failed", { url: r.url(), error: r.failure() }),
    );
    await context.route("**/*", routeRequest);
    return { ready: true };
  }
  if (q.op === "describe") return describe();
  if (q.op === "observe") return capture(getTab(q.tab_id), q);
  if (q.op === "action") return action(getTab(q.tab_id), q);
  if (q.op === "browser") return browserOperation(q);
  if (q.op === "cancel") {
    operations.get(q.operation_id)?.abort();
    return { cancel_requested: true };
  }
  if (q.op === "release_inputs") {
    for (const tab of tabs.values()) if (!tab.page.isClosed() && (!q.tab_id || q.tab_id === tab.id)) await releaseInput(tab.page);
    return complete();
  }
  if (q.op === "stop") {
    await shutdown();
    return { stopped: true };
  }
  throw new ProviderError(
    "UNSUPPORTED_CAPABILITY",
    `Unknown operation ${q.op}`,
  );
}
async function shutdown() {
  if (stopping) return;
  stopping = true;
  if (closeBackgroundBrowser) await closeBackgroundBrowser().catch(() => {});
  else await browser?.close().catch(() => {});
  await Promise.all([...downloadTasks]);
  if (artifacts && ownsArtifactDirectory)
    await rm(artifacts, { recursive: true, force: true });
}
const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  let q: Request;
  try {
    q = JSON.parse(line);
    requireValue(Number.isSafeInteger(q._request_id), "Request ID required");
  } catch (e) {
    process.stderr.write(String(e) + "\n");
    return;
  }
  const abort = new AbortController();
  operations.set(q._operation_id ?? String(q._request_id), abort);
  Object.defineProperty(q, "signal", { value: abort.signal });
  void request(q)
    .then(
      (result) => send({ _request_id: q._request_id, result }),
      (error) => {
        const known = error instanceof ProviderError;
        send({
          _request_id: q._request_id,
          error: {
            code: known
              ? error.code
              : error?.name === "TimeoutError"
                ? "ACTION_OUTCOME_UNKNOWN"
                : "PROVIDER_ERROR",
            message: String(error?.message ?? error),
            details: known ? error.details : undefined,
          },
        });
      },
    )
    .finally(() => operations.delete(q._operation_id ?? String(q._request_id)));
});
input.on("close", () => void shutdown().finally(() => process.exit(0)));
process.on("SIGTERM", () => void shutdown().finally(() => process.exit(0)));
process.on("SIGINT", () => void shutdown().finally(() => process.exit(0)));
