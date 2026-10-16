import type { Page } from "playwright";
import { ProviderError } from "./errors.js";

const states = new WeakMap<Page, { keys: Set<string>; buttons: Set<string> }>();
function canonical(key: string) {
  return key === "ControlOrMeta"
    ? process.platform === "darwin"
      ? "Meta"
      : "Control"
    : key;
}
function state(page: Page) {
  let value = states.get(page);
  if (!value)
    states.set(page, (value = { keys: new Set(), buttons: new Set() }));
  return value;
}
export function buttonHeld(page: Page, button = "left") {
  return state(page).buttons.has(button);
}
export async function releaseInput(page: Page) {
  const held = state(page);
  const failures = [];
  for (const key of [...held.keys]) {
    try {
      await page.keyboard.up(key);
      held.keys.delete(key);
    } catch (error) {
      failures.push(error);
    }
  }
  for (const button of [...held.buttons]) {
    try {
      await page.mouse.up({ button: button as any });
      held.buttons.delete(button);
    } catch (error) {
      failures.push(error);
    }
  }
  if (failures.length)
    throw new ProviderError(
      "ACTION_OUTCOME_UNKNOWN",
      "Input release could not be confirmed",
    );
}
export async function pause(ms: number, signal?: AbortSignal) {
  await new Promise<void>((resolve, reject) => {
    const finish = () => {
      signal?.removeEventListener("abort", abort);
      resolve();
    };
    const timer = setTimeout(finish, ms);
    const abort = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      reject(new ProviderError("CANCELLED"));
    };
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
  });
}
export async function browserInput(
  page: Page,
  kind: string,
  method: string,
  args: any[],
  signal?: AbortSignal,
) {
  const held = state(page);
  if (kind === "keyboard" && method === "press") {
    // Match Playwright's chord spelling, including the literal plus key.
    const parts: string[] = [];
    let token = "";
    for (const char of args[0]) {
      if (char === "+" && token) {
        parts.push(token);
        token = "";
      } else token += char;
    }
    parts.push(token);
    const key = parts.pop()!;
    if (held.keys.has(canonical(key)))
      throw new ProviderError(
        "INPUT_REFUSED",
        "Release held key before pressing it",
      );
    const owned: string[] = [];
    try {
      for (const modifier of parts)
        if (!held.keys.has(canonical(modifier))) {
          owned.push(modifier);
          await browserInput(page, "keyboard", "down", [modifier]);
        }
      owned.push(key);
      await browserInput(page, "keyboard", "down", [key]);
      if (args[1]?.delay) await pause(args[1].delay, signal);
    } finally {
      let failed = false;
      for (const key of owned.reverse()) {
        try {
          await browserInput(page, "keyboard", "up", [key]);
        } catch {
          failed = true;
        }
      }
      if (failed)
        throw new ProviderError("ACTION_OUTCOME_UNKNOWN", "Key release failed");
    }
    return;
  }
  const set = kind === "mouse" ? held.buttons : held.keys;
  const key =
    kind === "mouse" ? (args[0]?.button ?? "left") : canonical(args[0]);
  if (method === "down") set.add(key);
  if (
    kind === "mouse" &&
    ["click", "dblclick"].includes(method) &&
    held.buttons.has(args[2]?.button ?? "left")
  )
    throw new ProviderError(
      "INPUT_REFUSED",
      "Release the held button before clicking it",
    );
  const value = await (kind === "mouse" ? page.mouse : page.keyboard)[
    method as "down"
  ](...(args as [any]));
  if (method === "up") set.delete(key);
  return value;
}
