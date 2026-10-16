"""SDK-owned Playwright subset. Locators are descriptions, not native handles."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path

from .._errors import MuError
from .runtime import uid

CONTRACT = json.loads(Path(__file__).with_name("playwright-contract.json").read_text())


def snake(name):
    return re.sub(
        r"([a-z0-9])([A-Z])", r"\1_\2", re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    ).lower()


def camel(name):
    first, *rest = name.split("_")
    return first + "".join(s.title() for s in rest)


_UNDEFINED = object()


def encode(value, page):
    if value is _UNDEFINED:
        return dict(t="undefined")
    if isinstance(value, Locator):
        if value.page is not page:
            raise MuError("INVALID_ARGUMENT", "Locator belongs to another page")
        return dict(t="locator", v=value.query)
    if isinstance(value, re.Pattern):
        return dict(
            t="regexp",
            v=value.pattern,
            flags=("i" if value.flags & re.I else "")
            + ("m" if value.flags & re.M else "")
            + ("s" if value.flags & re.S else ""),
        )
    if value is None or type(value) in (str, bool, int, float):
        return dict(t="value", v=value)
    if isinstance(value, (list, tuple)):
        return dict(t="array", v=[encode(v, page) for v in value])
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return dict(t="object", v=[[k, encode(v, page)] for k, v in value.items()])
    raise MuError("INVALID_ARGUMENT", "Use JSON values, regular expressions, or same-page locators")


def decode(value):
    kind = value["t"]
    if kind == "undefined":
        return None
    if kind == "value":
        return value["v"]
    if kind == "response":
        from types import SimpleNamespace

        return SimpleNamespace(**{snake(k): v for k, v in value["v"].items()})
    if kind == "bytes":
        return base64.b64decode(value["v"])
    if kind == "array":
        return [decode(v) for v in value["v"]]
    if kind == "object":
        return {k: decode(v) for k, v in value["v"]}
    raise MuError("PROTOCOL_MISMATCH", "Invalid Playwright result")


class Remote:
    async def _call(self, kind, method, args, options=None):
        spec = CONTRACT["methods"][kind][method]
        options = dict(options or {})
        action_id = options.pop("action_id", None) or uid()
        default = (
            45000
            if method in ("goto", "goBack", "goForward", "reload", "waitForURL", "waitForLoadState")
            else 30000
        )
        timeout_ms = options.pop("timeout_ms", options.get("timeout", default)) or default
        if options:
            if spec["options"] is None:
                raise MuError("UNSUPPORTED_CAPABILITY", "Method does not accept options")
            while len(args) <= spec["option_index"]:
                args.append(_UNDEFINED)
            args[spec["option_index"]] = {camel(k): v for k, v in options.items()}
        result = await self.handle._backend._request(
            dict(
                op="browser",
                operation="playwright",
                kind=kind,
                method=method,
                args=[encode(v, getattr(self, "page", self)) for v in args],
                query=getattr(self, "query", []),
                timeout_ms=timeout_ms,
                action_id=action_id,
            )
        )
        if "page" in result:
            self._tabs[result["page"]["id"]] = result["page"]
        if "tabs" in result:
            for tab in self._tabs.values():
                tab["closed"] = True
            self._tabs.update({t["id"]: t for t in result["tabs"]})
        return decode(result["value"])


def state(handle):
    session = handle._session
    if not hasattr(session, "_playwright_states"):
        session._playwright_states = {}
    tabs = session._playwright_states.setdefault(handle._endpoint.info.id, {})
    if not hasattr(session, "_playwright_events"):

        async def update():
            try:
                async for event in session.events():
                    resource = event.get("resource_id", "")
                    payload = event.get("data") or {}
                    for endpoint, cache in session._playwright_states.items():
                        if not resource.startswith(endpoint + "/"):
                            continue
                        id = resource[len(endpoint) + 1 :]
                        old = cache.get(id, dict(id=id, url="about:blank", closed=False))
                        if event["type"] == "tab_created":
                            cache[id] = {**old, **payload}
                        elif event["type"] == "tab_closed":
                            cache[id] = {**old, "closed": True}
                        elif event["type"] == "frame_navigated" and payload.get("main_frame"):
                            cache[id] = {**old, "url": payload["url"]}
            except Exception:
                # The owning session closing also closes the event stream.
                return

        session._playwright_events = asyncio.create_task(update())
    return tabs


class PlaywrightPage(Remote):
    def __init__(self, handle):
        self.handle = handle
        self.page = self
        self._tabs = state(handle)
        self.mouse, self.keyboard = Mouse(self), Keyboard(self)

    @property
    def url(self):
        self.handle._check()
        return self._tabs.get(self.handle._tab_id, {}).get("url", "about:blank")

    @property
    def viewport_size(self):
        self.handle._check()
        return self._tabs.get(self.handle._tab_id, {}).get("viewport")

    def is_closed(self):
        self.handle._check()
        return self._tabs.get(self.handle._tab_id, {}).get("closed", False)


class PlaywrightContext(Remote):
    def __init__(self, handle):
        self.handle = handle
        self._tabs = state(handle)
        self._pages = {}

    def _page(self, id):
        if id not in self._pages:
            self._pages[id] = self.handle.browser._tab(dict(id=id)).playwright
        return self._pages[id]

    @property
    def pages(self):
        self.handle._check()
        return [self._page(id) for id, tab in self._tabs.items() if not tab.get("closed")]

    async def refresh(self):
        await self._call("context", "pages", [])
        return self.pages

    async def new_page(self, **options):
        return self._page(await self._call("context", "newPage", [], options))

    async def close(self, **options):
        await self._call("context", "close", [], options)


class Locator(Remote):
    def __init__(self, page, query):
        self.page, self.query = page, query
        self.handle, self._tabs = page.handle, page._tabs


class Mouse(Remote):
    def __init__(self, page):
        self.page, self.handle, self._tabs = page, page.handle, page._tabs


class Keyboard(Mouse):
    pass


def builder(method):
    def build(self, *args, **options):
        args = list(args)
        if options:
            args.append({camel(k): v for k, v in options.items()})
        return Locator(
            self.page,
            [
                *getattr(self, "query", []),
                dict(method=method, args=[encode(v, self.page) for v in args]),
            ],
        )

    return build


def terminal(kind, method):
    async def execute(self, *args, **options):
        return await self._call(kind, method, list(args), options)

    return execute


for cls in (PlaywrightPage, Locator):
    for name in CONTRACT["builders"]:
        setattr(
            cls,
            snake(name),
            property(builder(name)) if name in ("first", "last") else builder(name),
        )
for kind, cls in (
    ("page", PlaywrightPage),
    ("locator", Locator),
    ("mouse", Mouse),
    ("keyboard", Keyboard),
):
    for name in CONTRACT["methods"][kind]:
        setattr(cls, snake(name), terminal(kind, name))
