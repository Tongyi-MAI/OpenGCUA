import { contract } from "./playwright-contract.js";
import { ProviderError, requireValue } from "./errors.js";
import { browserInput, buttonHeld } from "./input.js";
export function encodeValue(v) {
    if (v === undefined)
        return { t: "undefined" };
    if (Buffer.isBuffer(v))
        return { t: "bytes", v: v.toString("base64") };
    if (v === null || typeof v === "string" || typeof v === "boolean")
        return { t: "value", v };
    if (typeof v === "number" && Number.isFinite(v))
        return { t: "value", v };
    if (Array.isArray(v))
        return { t: "array", v: Array.from(v, encodeValue) };
    if (v && Object.getPrototypeOf(v) === Object.prototype)
        return {
            t: "object",
            v: Object.entries(v).map(([k, x]) => [k, encodeValue(x)]),
        };
    throw new ProviderError("ACTION_OUTCOME_UNKNOWN", "Unsupported Playwright result; effects may have occurred");
}
function decode(v, page, depth = 0) {
    requireValue(v && depth < 64, "Invalid extension value");
    switch (v.t) {
        case "undefined":
            return undefined;
        case "value":
            requireValue(v.v === null || ["string", "boolean", "number"].includes(typeof v.v), "Invalid scalar");
            return v.v;
        case "function":
            throw new ProviderError("UNSUPPORTED_CAPABILITY", "Callbacks are supported only as the first evaluate/waitForFunction argument");
        case "regexp":
            return new RegExp(v.v, v.flags);
        case "array":
            return v.v.map((x) => decode(x, page, depth + 1));
        case "object":
            return Object.fromEntries(v.v.map(([k, x]) => [k, decode(x, page, depth + 1)]));
        case "locator":
            return resolve(page, v.v, depth + 1);
        default:
            throw new ProviderError("INVALID_ARGUMENT", "Invalid extension value type");
    }
}
function validateArgs(spec, args, builder = false) {
    requireValue(Array.isArray(args) && args.length >= spec.min && args.length <= spec.max, "Unsupported argument count");
    const index = spec.option_index ?? (builder ? 1 : spec.min);
    const options = args[index];
    if (spec.options !== null && options !== undefined) {
        requireValue(options && typeof options === "object" && !Array.isArray(options), "Expected options object");
        for (const key of Object.keys(options))
            if (!spec.options.includes(key))
                throw new ProviderError("UNSUPPORTED_CAPABILITY", `Unsupported option: ${key}`);
    }
}
function resolve(page, query, depth = 0) {
    requireValue(Array.isArray(query) && query.length <= 64 && depth < 64, "Invalid locator description");
    let target = page;
    for (const step of query) {
        const spec = contract.builders[step.method];
        requireValue(Object.hasOwn(contract.builders, step.method), "Unsupported locator builder");
        const args = step.args.map((v) => decode(v, page, depth + 1));
        validateArgs(spec, args, true);
        requireValue(typeof target[step.method] === "function", "Builder is not available on this scope");
        target = target[step.method](...args);
    }
    return target;
}
export async function executePlaywright(page, q) {
    const spec = contract.methods[q.kind]?.[q.method];
    requireValue(spec && Object.hasOwn(contract.methods[q.kind], q.method), "Unsupported Playwright method");
    const args = q.args.map((v, i) => {
        if (v.t === "function" && i === 0 && q.kind === "page" && ["evaluate", "waitForFunction"].includes(q.method)) {
            requireValue(typeof v.v === "string", "Invalid callback source");
            return v.v;
        }
        return decode(v, page);
    });
    validateArgs(spec, args);
    const opts = spec.options !== null ? (args[spec.option_index] ?? {}) : {};
    if (opts.delay !== undefined)
        requireValue(Number.isFinite(opts.delay) &&
            opts.delay >= 0 &&
            opts.delay + 100 < q.timeout_ms, "Delay exceeds remaining budget");
    if (opts.steps !== undefined)
        requireValue(Number.isInteger(opts.steps) && opts.steps >= 1 && opts.steps <= 1000, "steps must be 1..1000");
    let target = q.kind === "locator"
        ? resolve(page, q.query)
        : q.kind === "mouse"
            ? page.mouse
            : q.kind === "keyboard"
                ? page.keyboard
                : page;
    if (spec.options !== null && spec.options.includes("timeout")) {
        const index = spec.option_index;
        const options = args[index] ?? {};
        if (options.timeout !== undefined)
            requireValue(Number.isFinite(options.timeout) && options.timeout >= 0, "Invalid timeout");
        args[index] = {
            ...options,
            timeout: Math.min(options.timeout || q.timeout_ms, q.timeout_ms),
            signal: q.signal,
        };
    }
    if (q.method === "waitForTimeout") {
        requireValue(Number.isFinite(args[0]) && args[0] >= 0 && args[0] < q.timeout_ms, "Wait exceeds budget");
        await new Promise((resolve, reject) => {
            const done = () => {
                q.signal?.removeEventListener("abort", abort);
                resolve();
            };
            const timer = setTimeout(done, args[0]);
            const abort = () => {
                clearTimeout(timer);
                reject(new ProviderError("CANCELLED"));
            };
            q.signal?.addEventListener("abort", abort, { once: true });
            if (q.signal?.aborted)
                abort();
        });
        return encodeValue(undefined);
    }
    if (q.background_launch && q.kind === "locator" && q.method === "click") {
        const needsForeground = await target.evaluate((element) => {
            const link = element.closest("a");
            if (link?.target.toLowerCase() === "_blank")
                return true;
            const submitter = element.closest("button, input[type=submit], input[type=image]");
            if (!submitter ||
                (submitter instanceof HTMLButtonElement && submitter.type !== "submit"))
                return false;
            const form = submitter.form;
            if (!form || form.target.toLowerCase() !== "_blank")
                return false;
            const path = new URL(form.action, location.href).pathname;
            const search = form.method.toLowerCase() === "get" &&
                (/(^|\/)search(\/|$)/i.test(path) || form.getAttribute("role") === "search");
            if (!search)
                return true;
            form.target = "_self";
            return false;
        });
        if (needsForeground)
            throw new ProviderError("ACTION_REJECTED", "FOREGROUND_TAKEOVER_REQUIRED: This action opens a new tab and may activate Chrome. Ask the user before foreground takeover, then use a visible browser.");
    }
    if (q.kind === "page" && ["evaluate", "waitForFunction"].includes(q.method)) {
        requireValue(typeof args[0] === "string", "Provide a JavaScript function-expression source");
        const evaluate = ({ source, arg }) => {
            const value = (0, eval)(`(${source})`);
            return typeof value === "function" ? value(arg) : value;
        };
        const payload = { source: args[0], arg: args[1] };
        if (q.method === "evaluate")
            return encodeValue(await page.evaluate(evaluate, payload));
        const handle = await page.waitForFunction(evaluate, payload, args[2]);
        try {
            return encodeValue(await handle.jsonValue());
        }
        finally {
            await handle.dispose();
        }
    }
    if (q.kind === "locator" && q.method === "press") {
        if (q.background_launch && ["Enter", "NumpadEnter"].includes(args[0])) {
            const needsForeground = await target.evaluate((element) => {
                if (!(element instanceof HTMLInputElement))
                    return false;
                const form = element.form;
                if (!form || form.target.toLowerCase() !== "_blank")
                    return false;
                const path = new URL(form.action, location.href).pathname;
                const search = form.method.toLowerCase() === "get" &&
                    ["text", "search"].includes(element.type) &&
                    (/(^|\/)search(\/|$)/i.test(path) || form.getAttribute("role") === "search");
                if (!search)
                    return true;
                // Native submission of a search form may otherwise create a foreground tab.
                form.target = "_self";
                return false;
            });
            if (needsForeground)
                throw new ProviderError("ACTION_REJECTED", "FOREGROUND_TAKEOVER_REQUIRED: This form opens a new tab and may activate Chrome. Ask the user before foreground takeover, then use a visible browser.");
        }
        await target.focus({ timeout: q.timeout_ms });
        return encodeValue(await browserInput(page, "keyboard", "press", args, q.signal));
    }
    if (q.kind === "locator" &&
        ["click", "dblclick", "check", "uncheck", "setChecked"].includes(q.method))
        requireValue(!buttonHeld(page, args[spec.option_index]?.button ?? "left"), "Release held button before a composite click");
    if (q.kind === "page" &&
        ["goto", "goBack", "goForward", "reload"].includes(q.method)) {
        const response = await target[q.method](...args);
        return response
            ? {
                t: "response",
                v: {
                    url: response.url(),
                    status: response.status(),
                    statusText: response.statusText(),
                    headers: response.headers(),
                    ok: response.ok(),
                },
            }
            : encodeValue(null);
    }
    return encodeValue(await (q.kind === "mouse" || q.kind === "keyboard"
        ? browserInput(page, q.kind, q.method, args, q.signal)
        : target[q.method](...args)));
}
