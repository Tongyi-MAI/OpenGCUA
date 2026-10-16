"""Bound resource handles; capabilities are explicit and scope dependent."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields

from .._errors import MuError
from ..types import AppInfo, AppInventory, DisplayInfo
from .runtime import native

_APPINFO_FIELDS = frozenset(field.name for field in fields(AppInfo))


def _app_info(row):
    """Project a provider app row onto AppInfo, tolerating schema drift.

    Adapters that predate the running/pid projection only surface live apps, so a
    missing ``running`` is inferred from a recoverable pid, and the pid itself is
    parsed back out of the ``app:<pid>:<start>`` identity the macOS adapter mints.
    Unknown provider keys are dropped rather than raising.
    """
    known = {k: v for k, v in row.items() if k in _APPINFO_FIELDS}
    app_id = known.get("id") or ""
    # macOS apps_launch resolves to a bare {"id": ...}; fill the required identity
    # fields so a just-launched app still binds (it is launchable, label unknown).
    known.setdefault("label", app_id)
    known.setdefault("launchable", True)
    pid = known.get("pid")
    if pid is None and app_id.startswith("app:"):
        parts = app_id.split(":")
        if len(parts) >= 2 and parts[1].isdigit():
            pid = known["pid"] = int(parts[1])
    if "running" not in known:
        known["running"] = pid is not None
    return AppInfo(**known)


class UnsupportedNamespace:
    def __init__(self, name):
        self.name = name

    def __getattr__(self, method):
        raise MuError("UNSUPPORTED_CAPABILITY", f"{self.name} is WIP in the Python/Android release")


class _BackendSession:
    """Bound transport and Python value cache; authority and capture state are Rust-owned."""

    def __init__(self, handle):
        self.handle = handle
        self.epoch = handle._session.id
        self._description = handle._endpoint.description
        self._observations = {}

    @property
    def _stopped(self):
        return self.handle._session._closed or self.handle._anchor._detached

    async def capture(self, *, tree, screenshot=False, timeout_ms=1500):
        from ._capture import Captured

        data = await self._request(
            dict(
                op="observe", tree=tree, screenshot=screenshot, mode="latest", timeout_ms=timeout_ms
            )
        )
        return Captured(self.handle, data, tree=tree)

    async def _request(self, message):
        return await self.handle._session._request(self.handle, message)

    async def _action(self, action, *, action_id=None, timeout_ms=3000, based_on=None, **args):
        from .runtime import ActionReceipt, uid

        aid = action_id or uid()
        if "x" in args and "node_id" not in args:
            # Pixels are grounded in an image, not an AX snapshot. Keeping an
            # unrelated AX revision on the wire invites adapters to reject valid
            # visual input after ordinary scene updates.
            args.pop("revision", None)
        await self._request(
            dict(op="action", action=action, action_id=aid, timeout_ms=timeout_ms, **args)
        )
        result = await self.handle._session.action_status(aid)
        return ActionReceipt(**{**result, "based_on": based_on})


class Device:
    def __init__(
        self,
        session,
        endpoint,
        info,
        *,
        app_id=None,
        window_id=None,
        display_id=None,
        tab_id=None,
        anchor=None,
    ):
        self._session, self._endpoint, self._info = session, endpoint, info
        self._app_id, self._window_id, self._display_id = app_id, window_id, display_id
        self._tab_id = tab_id
        self.platform = endpoint.info.platform
        self._anchor = anchor or self
        self._detached = False
        if anchor is None:
            self._handles = []
        self._anchor._handles.append(self)
        self.id, self.name = info.id, info.name
        self.resource_id = (
            endpoint.info.id
            + ("/" + app_id if app_id else "")
            + ("/" + window_id if window_id else "")
            + ("/" + display_id if display_id else "")
            + ("/" + tab_id if tab_id else "")
        )
        self._scope = f"{self.id}/{app_id or 'device'}/{window_id or display_id or tab_id or ''}"
        self._ax_values, self._image_values = {}, {}
        self._backend = _BackendSession(self)
        from .services import Clipboard, Files, Processes, Shell
        from .ui import UI

        self.ui = UI(self)
        self.apps = Apps(self) if not (app_id or display_id) else UnsupportedNamespace("app.apps")
        self.displays = (
            Displays(self) if not (app_id or display_id) else UnsupportedNamespace("app.displays")
        )
        self.shell = Shell(self)
        self.processes = Processes(self)
        self.files = Files(self)
        self.android = (
            Android(self) if self.platform == "android" else UnsupportedNamespace("android")
        )
        for name in ("browser", "macos", "ios", "windows_os", "linux", "clipboard", "windows"):
            setattr(self, name, UnsupportedNamespace(name))
        if self.platform == "linux":
            self.apps = UnsupportedNamespace("linux.apps")
            self.clipboard = Clipboard(self)
        if self.platform == "browser":
            from .browser import Browser
            from .playwright import PlaywrightContext, PlaywrightPage

            self.browser = Browser(self) if not tab_id else UnsupportedNamespace("tab.browser")
            self.playwright = PlaywrightPage(self) if tab_id else PlaywrightContext(self)
        if self.platform == "macos":
            self.windows = (
                Windows(self)
                if not (display_id or window_id)
                else UnsupportedNamespace("scope.windows")
            )
            self.macos = Macos(self)
            self.clipboard = Clipboard(self)

    async def destroy(self, *, timeout_ms=5000, action_id=None):
        if self.platform != "browser" or self._tab_id:
            raise MuError("UNSUPPORTED_CAPABILITY")
        return await self.browser._call("destroy", timeout_ms=timeout_ms, action_id=action_id)

    def _view(self, key):
        return json.loads(self._session._runtime._core.views(self._scope)).get(key)

    @property
    def _baseline(self):
        return self._ax_values.get(self._view("baseline"))

    @property
    def _presented_ax(self):
        return self._ax_values.get(self._view("ax"))

    @property
    def _turn_ax(self):
        return self._ax_values.get(self._view("turn_ax"))

    def _images(self, key):
        ids = self._view("turn_images" if key == "turn_image" else "images") or []
        return {id: self._image_values[id] for id in ids if id in self._image_values}

    @property
    def _presented_images(self):
        return self._images("image")

    @property
    def _turn_images(self):
        return self._images("turn_image")

    def _check(self, *, write=False):
        self._session._check()
        if self._anchor._detached:
            raise MuError("STALE_ATTACHMENT")
        # With auto-reconnect the offline gate is advisory: Session._request owns
        # the rebuild-and-retry, so defer instead of failing this sync precheck.
        defer = getattr(self._session, "_auto_reconnect", False)
        try:
            native(self._session._runtime._core.check, self.id, write)
            offline = self._endpoint.offline
        except MuError as error:
            if not (defer and error.code == "DEVICE_OFFLINE"):
                raise
            return
        if offline and not defer:
            raise MuError("DEVICE_OFFLINE")

    async def capabilities(self):
        self._check()
        description = await self._backend._request({"op": "describe", "timeout_ms": 5000})
        self._endpoint.description = description
        self._backend._description = description
        from .runtime import state

        return state(
            self._session._runtime._core,
            op="capabilities",
            attachment=self.id,
            description=description,
            routing=dict(
                app_id=self._app_id,
                window_id=self._window_id,
                display_id=self._display_id,
                tab_id=self._tab_id,
            ),
        )

    def _require(self, capability):
        self._check()
        if (
            self._endpoint.description.get("capabilities", {}).get(capability, {}).get("state")
            != "ready"
        ):
            raise MuError("UNSUPPORTED_CAPABILITY", capability)

    def _whole_device(self):
        self._check()
        if self._app_id or self._display_id:
            raise MuError(
                "UNSUPPORTED_CAPABILITY", "this service requires a whole-device attachment"
            )


class App(Device):
    @property
    def app_id(self):
        return self._app_id

    @property
    def _identity(self):
        """Resolved AppInfo when this handle came from Apps.get/launch; else None."""
        return getattr(self, "_app_info", None)

    @property
    def _pid_from_id(self):
        # The macOS adapter mints identities as ``app:<pid>:<start>``; recover the
        # pid so handles bound through Session.register still expose it.
        parts = (self._app_id or "").split(":")
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1])
        return None

    @property
    def label(self):
        info = self._identity
        return info.label if info is not None else (self._app_id or "")

    @property
    def displayName(self):
        return self.label

    @property
    def native_id(self):
        info = self._identity
        return (info.native_id if info is not None else None) or self._app_id

    @property
    def pid(self):
        info = self._identity
        if info is not None and info.pid is not None:
            return info.pid
        return self._pid_from_id

    @property
    def running(self):
        info = self._identity
        if info is not None:
            return bool(info.running)
        return self.pid is not None

    @property
    def launch_path(self):
        info = self._identity
        return info.launch_path if info is not None else None

    async def launch(self, *, timeout_ms=None, action_id=None):
        """Ensure this app is running and frontmost (v1 App.launch contract)."""
        return await self.activate(timeout_ms=timeout_ms, action_id=action_id)

    async def activate(self, *, timeout_ms=None, action_id=None):
        timeout_ms = (
            (20000 if self.platform == "macos" else 5000) if timeout_ms is None else timeout_ms
        )
        self._require("app.launch")
        return await self._backend._action(
            "activate" if self.platform == "macos" else "launch",
            app_id=self._app_id,
            timeout_ms=timeout_ms,
            action_id=action_id,
        )


class Apps:
    def __init__(self, device):
        self._device = device
        self._cache = {}

    async def list(self, *, timeout_ms=5000, refresh=False):
        """Capture app metadata silently; mu.emit renders compact, usable app labels.

        ``refresh`` is accepted for compatibility and ignored: every call already
        re-queries the provider, so there is no stale cache to bypass.
        """
        self._device._require("device.list_apps")
        data = await self._device._backend._request(
            dict(op="list_apps", launchable_only=True, timeout_ms=timeout_ms)
        )
        return AppInventory(
            apps=tuple(_app_info(row) for row in data["apps"]), complete=data["complete"]
        )

    async def get(self, id_or_name, *, timeout_ms=5000):
        import dataclasses

        from .runtime import state

        state(self._device._session._runtime._core, op="normalize_app", name=id_or_name)
        inventory = await self.list(timeout_ms=timeout_ms)
        found = _app_info(
            state(
                self._device._session._runtime._core,
                op="match_app",
                name=id_or_name,
                inventory=dataclasses.asdict(inventory),
            )
        )
        return self._bind(found)

    async def launch(self, id_or_name, *, timeout_ms=5000, action_id=None):
        """Resolve and launch through the shared host with one total deadline."""
        from .runtime import call

        d = self._device
        d._require("app.launch")
        found = await call(d._session._runtime._core, dict(
            op="apps_launch", session=d._session.id, attachment=d.id,
            resource=d.resource_id, name=id_or_name, timeout_ms=timeout_ms, action_id=action_id,
        ))
        if "label" not in found:
            # macOS apps_launch resolves to a bare {"id": ...}; re-resolve so the bound
            # App carries a real label/native_id instead of the raw identity string.
            try:
                return await self.get(found["id"], timeout_ms=timeout_ms)
            except MuError:
                pass
        return self._bind(_app_info(found))

    def _bind(self, found):
        app = self._cache.get(found.id)
        if app is None:
            d = self._device
            app = self._cache[found.id] = App(
                d._session, d._endpoint, d._info, app_id=found.id, anchor=d._anchor
            )
        app._app_info = found
        return app


class Displays:
    def __init__(self, device):
        self._device = device
        self._cache = {}

    async def list(self, *, timeout_ms=1500):
        self._device._require("device.list_displays")
        reply = await self._device._backend._request(dict(op="displays", timeout_ms=timeout_ms))
        return tuple(DisplayInfo(**d) for d in reply["displays"])

    def get(self, display_id):
        self._device._check()
        if display_id != "display:0":
            raise MuError("UNSUPPORTED_CAPABILITY", "only the primary display is implemented")
        if self._device.platform == "android":
            return self._device
        if display_id not in self._cache:
            h = self._device
            self._cache[display_id] = Display(
                h._session, h._endpoint, h._info, display_id=display_id, anchor=h._anchor
            )
        return self._cache[display_id]


class Display(Device):
    @property
    def display_id(self):
        return self._display_id


class Android:
    def __init__(self, device):
        self._device = device
        self.displays = UnsupportedNamespace("android.displays.create")

    async def press(self, button, *, timeout_ms=3000, action_id=None):
        self._device._whole_device()
        self._device._require("android.press")
        if button not in ("back", "home", "recents", "notifications", "quick_settings"):
            raise MuError("INVALID_ARGUMENT", "unknown Android system button")
        return await self._device._backend._action(
            "system", button=button, timeout_ms=timeout_ms, action_id=action_id
        )


@dataclass(frozen=True)
class NativeWindowInfo:
    id: str
    app_id: str
    title: str
    bounds: dict | None = None


@dataclass(frozen=True)
class NativeWindowInventory:
    windows: tuple[NativeWindowInfo, ...]
    complete: bool


class Window(App):
    @property
    def window_id(self):
        return self._window_id

    @property
    def _winfo(self):
        """NativeWindowInfo captured at bind time (title/bounds), when available."""
        return getattr(self, "_window_info", None)

    @property
    def title(self):
        info = self._winfo
        return info.title if info is not None else ""

    @property
    def bounds(self):
        info = self._winfo
        return info.bounds if info is not None else None


class Windows:
    def __init__(self, handle):
        self._handle = handle
        self._cache = {}

    async def list(self, *, timeout_ms=5000, refresh=False):
        # ``refresh`` is tolerated and ignored; list_windows always re-queries the driver.
        h = self._handle
        h._require("device.list_windows")
        data = await h._backend._request(dict(op="list_windows", timeout_ms=timeout_ms))
        return NativeWindowInventory(
            tuple(NativeWindowInfo(**w) for w in data["windows"]), data["complete"]
        )

    async def get(self, id, *, timeout_ms=5000):
        inventory = await self.list(timeout_ms=timeout_ms)
        matches = [w for w in inventory.windows if w.id == id]
        if len(matches) != 1:
            raise MuError("TARGET_NOT_FOUND")
        w = matches[0]
        win = self._cache.get(w.id)
        if win is None:
            h = self._handle
            win = self._cache[w.id] = Window(
                h._session, h._endpoint, h._info, app_id=w.app_id, window_id=w.id, anchor=h._anchor
            )
        win._window_info = w
        return win


class Macos:
    def __init__(self, handle):
        self._handle = handle

    async def set_frame(self, *, x, y, width, height, timeout_ms=5000, action_id=None):
        h = self._handle
        if not h._window_id:
            raise MuError("INVALID_ARGUMENT", "bind a window before changing its frame")
        h._require("macos.set_frame")
        return await h._backend._action(
            "set_frame",
            x=x,
            y=y,
            width=width,
            height=height,
            timeout_ms=timeout_ms,
            action_id=action_id,
        )

    async def invoke_menu(self, path, *, timeout_ms=5000, action_id=None):
        h = self._handle
        if (
            not h._app_id
            or not isinstance(path, (list, tuple))
            or not path
            or not all(isinstance(p, str) and p for p in path)
        ):
            raise MuError("INVALID_ARGUMENT", "bind an app and supply a nonempty menu path")
        h._require("macos.invoke_menu")
        return await h._backend._action(
            "invoke_menu", path=list(path), timeout_ms=timeout_ms, action_id=action_id
        )
