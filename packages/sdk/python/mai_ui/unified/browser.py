"""Portable browser handles; execution and lifecycle belong to the Rust host."""

import base64
from dataclasses import dataclass

from .._errors import MuError
from .resources import Device
from .runtime import ActionReceipt, AttachmentInfo, call, state, uid
from .services import FileRef


class BrowserFactories:
    def __init__(self, session):
        self._session = session

    async def create(
        self,
        *,
        name,
        provider,
        lifetime="session",
        storage_state=None,
        timeout_ms=30000,
        action_id=None,
    ):
        mu = self._session
        result = await call(
            mu._runtime._core,
            dict(
                op="browser_create",
                session=mu.id,
                name=name,
                provider=provider,
                lifetime=lifetime,
                storage_state=storage_state,
                timeout_ms=timeout_ms,
                action_id=action_id,
            ),
        )
        info = mu._runtime._cache_endpoint(result["resource"])
        endpoint = mu._runtime._endpoints[info.id]
        handle = Device(mu, endpoint, AttachmentInfo(**result["attachment"]))
        mu._devices[handle.id] = handle
        return handle


@dataclass(frozen=True)
class TabInfo:
    id: str
    resource_id: str
    title: str
    url: str
    kind: str = "tab"
    viewport: dict | None = None


@dataclass(frozen=True)
class TabInventory:
    tabs: tuple[TabInfo, ...]
    complete: bool = True


class Browser:
    def __init__(self, handle):
        self._handle = handle
        self.cookies = Cookies(self)
        self.network = Network(self)

    async def _call(self, operation, *, timeout_ms=5000, action_id=None, read=False, **args):
        h = self._handle
        wire = dict(op="browser", operation=operation, timeout_ms=timeout_ms, **args)
        if not read:
            action_id = action_id or uid()
            wire["action_id"] = action_id
        result = await h._backend._request(wire)
        if read:
            return result
        return ActionReceipt(**await h._session.action_status(action_id))

    def _tab(self, value):
        tab = Tab(
            self._handle._session,
            self._handle._endpoint,
            self._handle._info,
            tab_id=value["id"],
            anchor=self._handle._anchor,
        )

        tab.playwright._tabs.setdefault(value["id"], {}).update(value)
        return tab

    async def list_tabs(self, *, timeout_ms=5000):
        result = await self._call("list_tabs", read=True, timeout_ms=timeout_ms)
        return TabInventory(tuple(TabInfo(**t) for t in result["tabs"]), result["complete"])

    async def get_tab(self, id, *, timeout_ms=5000):
        result = await self._call("get_tab", id=id, read=True, timeout_ms=timeout_ms)
        return self._tab(result)

    async def new_tab(self, url="about:blank", *, timeout_ms=30000, action_id=None):
        h = self._handle
        result = await h._backend._request(
            dict(
                op="browser",
                operation="new_tab",
                url=url,
                timeout_ms=timeout_ms,
                action_id=action_id or uid(),
            )
        )
        return self._tab(result["tab"])

    async def storage_state(self, *, timeout_ms=5000):
        return await self._call("storage_state", read=True, timeout_ms=timeout_ms)


