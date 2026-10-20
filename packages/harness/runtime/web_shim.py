"""Local Playwright implementation of the SDK `web` browser resource.

Why this exists
---------------
The SDK browser route goes through the Rust core's browser endpoint
(`session.browsers.create(name=..., provider=...)`). On hosts where that endpoint
rejects the call (observed: `MuError: INVALID_ARGUMENT` raised from
composition::compose -> authority.begin, while `add_browser_factory` itself succeeds)
the harness silently degrades to `web=None`, and every web task then falls back to
vision+AX on the user's own Chrome - exactly what MAI_UI_SYSTEM_PROMPT forbids.

This module keeps the *model-visible* contract identical while driving a local
Playwright chromium. It does not modify the SDK: it is an alternative backend with
the same method names, keyword arguments, return shapes and semantics.

Contract preserved (verbatim from MAI_UI_SYSTEM_PROMPT -> "网页浏览器 web")::

    tab = await web.browser.new_tab(url)
    pg  = tab.playwright
    await pg.goto(url)
    await pg.locator(sel).fill(text) / .click() / .press("Enter")
    await pg.evaluate(js)
    await pg.wait_for_load_state()
    await tab.close()
    await web.browser.storage_state()
    web = await reset_web() / reset_web(visible=True)

Two-layer timeouts are preserved: `timeout=` is forwarded to Playwright (internal
wait), `timeout_ms=` bounds the whole SDK-side operation (deadline).

Return shapes: action-shaped calls return the SDK's own `ActionReceipt`; read-shaped
calls return the Playwright value. Anything outside the documented contract raises
`MuError("UNSUPPORTED_CAPABILITY")` - the SDK's own convention - rather than
inventing new behaviour.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import os
import sys
import time
from typing import Any, Optional

try:  # reuse the SDK's own receipt type so field names cannot drift
    from mai_ui.unified.runtime import ActionReceipt
except Exception:  # noqa: BLE001 - fall back to an identical local shape
    from dataclasses import dataclass

    @dataclass
    class ActionReceipt:  # type: ignore[no-redef]
        action_id: str
        sequence: int
        session_id: str
        attachment_id: str
        resource_id: str
        provider: str
        execution: str
        effect: str
        duration_ms: Optional[int] = None
        error: Optional[dict] = None
        based_on: Optional[str] = None
        mechanism: Optional[str] = None
        delivery: Any = None
        evidence: Any = None

try:
    from mai_ui import MuError
except Exception:  # noqa: BLE001
    class MuError(Exception):  # type: ignore[no-redef]
        def __init__(self, code, message=""):
            super().__init__(f"{code}: {message}")
            self.code = code


# Read-shaped calls return the Playwright value; everything else is an action and
# returns a receipt (mirrors Browser._call(read=...) vs the action path in the SDK).
_READ = frozenset({
    "evaluate", "content", "title", "url", "text_content", "inner_text", "inner_html",
    "all_text_contents", "get_attribute", "input_value", "count", "is_checked",
    "is_visible", "is_enabled", "is_hidden", "is_disabled", "screenshot",
    "wait_for_load_state", "wait_for_url", "wait_for_selector", "wait_for_timeout",
    "wait_for_function", "storage_state", "list_tabs", "get_tab", "viewport_size",
    "is_closed", "frame_locator", "get_by_role", "get_by_text", "get_by_label",
    "query_selector", "query_selector_all", "locator", "first", "last", "nth",
    "element_handle", "pages", "cookies", "read",
})
_DEFAULT_DEADLINE_MS = 30_000          # normal ops (SDK corrected default)
_NAV_DEADLINE_MS = 45_000              # navigation ops (SDK playwright layer default)
_NAV = frozenset({"goto", "new_tab", "go_back", "go_forward", "reload",
                  "wait_for_url", "wait_for_load_state"})
# Hard ceiling for a single call. The harness kills a step at ~300s and resets the whole
# Python session, so one long wait (e.g. networkidle on a page with long-polling
# telemetry, which never goes idle) must not be able to consume the entire step budget.
# 60s, not 120s: a single code block often issues several web calls, and the harness
# kills a step at ~300s and resets the whole Python session. 60s keeps six calls per
# block inside the budget while still matching the SDK's own defaults (30s normal,
# 45s navigation).
_MAX_DEADLINE_MS = int(os.environ.get("MAC_AGENT_WEB_MAX_DEADLINE_MS", "60000"))


def _deadline(name: str, requested: Optional[int], default_ms: int) -> int:
    want = int(requested) if requested else default_ms
    if want > _MAX_DEADLINE_MS:
        print(f"[web-shim] clamping {name} timeout_ms={want} -> {_MAX_DEADLINE_MS} "
              f"(a single call must not consume the whole step budget)")
        return _MAX_DEADLINE_MS
    return max(1, want)


_seq = itertools.count(1)


def _uid() -> str:
    return f"webshim{int(time.time() * 1000):x}{next(_seq):x}"


_TIMEOUT_OK: dict[tuple[str, str], bool] = {}


def _accepts_timeout(fn: Any) -> bool:
    """Only inject Playwright's own `timeout=` where the method really takes it.

    Several page/locator accessors (title(), url, content() on some versions) do not,
    and passing it raises TypeError. The SDK-level deadline is still enforced by the
    outer asyncio.wait_for, so dropping the inner option loses nothing.
    """
    key = (type(fn).__name__, getattr(fn, "__name__", "?"))
    hit = _TIMEOUT_OK.get(key)
    if hit is None:
        try:
            params = inspect.signature(fn).parameters
            hit = "timeout" in params or any(
                p.kind is p.VAR_KEYWORD for p in params.values())
        except (TypeError, ValueError):
            hit = False
        _TIMEOUT_OK[key] = hit
    return hit


def _first_param_is_timeout(fn: Any, args: tuple) -> bool:
    """True when a positional arg already fills a parameter named `timeout`.

    Playwright's wait_for_timeout(timeout) is the canonical case: injecting our own
    `timeout=` keyword there raises "got multiple values for argument 'timeout'".
    """
    if not args:
        return False
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    return bool(params) and params[0].name == "timeout"


def _receipt(action_id: str, started: float, error: Optional[dict] = None,
             effect: str = "applied") -> ActionReceipt:
    return ActionReceipt(
        action_id=action_id, sequence=next(_seq), session_id="web-shim",
        attachment_id="web-shim", resource_id="browser:web-shim", provider="web",
        execution="playwright-local", effect=effect,
        duration_ms=int((time.perf_counter() - started) * 1000), error=error)


_PROXY_TYPES = frozenset({"Mouse", "Keyboard", "Touchscreen", "Frame", "Locator",
                          "ElementHandle", "FileChooser"})


class _Proxy:
    """Forward attribute access to a Playwright object, keeping the SDK's calling
    convention: accept `timeout_ms=` as an operation deadline and `action_id=` for
    side-effecting calls, and hand back a receipt for actions."""

    def __init__(self, target: Any, tab: "_Tab | None" = None):
        object.__setattr__(self, "_t", target)
        object.__setattr__(self, "_tab", tab)

    def __getattr__(self, name: str) -> Any:
        target = object.__getattribute__(self, "_t")
        tab = object.__getattribute__(self, "_tab")
        attr = getattr(target, name)
        if not callable(attr):
            # Sub-objects (pg.mouse / pg.keyboard / pg.main_frame / a Locator) must be
            # proxied as well: returned raw, their calls bypass every deadline, and an
            # observed mouse.wheel() on a busy page hung the whole 300s step.
            if type(attr).__name__ in _PROXY_TYPES:
                return _Proxy(attr, tab)
            return attr
        is_read = name in _READ
        default_ms = _NAV_DEADLINE_MS if name in _NAV else _DEFAULT_DEADLINE_MS

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if tab is not None and tab._stale:
                raise MuError("STALE_TARGET", tab._stale)
            action_id = kwargs.pop("action_id", None)
            deadline = kwargs.pop("timeout_ms", None)
            inject = (deadline is None and "timeout" not in kwargs
                      and _accepts_timeout(attr) and not _first_param_is_timeout(attr, args))
            if inject:
                # the SDK deadline also caps the Playwright wait, so a slow site fails
                # with a deadline instead of hanging the worker
                kwargs["timeout"] = min(default_ms, 45_000)
            try:
                coro = attr(*args, **kwargs)
            except TypeError:
                # e.g. a method whose first positional parameter IS `timeout`
                if not inject:
                    raise
                kwargs.pop("timeout", None)
                coro = attr(*args, **kwargs)
            if not inspect.isawaitable(coro):
                if name in ("locator", "first", "last", "nth", "frame_locator",
                            "get_by_role", "get_by_text", "get_by_label"):
                    return _Proxy(coro, tab)
                return coro
            return _run(coro, action_id,
                        _deadline(name, deadline, default_ms),
                        is_read, name, tab)

        return wrapper

    def __repr__(self) -> str:
        return f"<web-shim proxy {type(object.__getattribute__(self, '_t')).__name__}>"


async def _run(coro, action_id: Optional[str], deadline_ms: int, is_read: bool,
               name: str, tab: "_Tab | None") -> Any:
    """Await a Playwright call under the SDK deadline, cancelling it on expiry.

    The harness drains pending asyncio tasks at the end of every code block
    (code_runtime.py: `_drain_code_tasks()`, else `RuntimeError("后台任务未在截止时间内
    结束，必须重置 worker")`). Leaving a timed-out call running therefore freezes the
    worker until the 300s step kill - observed in a live run. So we cancel, wait for the
    cancellation to settle (nothing may stay pending), and then invalidate the tab and
    force the CDP connection to be rebuilt on next use, because cancelling an in-flight
    protocol call can desynchronise the shared connection pipe.
    """
    started = time.perf_counter()
    aid = action_id or _uid()
    task = asyncio.ensure_future(coro)
    done, _pending = await asyncio.wait({task}, timeout=max(1.0, deadline_ms / 1000))
    if not done:
        task.cancel()
        await asyncio.wait({task}, timeout=10)      # leave nothing pending for the drain
        err = {"code": "DEADLINE_EXCEEDED",
               "message": f"{name} exceeded timeout_ms={deadline_ms}; the tab was abandoned "
                          f"and the browser connection will be rebuilt - open a new tab"}
        if tab is not None:
            tab._mark_stale(err["message"])
        if is_read:
            raise MuError("DEADLINE_EXCEEDED", err["message"]) from None
        return _receipt(aid, started, error=err, effect="unknown")
    try:
        value = task.result()
    except Exception as exc:  # noqa: BLE001 - surfaced as a receipt error, like the SDK
        err = {"code": type(exc).__name__.upper(), "message": str(exc)[:400]}
        if is_read:
            raise
        return _receipt(aid, started, error=err, effect="failed")
    if is_read:
        return value
    return _receipt(aid, started)


class _Cookies:
    def __init__(self, ctx):
        self._ctx = ctx

    async def read(self, urls=None, *, timeout_ms=30000):
        return await self._ctx.cookies(urls) if urls else await self._ctx.cookies()

    async def set(self, cookies, *, timeout_ms=30000, action_id=None):
        started = time.perf_counter()
        await self._ctx.add_cookies(cookies)
        return _receipt(action_id or _uid(), started)

    async def clear(self, filter=None, *, timeout_ms=30000, action_id=None):
        started = time.perf_counter()
        await self._ctx.clear_cookies()
        return _receipt(action_id or _uid(), started)


class _Unsupported:
    """Network/dialog rule injection has no local equivalent that matches the SDK's
    declarative rule semantics; say so instead of pretending."""

    def __init__(self, what: str):
        self._what = what

    async def set_rules(self, rules, *, timeout_ms=30000, action_id=None):
        raise MuError("UNSUPPORTED_CAPABILITY",
                      f"web shim: {self._what}.set_rules is not implemented")


class _Browser:
    def __init__(self, playwright, browser, ctx, name="web", launch_kwargs=None, cdp=None):
        self._pw = playwright
        self._browser = browser
        self._ctx = ctx
        self._launch_kwargs = dict(launch_kwargs or {})
        self._cdp = cdp            # when set, the browser lives in the guest (do not kill)
        self._wedged = False       # a cancelled call desynced the pipe; rebuild on next use
        self.name = name
        self.cookies = _Cookies(ctx)
        self.network = _Unsupported("browser.network")
        self._tabs: dict[str, _Tab] = {}

    async def _ensure_alive(self) -> None:
        """Relaunch/reconnect when the browser died or a cancelled call wedged it.

        new_page() on a closed browser does not raise - it hangs, which cost a whole
        300s step and a session reset. Reconnecting keeps `web` usable across a
        close/reuse cycle instead of forcing the agent to call reset_web().
        """
        try:
            alive = self._browser.is_connected() and not self._wedged
        except Exception:  # noqa: BLE001
            alive = False
        if alive:
            return
        print(f"[web-shim] rebuilding browser connection (wedged={self._wedged})")
        self._wedged = False
        from playwright.async_api import async_playwright
        kw = dict(self._launch_kwargs)
        state = kw.pop("storage_state", None)
        headless = kw.pop("headless", True)
        try:
            await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        pw = await async_playwright().start()
        if self._cdp:
            browser = await pw.chromium.connect_over_cdp(self._cdp)
            ctx = await self._fresh_context(browser, state)
        else:
            browser = await pw.chromium.launch(
                headless=headless, args=["--disable-blink-features=AutomationControlled"])
            ctx_kwargs = {"viewport": {"width": 1440, "height": 900}, "locale": "zh-CN"}
            if state and os.path.isfile(state):
                ctx_kwargs["storage_state"] = state
            ctx = await browser.new_context(**ctx_kwargs)
        self._pw, self._browser = pw, browser
        self._ctx = ctx
        self.cookies = _Cookies(self._ctx)
        self._tabs.clear()

    @staticmethod
    async def _fresh_context(browser, storage_state=None):
        """Always open an isolated context in CDP mode.

        Reusing the guest Chrome's default context inherits whatever pages are still
        running there; after a worker reset those are heavy half-loaded Amazon/IHG tabs,
        and new_page() then hangs until the step budget dies (observed: every later
        new_tab returned DEADLINE_EXCEEDED, even for about:blank). A fresh context is
        isolated from them. Trade-off: cookies are not written back to the Chrome
        profile, so login persistence relies on the injected storage_state snapshot.
        """
        kwargs: dict = {"viewport": {"width": 1440, "height": 900}, "locale": "zh-CN"}
        if storage_state and os.path.isfile(storage_state):
            kwargs["storage_state"] = storage_state
        return await browser.new_context(**kwargs)

    async def new_tab(self, url: str = "about:blank", *, timeout_ms: int = _NAV_DEADLINE_MS,
                      action_id: Optional[str] = None) -> "_Tab":
        await self._ensure_alive()
        # new_page() must be bounded too: if the browser is still busy with a call whose
        # deadline we stopped waiting on, an unbounded new_page() hangs and costs the
        # whole 300s step (observed: step timed out with empty stdout).
        try:
            page = await _run(self._ctx.new_page(), action_id,
                              _deadline("new_page", timeout_ms, _NAV_DEADLINE_MS),
                              True, "new_page", None)
        except MuError:
            self._wedged = True        # rebuild the connection on the next attempt
            raise
        tab = _Tab(self, page, url)
        self._tabs[tab.tab_id] = tab
        if url and url != "about:blank":
            await _run(page.goto(url, wait_until="domcontentloaded"), action_id,
                       _deadline("new_tab", timeout_ms, _NAV_DEADLINE_MS),
                       False, "new_tab", tab)
        return tab

    async def list_tabs(self, *, timeout_ms: int = 30000):
        from types import SimpleNamespace
        tabs = []
        for t in list(self._tabs.values()):
            page = t.playwright_raw
            if page.is_closed():
                continue
            try:
                title = await _run(page.title(), None,
                                   _deadline("title", 10000, 10000), True, "title", None)
            except Exception:  # noqa: BLE001 - a crashed tab must not break the inventory
                title = ""
            tabs.append(SimpleNamespace(id=t.tab_id, resource_id="browser:web-shim",
                                        title=title, url=page.url, kind="tab",
                                        viewport=None))
        return SimpleNamespace(tabs=tuple(tabs), complete=True)

    async def get_tab(self, id, *, timeout_ms: int = 30000) -> "_Tab":
        tab = self._tabs.get(id)
        if tab is None:
            raise MuError("TARGET_NOT_FOUND", f"web shim: unknown tab {id}")
        return tab

    async def storage_state(self, *, timeout_ms: int = 30000) -> dict:
        return await _run(self._ctx.storage_state(), None,
                          _deadline("storage_state", timeout_ms, _DEFAULT_DEADLINE_MS),
                          True, "storage_state", None)

    async def close(self, *, timeout_ms: int = 30000) -> None:
        """Best-effort teardown; a hang here must not block worker shutdown.

        In CDP mode the browser belongs to the guest, so `browser.close()` only drops
        the connection - but the LOCAL playwright driver must still be stopped, or its
        dispatcher task stays pending and the harness's end-of-block drain fails
        ("后台任务未在截止时间内结束"), resetting the worker.
        """
        for step in (self._ctx.close(), self._browser.close(), self._pw.stop()):
            task = asyncio.ensure_future(step)
            await asyncio.wait({task}, timeout=max(1.0, timeout_ms / 1000))
            if not task.done():
                task.cancel()
                await asyncio.wait({task}, timeout=5)   # never leave it pending
            elif task.exception() is not None:
                pass                                   # teardown errors are not fatal


class _Tab:
    def __init__(self, browser: _Browser, page, url: str = "about:blank"):
        self._browser = browser
        self._page = page
        self._id = _uid()
        self._stale: Optional[str] = None
        self.playwright = _Proxy(page, self)
        self.dialogs = _Unsupported("tab.dialogs")
        self.downloads = _Unsupported("tab.downloads")
        self.storage = _Storage(page)

    def _mark_stale(self, reason: str) -> None:
        """Abandon this tab and force the browser connection to be rebuilt.

        Called when a call had to be cancelled at its deadline: the cancellation can
        desynchronise the shared CDP pipe, so failing fast on this tab (instead of
        letting every later call burn another deadline) is what keeps a step alive.
        """
        self._stale = reason
        self._browser._wedged = True

    def _check(self) -> None:
        if self._stale:
            raise MuError("STALE_TARGET", self._stale)

    # --- Device-ish identity attributes the harness may read ---
    @property
    def id(self) -> str:
        return self._id

    @property
    def resource_id(self) -> str:
        return "browser:web-shim"

    @property
    def name(self) -> str:
        return "web"

    @property
    def tab_id(self) -> str:
        return self._id

    @property
    def playwright_raw(self):
        """Escape hatch for the shim itself (no proxying)."""
        return self._page

    async def goto(self, url, *, wait_until="domcontentloaded",
                   timeout_ms: int = _NAV_DEADLINE_MS, action_id=None):
        self._check()
        return await _run(self._page.goto(url, wait_until=wait_until), action_id,
                          _deadline("goto", timeout_ms, _NAV_DEADLINE_MS),
                          False, "goto", self)

    async def go_back(self, *, wait_until="domcontentloaded",
                      timeout_ms: int = _NAV_DEADLINE_MS, action_id=None):
        return await _run(self._page.go_back(wait_until=wait_until), action_id,
                          _deadline("go_back", timeout_ms, _NAV_DEADLINE_MS),
                          False, "go_back", self)

    async def go_forward(self, *, wait_until="domcontentloaded",
                         timeout_ms: int = _NAV_DEADLINE_MS, action_id=None):
        return await _run(self._page.go_forward(wait_until=wait_until), action_id,
                          _deadline("go_forward", timeout_ms, _NAV_DEADLINE_MS),
                          False, "go_forward", self)

    async def reload(self, *, wait_until="domcontentloaded",
                     timeout_ms: int = _NAV_DEADLINE_MS, action_id=None):
        return await _run(self._page.reload(wait_until=wait_until), action_id,
                          _deadline("reload", timeout_ms, _NAV_DEADLINE_MS),
                          False, "reload", self)

    async def close(self, *, timeout_ms: int = 30000, action_id=None):
        # unbounded page.close() was observed to hang when a previous call was still
        # in flight, which cost the whole step (300s) and reset the Python session;
        # drop the tab from the inventory first, then close under the same deadline.
        self._browser._tabs.pop(self._id, None)
        if self._page.is_closed():
            return _receipt(action_id or _uid(), time.perf_counter())
        return await _run(self._page.close(), action_id,
                          _deadline("close", timeout_ms, 30000), False, "close", self)

    async def evaluate(self, source, arg=None, *, timeout_ms: int = 30000, action_id=None):
        self._check()
        return await _run(self._page.evaluate(source, arg), action_id,
                          _deadline("evaluate", timeout_ms, _DEFAULT_DEADLINE_MS),
                          True, "evaluate", self)

    async def upload(self, target, files, *, timeout_ms: int = 30000, action_id=None):
        raise MuError("UNSUPPORTED_CAPABILITY",
                      "web shim: tab.upload needs the SDK's AX node wire")


class _Storage:
    def __init__(self, page):
        self._page = page

    async def read(self, key=None, *, area="local", timeout_ms=30000):
        fn = "(k) => (k == null ? JSON.parse(JSON.stringify(" \
             f"window.{'localStorage' if area == 'local' else 'sessionStorage'})) : " \
             f"window.{'localStorage' if area == 'local' else 'sessionStorage'}.getItem(k))"
        return await self._page.evaluate(fn, key)

    async def set(self, key, value, *, area="local", timeout_ms=30000, action_id=None):
        started = time.perf_counter()
        store = "localStorage" if area == "local" else "sessionStorage"
        await self._page.evaluate(f"([k, v]) => window.{store}.setItem(k, v)", [key, value])
        return _receipt(action_id or _uid(), started)

    async def remove(self, key, *, area="local", timeout_ms=30000, action_id=None):
        started = time.perf_counter()
        store = "localStorage" if area == "local" else "sessionStorage"
        await self._page.evaluate(f"(k) => window.{store}.removeItem(k)", key)
        return _receipt(action_id or _uid(), started)

    async def clear(self, *, area="local", timeout_ms=30000, action_id=None):
        started = time.perf_counter()
        store = "localStorage" if area == "local" else "sessionStorage"
        await self._page.evaluate(f"() => window.{store}.clear()")
        return _receipt(action_id or _uid(), started)


class _Web:
    """Stands in for the SDK browser Device handle: `web.browser.*`."""

    def __init__(self, browser: _Browser):
        self.browser = browser
        self.id = "web-shim"
        self.name = "web"
        self.resource_id = "browser:web-shim"

    async def close(self, *, timeout_ms: int = 30000) -> None:
        await self.browser.close()


async def create_web(*, storage_state: Optional[str] = None,
                     headless: Optional[bool] = None,
                     visible: bool = False) -> _Web:
    """Launch a local chromium and return the `web`-shaped handle.

    `storage_state` is the same login snapshot path the SDK route uses, so saved
    logins keep working across sessions.
    """
    from playwright.async_api import async_playwright

    cdp = os.environ.get("MAC_AGENT_WEB_CDP")
    if headless is None:
        headless = False
    if visible:
        headless = False
    if (not cdp and not headless and sys.platform.startswith("linux")
            and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")):
        # a headless server has no X/Wayland display; a windowed launch would fail
        print("[web-shim] no DISPLAY on linux; forcing headless chromium")
        headless = True
    pw = await async_playwright().start()
    try:
        if cdp:
            # Drive the guest's real Chrome: sites such as Amazon/IHG block by client
            # fingerprint, and a real Chrome is the only client that gets through from
            # this network (measured 4/4 vs 1/3 for a locally launched chromium).
            browser = await pw.chromium.connect_over_cdp(cdp)
            ctx = await _Browser._fresh_context(browser, storage_state)
            print(f"[web-shim] attached to guest chrome over CDP: {cdp}")
        else:
            browser = await pw.chromium.launch(
                headless=headless, args=["--disable-blink-features=AutomationControlled"])
            ctx_kwargs = {"viewport": {"width": 1440, "height": 900}, "locale": "zh-CN"}
            if storage_state and os.path.isfile(storage_state):
                ctx_kwargs["storage_state"] = storage_state
            ctx = await browser.new_context(**ctx_kwargs)
    except Exception:
        await pw.stop()
        raise
    return _Web(_Browser(pw, browser, ctx, cdp=cdp,
                         launch_kwargs={"headless": headless,
                                        "storage_state": storage_state}))
