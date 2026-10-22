"""Host-owned catalog, session routing and asynchronous provider dispatch.

The Rust authority is shared by every session in this Runtime. The Android broker
also takes an OS lease so independent runtimes cannot silently become co-writers.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .. import _native
from .._errors import MuError
from ..adapters import Adapter


def uid():
    return uuid.uuid4().hex


def native(method, *args):
    try:
        return method(*args)
    except ValueError as error:
        raise decode_error(error) from error


def decode_error(error):
    try:
        data = json.loads(str(error))
    except (ValueError, TypeError):
        return MuError(str(error))
    details = data.get("details")
    receipt = (ActionReceipt(**details["receipt"])
               if isinstance(details, dict) and "receipt" in details else None)
    return MuError(data["code"], data.get("message", ""), receipt=receipt, details=details)


def encode(request):
    try:
        return json.dumps(request, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, OverflowError) as error:
        raise MuError("INVALID_ARGUMENT", "JSON-safe values required") from error


async def call(core, request):
    wire = encode(request)
    try:
        return json.loads(await core.call(wire))
    except ValueError as error:
        exc = decode_error(error)
        if exc.code == "CANCELLED_BEFORE_DISPATCH":
            raise asyncio.CancelledError() from error
        raise exc from error


def state(core, **request):
    return json.loads(native(core.state, encode(request)))


def timeout(value):
    if type(value) is not int or not 1 <= value <= 120000:
        raise MuError("INVALID_ARGUMENT", "timeout_ms must be an integer in 1..120000")
    return value / 1000


@dataclass(frozen=True)
class ResourceInfo:
    id: str
    name: str
    kind: str
    platform: str
    parent_id: str | None = None


@dataclass(frozen=True)
class Inventory:
    resources: tuple[ResourceInfo, ...]


@dataclass(frozen=True)
class AttachmentInfo:
    id: str
    name: str
    resource_id: str
    access: str


@dataclass(frozen=True)
class DeviceInventory:
    devices: tuple[AttachmentInfo, ...]


@dataclass(frozen=True)
class DetachReceipt:
    attachment_id: str
    state: str
    in_flight_drained: bool


@dataclass(frozen=True)
class StopReceipt:
    state: str
    queue_fenced: bool
    in_flight_drained: bool
    unresolved_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionReceipt:
    action_id: str
    sequence: int
    session_id: str
    attachment_id: str
    resource_id: str
    provider: str
    execution: str
    effect: str
    duration_ms: int | None = None
    error: dict | None = None
    based_on: str | None = None
    mechanism: str | None = None
    delivery: dict | None = None
    evidence: list | dict | None = None
    policy: str | None = None


class _Endpoint:
    def __init__(self, info, adapter, core):
        self.info, self.adapter = info, adapter
        self.description = {}
        self._offline = False
        self._core = core
        self.pump = None
        self._rebuild = None
        self._background_requests = set()
        self.leases = set()

    @property
    def offline(self):
        try:
            return self._offline or state(self._core, op="endpoint", id=self.info.id)["offline"]
        except MuError:
            return self._offline

    @offline.setter
    def offline(self, value):
        self._offline = value

    @property
    def offline_reason(self):
        try:
            return state(self._core, op="endpoint", id=self.info.id).get("offline_reason")
        except MuError as error:
            return str(error)


class _NativeAdapter:
    def __init__(self, core, resource_id, provider):
        self.core, self.resource_id, self.provider = core, resource_id, provider

    async def close(self):
        await call(self.core, dict(op="remove", id=self.resource_id))


class Runtime:
    """Trusted host configuration; agents receive Session, never this catalog owner."""

    def __init__(self):
        self._core = _native.RuntimeCore()
        self._endpoints = {}
        self._sessions = set()
        self._closed = False
        self._catalog_lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()

    def _cache_endpoint(self, data, adapter=None, rebuild=None):
        info = ResourceInfo(
            **{k: v for k, v in data.items() if k in ResourceInfo.__dataclass_fields__}
        )
        endpoint = _Endpoint(info, adapter, self._core)
        endpoint.description = state(self._core, op="endpoint", id=info.id)["description"]
        if adapter is None:
            endpoint.adapter = _NativeAdapter(self._core, info.id, data.get("provider", ""))
        endpoint._rebuild = rebuild
        self._endpoints[info.id] = endpoint
        return info

    async def add_android(self, serial: str, *, name: str) -> ResourceInfo:
        """Add one explicit ADB target, also usable alongside a web scrcpy preview.

        The host broker uses the local ADB server, so a browser preview should
        connect to that same server and serial. Streaming is independent of SDK
        observations: use this resource's UI handles to ground agent actions.
        """
        if not isinstance(serial, str) or not serial or serial != serial.strip() or any(c.isspace() for c in serial):
            raise MuError("INVALID_ARGUMENT", "An explicit ADB serial without whitespace is required")
        return self._cache_endpoint(
            await call(self._core, dict(op="add_android", serial=serial, name=name)),
            rebuild=("native", "add_android", dict(serial=serial, name=name)),
        )

    async def add_macos(self, *, name: str, app_path: str | None = None,
                        socket: str | None = None) -> ResourceInfo:
        """Connect to the installed driver or a signed, privately socketed companion."""
        options = dict(name=name)
        if app_path is not None:
            options["app_path"] = app_path
        if socket is not None:
            options["socket"] = socket
        return self._cache_endpoint(
            await call(self._core, dict(op="add_macos", **options)),
            rebuild=("native", "add_macos", options),
        )

    async def add_linux(
        self, *, name: str, display=None, xauthority=None, python=None
    ) -> ResourceInfo:
        return self._cache_endpoint(
            await call(
                self._core,
                dict(
                    op="add_linux",
                    name=name,
                    display=display,
                    xauthority=xauthority,
                    python=python,
                ),
            ),
            rebuild=(
                "native",
                "add_linux",
                dict(name=name, display=display, xauthority=xauthority, python=python),
            ),
        )

    async def add_browser_factory(
        self,
        name,
        *,
        engine,
        headless=True,
        viewport=None,
        device_scale_factor=1,
        channel=None,
        proxy=None,
        locale=None,
        user_agent=None,
        service_workers=None,
        connect_url=None,
        user_data_dir=None,
        background_launch=False,
    ):
        options = dict(
            op="add_browser_factory",
            name=name,
            engine=engine,
            headless=headless,
            background_launch=background_launch,
            device_scale_factor=device_scale_factor,
        )
        if viewport is not None:
            options["viewport"] = viewport
        options.update(
            {
                k: v
                for k, v in dict(
                    channel=channel,
                    proxy=proxy,
                    locale=locale,
                    user_agent=user_agent,
                    service_workers=service_workers,
                    connect_url=connect_url,
                    user_data_dir=user_data_dir,
                ).items()
                if v is not None
            }
        )
        state(self._core, **options)

    async def add_adapter(self, adapter: Adapter, *, name: str, resource_id: str) -> ResourceInfo:
        """Host-only adapter injection for implemented platform profiles."""
        async with self._catalog_lock:
            if self._closed:
                raise MuError("SESSION_CLOSED")
            if not name or not resource_id:
                raise MuError("INVALID_ARGUMENT")
            if resource_id in self._endpoints or any(
                e.info.name == name for e in self._endpoints.values()
            ):
                raise MuError("ALREADY_REGISTERED")
            if adapter.platform not in ("android", "macos"):
                raise MuError("UNSUPPORTED_CAPABILITY", f"{adapter.platform} adapter is WIP")
            endpoint = _Endpoint(
                ResourceInfo(resource_id, name, "device", adapter.platform), adapter, self._core
            )
            endpoint._rebuild = ("bridge", adapter, name, resource_id)
            try:
                await adapter.start()
                response = await adapter.request({"op": "describe", "timeout_ms": 5000})
                if "error" in response:
                    error = response["error"]
                    # 结构化拒绝不等于断线；复用统一解码并保留原始诊断。
                    if isinstance(error, dict) and isinstance(error.get("code"), str) and error["code"]:
                        raise decode_error(ValueError(encode(error)))
                    raise MuError("DEVICE_OFFLINE", str(error))
                endpoint.description = response["result"]
                if (
                    endpoint.description.get("platform") != adapter.platform
                    or endpoint.description.get("protocol_major") != 1
                ):
                    raise MuError("PROTOCOL_MISMATCH")
            except BaseException:
                await adapter.close()
                raise
            state(
                self._core,
                op="add_bridge",
                info=dict(
                    id=resource_id,
                    name=name,
                    kind="device",
                    platform=adapter.platform,
                    provider=adapter.provider,
                ),
                description=endpoint.description,
            )

            async def pump():
                active = endpoint._background_requests

                async def answer(request):
                    request_id = request.pop("_request_id")
                    try:
                        response = await adapter.request(request)
                    except MuError as error:
                        # 与 adapter 返回错误 Reply 等价，不把明确拒绝升级成结果未知或端点离线。
                        response = dict(error=dict(code=error.code, message=str(error), details=error.details))
                    except Exception as error:
                        response = dict(error=dict(code="DEVICE_OFFLINE", message=str(error)))
                    state(
                        self._core,
                        op="bridge_reply",
                        id=resource_id,
                        reply=dict(response, _request_id=request_id),
                    )

                try:
                    while True:
                        request = await call(self._core, dict(op="bridge_next", id=resource_id))
                        task = asyncio.create_task(answer(request))
                        active.add(task)
                        task.add_done_callback(active.discard)
                finally:
                    for task in active:
                        task.cancel()

            endpoint.pump = asyncio.create_task(pump())
            self._endpoints[resource_id] = endpoint
            return endpoint.info

    def session(self, *, output: Callable | None = None) -> Session:
        if self._closed:
            raise MuError("SESSION_CLOSED")
        session = Session(self, output=output)
        self._sessions.add(session)
        return session

    async def remove(self, resource: str, *, timeout_ms=5000):
        timeout(timeout_ms)
        endpoint = self._endpoints.get(resource)
        if endpoint is None:
            raise MuError("TARGET_NOT_FOUND")
        for session in tuple(self._sessions):
            for device in tuple(session._devices.values()):
                if device._endpoint is endpoint:
                    await session.unregister(device.id, timeout_ms=timeout_ms)
        await call(self._core, dict(op="remove", id=resource, timeout_ms=timeout_ms))
        if endpoint.pump:
            endpoint.pump.cancel()
            await endpoint.adapter.close()
        self._endpoints.pop(resource, None)

    async def reconnect(self, resource: str, *, timeout_ms=8000, force=False):
        """Trusted-host recovery for a fenced or offline endpoint.

        The Rust core never clears a latched ``offline`` flag or a scope fence, so
        recovery rebuilds the endpoint from its recorded factory and re-points the
        whole-device attachments that referenced it. Scoped handles (app/window/
        display/tab) cannot be transparently rebound because the driver mints fresh
        identities, so they stay detached for the agent to re-observe and rebind.
        """
        timeout(timeout_ms)
        async with self._reconnect_lock:
            if self._closed:
                raise MuError("SESSION_CLOSED")
            endpoint = self._endpoints.get(resource)
            if endpoint is None:
                raise MuError("TARGET_NOT_FOUND")
            if not force and not endpoint.offline:
                return endpoint.info
            rebuild = endpoint._rebuild
            if rebuild is None:
                raise MuError("UNSUPPORTED_CAPABILITY", "endpoint has no reconnect factory")
            survivors = [
                device
                for session in tuple(self._sessions)
                for device in tuple(session._devices.values())
                if device._endpoint is endpoint
            ]
            await self.remove(resource, timeout_ms=timeout_ms)
            if rebuild[0] == "native":
                _, op, kwargs = rebuild
                info = self._cache_endpoint(
                    await call(self._core, dict(op=op, **kwargs)), rebuild=rebuild
                )
            else:
                _, adapter, name, rid = rebuild
                info = await self.add_adapter(adapter, name=name, resource_id=rid)
            new_endpoint = self._endpoints[info.id]
            new_endpoint._rebuild = rebuild
            for device in survivors:
                await device._session._re_register(
                    device, new_endpoint, timeout_ms=timeout_ms
                )
            return info

    async def close(self, *, timeout_ms=5000):
        timeout(timeout_ms)
        if self._closed:
            return
        for session in tuple(self._sessions):
            await session.close(timeout_ms=timeout_ms)
        await call(self._core, dict(op="close", timeout_ms=timeout_ms))
        for endpoint in tuple(self._endpoints.values()):
            if endpoint.pump:
                endpoint.pump.cancel()
                await endpoint.adapter.close()
        self._endpoints.clear()
        self._closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


class Session:
    api_version = "0.3"

    def __init__(self, runtime, *, output=None):
        self.id = uid()
        state(runtime._core, op="session", id=self.id)
        self._runtime = runtime
        self._devices = {}
        self._closed = False
        self._close_receipt = None
        self._output = output
        self._emit_lock = asyncio.Lock()
        self._in_turn = False
        self._coordinate_space = "pixels"
        self._auto_reconnect = False
        self.image_sink = None  # Compatibility bridge for embedded Python workers.
        from .services import Transfers

        self.files = Transfers(self)
        from .browser import BrowserFactories

        self.browsers = BrowserFactories(self)

    @property
    def coordinate_space(self):
        return self._coordinate_space

    @coordinate_space.setter
    def coordinate_space(self, value):
        if value not in ("pixels", "normalized", "normalized_1000"):
            raise MuError("INVALID_ARGUMENT", "unknown coordinate space")
        self._coordinate_space = value

    @property
    def auto_reconnect(self):
        """When True, a DEVICE_OFFLINE request transparently rebuilds the endpoint.

        明确标记 scope_fenced 的临时阻塞不触发自动重建；原异常交由调用方处理。
        Only whole-device attachments survive the rebuild; scoped handles stay
        detached and must be rebound. Hosts opt in explicitly because a reconnect
        re-acquires the driver and drops in-flight scope state.
        """
        return self._auto_reconnect

    @auto_reconnect.setter
    def auto_reconnect(self, value):
        self._auto_reconnect = bool(value)

    def _check(self):
        if self._closed or self._runtime._closed:
            raise MuError("SESSION_CLOSED")

    async def discover(self, *, kind=None, query=None):
        self._check()
        result = state(self._runtime._core, op="discover", session=self.id, kind=kind, query=query)
        return Inventory(tuple(ResourceInfo(**r) for r in result["resources"]))

    async def register(self, resource, *, name, access="control"):
        self._check()
        from .browser import Tab
        from .resources import App, Device, Display, Window

        request = dict(op="register", session=self.id, name=name, access=access)
        if isinstance(resource, Device):
            resource._check()
            if resource._session._runtime is not self._runtime:
                raise MuError("INVALID_ARGUMENT", "foreign runtime resource")
            request.update(
                source_attachment=resource.id,
                routing=dict(
                    app_id=resource._app_id,
                    window_id=resource._window_id,
                    display_id=resource._display_id,
                    tab_id=resource._tab_id,
                ),
            )
        else:
            request["resource"] = resource.id if isinstance(resource, ResourceInfo) else resource
        result = state(self._runtime._core, **request)
        routing = result["routing"]
        cls = (
            Tab
            if routing.get("tab_id")
            else Window
            if routing.get("window_id")
            else App
            if routing.get("app_id")
            else Display
            if routing.get("display_id")
            else Device
        )
        endpoint = self._runtime._endpoints[result["resource"]["id"]]
        device = cls(self, endpoint, AttachmentInfo(**result["attachment"]), **routing)
        self._devices[device.id] = device
        endpoint.leases.add(device.id)
        return device

    def device(self, name_or_attachment_id):
        self._check()
        info = state(self._runtime._core, op="device", session=self.id, name=name_or_attachment_id)
        return self._devices[info["id"]]

    async def devices(self):
        result = state(self._runtime._core, op="devices", session=self.id)
        return DeviceInventory(tuple(AttachmentInfo(**d) for d in result["devices"]))

    async def unregister(self, name_or_attachment_id, *, timeout_ms=5000):
        timeout(timeout_ms)
        matches = [d for d in self._devices.values() if name_or_attachment_id in (d.id, d.name)]
        if not matches:
            raise MuError("STALE_ATTACHMENT")
        device = matches[0]
        device._detached = True
        self._devices.pop(device.id)
        result = await call(
            self._runtime._core, dict(op="unregister", id=device.id, timeout_ms=timeout_ms)
        )
        return DetachReceipt(**result)

    def _event(self, kind, info):
        # Lifecycle events are recorded by Rust. Kept for old embedding integrations.
        pass

    async def events(self, *, after=0):
        if type(after) is not int or after < 0:
            raise MuError("INVALID_ARGUMENT", "invalid event cursor")
        while True:
            result = await call(
                self._runtime._core, dict(op="events_wait", session=self.id, after=after)
            )
            for event in result["events"]:
                after = event["cursor"]
                yield event
            if result["closed"]:
                return

    async def _request(self, device, message):
        wire = dict(message)
        for key in ("app_id", "window_id", "display_id", "tab_id"):
            value = getattr(device, "_" + key, None)
            if value:
                wire.setdefault(key, value)
        revived = False
        while True:
            try:
                device._check()
                return await call(
                    self._runtime._core,
                    dict(
                        op="request",
                        attachment=device.id,
                        resource=device.resource_id,
                        wire=wire,
                    ),
                )
            except MuError as error:
                # 作用域临时阻塞不等于端点断线；强制重连会移除仍在运行的端点与句柄。
                scope_fenced = (
                    isinstance(error.details, dict) and error.details.get("cause") == "scope_fenced"
                )
                if (
                    error.code != "DEVICE_OFFLINE"
                    or scope_fenced
                    or not self._auto_reconnect
                    or revived
                    or not await self._revive(device)
                ):
                    raise
                revived = True

    async def _revive(self, device):
        """Ask the trusted runtime to rebuild a dead endpoint; True if reusable."""
        endpoint = device._endpoint
        if endpoint is None:
            return False
        try:
            await self._runtime.reconnect(endpoint.info.id, force=True)
        except MuError:
            return False
        return (
            not device._detached
            and device._endpoint is not endpoint
            and not device._endpoint.offline
        )

    async def _re_register(self, device, endpoint, *, timeout_ms=5000):
        """Re-point a whole-device attachment at a rebuilt endpoint in place.

        Returns False for scoped handles: their driver identities do not survive a
        reconnect, so they stay detached and the agent rebinds from a fresh listing.
        """
        from .resources import _BackendSession

        scoped = any(
            getattr(device, "_" + key, None)
            for key in ("app_id", "window_id", "display_id", "tab_id")
        )
        if scoped or self._closed or self._runtime._closed:
            return False
        result = state(
            self._runtime._core,
            op="register",
            session=self.id,
            name=device._info.name,
            access=device._info.access,
            resource=endpoint.info.id,
        )
        attachment = AttachmentInfo(**result["attachment"])
        old_id = device.id
        device._endpoint = endpoint
        device._info = attachment
        device.id = attachment.id
        device.name = attachment.name
        device.resource_id = endpoint.info.id
        device._scope = f"{device.id}/device/"
        device._ax_values = {}
        device._image_values = {}
        device._backend = _BackendSession(device)
        device._detached = False
        # Driver app identities can stay identical across an endpoint rebuild.
        # Cached scoped handles still carry the old attachment, so a fresh
        # apps.get()/windows.get() must not return them after re-registration.
        for name in ("apps", "windows", "displays"):
            namespace = vars(device).get(name)
            cache = vars(namespace).get("_cache") if hasattr(namespace, "__dict__") else None
            if isinstance(cache, dict):
                cache.clear()
        device._handles = [device]
        endpoint.leases.add(device.id)
        self._devices.pop(old_id, None)
        self._devices[device.id] = device
        return True

    async def action_status(self, action_id):
        return state(self._runtime._core, op="status", session=self.id, id=action_id)

    async def cancel(self, action_id):
        return state(self._runtime._core, op="cancel", session=self.id, id=action_id)

    def begin_turn(self):
        self._in_turn = True
        for device in self._devices.values():
            for handle in device._handles:
                self._runtime._core.freeze(handle._scope)

    def end_turn(self):
        self._in_turn = False

    def reset_output_context(self):
        """Call when the consumer loses its AX history (for example context trimming)."""
        for device in self._devices.values():
            for handle in device._handles:
                self._runtime._core.reset(handle._scope)

    async def _deliver(self, output):
        if self._output:
            result = self._output(output)
            if inspect.isawaitable(result):
                await result
        elif output["type"] == "image":
            if self.image_sink is None:
                raise MuError("OUTPUT_UNAVAILABLE", "configure a host image sink")
            result = self.image_sink(output["data"], output.get("caption"))
            if inspect.isawaitable(result):
                await result
        else:
            print(output["text"], flush=True)

    async def emit(self, value, *, full=False, caption=None):
        self._check()
        from .observations import emit

        async with self._emit_lock:
            await emit(self, value, full=full, caption=caption)

    def help(self, topic="core"):
        return state(self._runtime._core, op="help", topic=topic)

    async def close(self, *, timeout_ms=5000):
        timeout(timeout_ms)
        if self._close_receipt:
            return self._close_receipt
        result = await call(
            self._runtime._core, dict(op="close_session", session=self.id, timeout_ms=timeout_ms)
        )
        for device in self._devices.values():
            device._detached = True
        self._devices.clear()
        self._closed = True
        self._runtime._sessions.discard(self)
        result["unresolved_actions"] = tuple(result["unresolved_actions"])
        self._close_receipt = StopReceipt(**result)
        return self._close_receipt

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