class Tab(Device):
    def __init__(self, *args, **options):
        super().__init__(*args, **options)
        self._browser_ops = Browser(self)
        self.dialogs = Dialogs(self._browser_ops)
        self.downloads = Downloads(self._browser_ops)
        self.storage = Storage(self._browser_ops)

    @property
    def tab_id(self):
        return self._tab_id

    async def goto(self, url, *, wait_until="domcontentloaded", timeout_ms=30000, action_id=None):
        return await self._browser_ops._call(
            "goto", url=url, wait_until=wait_until, timeout_ms=timeout_ms, action_id=action_id
        )

    async def go_back(self, *, wait_until="domcontentloaded", timeout_ms=30000, action_id=None):
        return await self._browser_ops._call(
            "go_back", wait_until=wait_until, timeout_ms=timeout_ms, action_id=action_id
        )

    async def go_forward(self, *, wait_until="domcontentloaded", timeout_ms=30000, action_id=None):
        return await self._browser_ops._call(
            "go_forward", wait_until=wait_until, timeout_ms=timeout_ms, action_id=action_id
        )

    async def reload(self, *, wait_until="domcontentloaded", timeout_ms=30000, action_id=None):
        return await self._browser_ops._call(
            "reload", wait_until=wait_until, timeout_ms=timeout_ms, action_id=action_id
        )

    async def close(self, *, timeout_ms=5000, action_id=None):
        return await self._browser_ops._call("close", timeout_ms=timeout_ms, action_id=action_id)

    async def evaluate(self, source, arg=None, *, timeout_ms=5000, action_id=None):
        result = await self._backend._request(
            dict(
                op="browser",
                operation="evaluate",
                source=source,
                arg=arg,
                timeout_ms=timeout_ms,
                action_id=action_id or uid(),
            )
        )
        return result["value"]

    async def upload(self, target, files, *, timeout_ms=5000, action_id=None):
        ref = self.ui.ax._node(target)
        wire = state(
            self._session._runtime._core,
            op="node_wire",
            scope=self._scope,
            id=ref.observation_id,
            node=ref.id,
        )
        payloads = []
        for file in files:
            if isinstance(file, FileRef):
                h = file._files._handle
                payloads.append(
                    dict(source=dict(attachment=h.id, resource=h.resource_id, path=file.path))
                )
            elif (
                isinstance(file, dict)
                and isinstance(file.get("data"), bytes)
                and isinstance(file.get("name"), str)
            ):
                payloads.append(
                    dict(
                        name=file["name"],
                        mime_type=file.get("mime_type", "application/octet-stream"),
                        data=base64.b64encode(file["data"]).decode(),
                    )
                )
            else:
                raise MuError("INVALID_ARGUMENT", "Use a FileRef or named bytes payload")
        action_id = action_id or uid()
        await call(
            self._session._runtime._core,
            dict(
                op="upload_files",
                session=self._session.id,
                attachment=self.id,
                resource=self.resource_id,
                wire={"tab_id": self.tab_id, **wire},
                files=payloads,
                timeout_ms=timeout_ms,
                action_id=action_id,
            ),
        )
        return ActionReceipt(**await self._session.action_status(action_id))


class Cookies:
    def __init__(self, browser):
        self._browser = browser

    async def read(self, urls=None, *, timeout_ms=5000):
        options = {} if urls is None else dict(urls=urls)
        return (
            await self._browser._call("cookies_read", read=True, timeout_ms=timeout_ms, **options)
        )["cookies"]

    async def set(self, cookies, *, timeout_ms=5000, action_id=None):
        return await self._browser._call(
            "cookies_set", cookies=cookies, timeout_ms=timeout_ms, action_id=action_id
        )

    async def clear(self, filter=None, *, timeout_ms=5000, action_id=None):
        return await self._browser._call(
            "cookies_clear", filter=filter, timeout_ms=timeout_ms, action_id=action_id
        )


class Network:
    def __init__(self, browser):
        self._browser = browser

    async def set_rules(self, rules, *, timeout_ms=5000, action_id=None):
        return await self._browser._call(
            "network_set_rules", rules=rules, timeout_ms=timeout_ms, action_id=action_id
        )


class Dialogs(Network):
    async def set_rules(self, rules, *, timeout_ms=5000, action_id=None):
        return await self._browser._call(
            "dialogs_set_rules", rules=rules, timeout_ms=timeout_ms, action_id=action_id
        )


class Storage:
    def __init__(self, browser):
        self._browser = browser

    async def read(self, key=None, *, area="local", timeout_ms=5000):
        return (
            await self._browser._call(
                "storage_read", key=key, area=area, read=True, timeout_ms=timeout_ms
            )
        )["value"]

    async def set(self, key, value, *, area="local", timeout_ms=5000, action_id=None):
        return await self._browser._call(
            "storage_set",
            key=key,
            value=value,
            area=area,
            timeout_ms=timeout_ms,
            action_id=action_id,
        )

    async def remove(self, key, *, area="local", timeout_ms=5000, action_id=None):
        return await self._browser._call(
            "storage_remove", key=key, area=area, timeout_ms=timeout_ms, action_id=action_id
        )

    async def clear(self, *, area="local", timeout_ms=5000, action_id=None):
        return await self._browser._call(
            "storage_clear", area=area, timeout_ms=timeout_ms, action_id=action_id
        )


class Downloads:
    def __init__(self, browser):
        self._browser = browser

    async def list(self, *, timeout_ms=5000):
        return (await self._browser._call("downloads_list", read=True, timeout_ms=timeout_ms))[
            "downloads"
        ]

    async def get(self, id, *, timeout_ms=5000):
        data = await self._browser._call("downloads_get", id=id, read=True, timeout_ms=timeout_ms)
        return Download(self._browser, data)


class Download:
    def __init__(self, browser, data):
        self._browser = browser
        for key, value in data.items():
            setattr(self, key, value)

    async def read(self, *, offset=0, max_bytes=1048576, timeout_ms=5000):
        value = await self._browser._call(
            "artifact_read",
            id=self.id,
            offset=offset,
            max_bytes=max_bytes,
            read=True,
            timeout_ms=timeout_ms,
        )
        return dict(value, data=base64.b64decode(value["data"]))
