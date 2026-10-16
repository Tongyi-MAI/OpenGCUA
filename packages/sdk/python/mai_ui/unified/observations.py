"""Captured values and acknowledged, per-scope output histories."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass

from .._apps import format_apps
from .._errors import MuError
from ..types import AppInventory
from ._capture import NodeRef, PointRef
from .runtime import native, state


class AXNode:
    """Read-only normalized metadata plus an opaque, captured native target."""

    __slots__ = ("_ref",)

    def __init__(self, ref):
        object.__setattr__(self, "_ref", ref)

    def __setattr__(self, name, value):
        raise AttributeError("captured nodes are immutable")

    def __getattr__(self, name):
        if name == "index":
            return self._ref.index
        return getattr(self._ref.node, name)


@dataclass(frozen=True)
class AXTree:
    nodes: tuple[AXNode, ...]
    status: str
    truncated: bool


class AXText(str):
    """Rendered AX text that also paginates/searches the SAME captured snapshot.

    A str subclass, so ``ax.text`` still emits/compares as plain text; the extra
    methods let the agent page or search long content without re-observing.
    """

    def __new__(cls, value, observation):
        obj = super().__new__(cls, value)
        obj._observation = observation
        return obj

    @staticmethod
    def _row(node):
        return {"index": node.index, "role": node.role, "name": node.name, "text": node.text}

    def search(self, substring, *, limit=50):
        if not isinstance(substring, str) or not substring:
            raise MuError("INVALID_ARGUMENT", "search needs a nonempty string")
        needle = substring.casefold()
        matches = []
        for node in self._observation.tree.nodes:
            haystack = " ".join(str(v) for v in (node.name, node.text) if v).casefold()
            if needle in haystack:
                matches.append(self._row(node))
                if len(matches) >= limit:
                    break
        return matches

    def page(self, start=0, count=50):
        if type(start) is not int or type(count) is not int or start < 0 or count < 1:
            raise MuError("INVALID_ARGUMENT", "page needs a non-negative start and a positive count")
        return [self._row(n) for n in self._observation.tree.nodes[start:start + count]]

    def read(self, index, field="text", offset=0, count=None):
        field = {"label": "name", "value": "text", "frame": "bounds"}.get(field, field)
        node = next((n for n in self._observation.tree.nodes if n.index == index), None)
        if node is None:
            raise MuError("STALE_TARGET", f"no node at index {index} in this snapshot")
        if field == "bounds":
            b = node.bounds
            return None if b is None else {"x": b.x, "y": b.y, "width": b.width, "height": b.height}
        value = getattr(node, field, None)
        if not isinstance(value, str):
            return value
        return value[offset:] if count is None else value[offset:offset + count]


class AXObservation:
    def __init__(self, handle, captured):
        self._handle, self._captured = handle, captured
        self.sequence = captured.sequence
        handle._ax_values[captured.id] = self
        self.id = captured.id
        self.tree = AXTree(
            tuple(AXNode(captured._ref(n.id)) for n in captured._tree.nodes),
            captured._tree.status,
            captured._tree.truncated,
        )
        self.components = captured.components
        self._last_stats = None

    @property
    def text(self):
        return AXText(self._render(full=True)["text"], self)

    @property
    def emission_stats(self):
        return dict(self._last_stats) if self._last_stats else None

    def _render(self, *, full=False):
        h = self._handle
        header = (
            f"AX {h.name} scope={h.resource_id} observation={self.id} status={self.tree.status}"
        )
        if self.components:
            header += " windows=" + json.dumps(self.components, ensure_ascii=False)
        return json.loads(
            native(h._session._runtime._core.render_capture, h._scope, self.id, header, full)
        )


class Image:
    def __init__(self, handle, captured):
        self._handle, self._captured = handle, captured
        frame = captured.image.frame
        self.id, self.width, self.height = frame.id, frame.width, frame.height
        self.mime_type = frame.mime_type
        self.label = captured._data.get("label", f"{handle.name} / display:0")
        self.resource_id = handle.resource_id
        self.captured_at = captured._data.get("captured_at")
        self.geometry = captured._data.get("geometry")
        self.status = captured._data.get("status", "ok")
        handle._image_values[captured.id] = self

    async def read(self):
        self._handle._check()
        return native(
            self._handle._session._runtime._core.image_bytes, self._handle._scope, self._captured.id
        )

    def point(self, x, y, *, space="pixels", max_age_ms=120000):
        self._handle._check()
        return self._captured.point(x, y, space=space, max_age_ms=max_age_ms)


class VisionObservation:
    def __init__(self, handle, captured):
        self._handle = handle
        self.id = captured.id
        self.images = tuple(Image(handle, part) for part in captured.images)
        if not self.images and captured.image.frame:
            self.images = (Image(handle, captured),)
        self.components = captured.components
        self.status = (
            "partial"
            if self.images and any(c.get("status") != "ok" for c in self.components)
            else "ok"
            if self.images
            else "unavailable"
        )


async def emit(session, value, *, full=False, caption=None):
    if type(full) is not bool or (caption is not None and not isinstance(caption, str)):
        raise MuError("INVALID_ARGUMENT")
    if full and not isinstance(value, AXObservation):
        raise MuError("INVALID_ARGUMENT", "full=True requires an AX observation")
    if isinstance(value, (AXObservation, VisionObservation, Image)):
        handle = value._handle
        if handle._session is not session:
            raise MuError("INVALID_ARGUMENT", "foreign observation")
        handle._check()
    if isinstance(value, AXObservation):
        if caption is not None:
            raise MuError("INVALID_ARGUMENT", "caption requires an image")
        result = value._render(full=full)
        try:
            await session._deliver(dict(type="text", text=result["text"], observation_id=value.id))
        except BaseException:
            # A sink failure may occur after partial delivery: preserve old targets,
            # but require a full resynchronization on the next explicit emission.
            handle._session._runtime._core.failed_delivery(handle._scope)
            raise
        value._last_stats = {k: v for k, v in result.items() if k != "text"}
        native(handle._session._runtime._core.acknowledge, handle._scope, value.id, False)
    elif isinstance(value, VisionObservation):
        if not value.images:
            await session._deliver(dict(type="text", text="Screenshot unavailable."))
        for image in value.images:
            await emit(session, image, caption=caption)
        missing = [c for c in value.components if c.get("status") != "ok"]
        if missing:
            await session._deliver(
                dict(type="text", text=json.dumps(dict(window_capture_status=missing)))
            )
    elif isinstance(value, Image):
        data = await value.read()
        historical = native(
            handle._session._runtime._core.historical, handle._scope, value._captured.id, True
        )
        label = caption or f"{value.label} image={value.id} {value.width}x{value.height}"
        if historical:
            label = "Historical image; does not update live targets. " + label
        await session._deliver(
            dict(
                type="image",
                data=data,
                mime_type=value.mime_type,
                image_id=value.id,
                width=value.width,
                height=value.height,
                coordinate_space=session.coordinate_space,
                caption=label,
            )
        )
        native(handle._session._runtime._core.acknowledge, handle._scope, value._captured.id, True)
    elif isinstance(value, bytes):
        mime = (
            "image/png"
            if value.startswith(b"\x89PNG\r\n\x1a\n")
            else "image/jpeg"
            if value.startswith(b"\xff\xd8\xff")
            else None
        )
        if mime is None:
            raise MuError("INVALID_ARGUMENT", "image bytes must be PNG or JPEG")
        await session._deliver(dict(type="image", data=value, mime_type=mime, caption=caption))
        # Agent-processed bytes do not replace any resource's coordinate history.
    else:
        if caption is not None:
            raise MuError("INVALID_ARGUMENT", "caption requires an image")
        if isinstance(value, (AXNode, NodeRef, PointRef)):
            raise MuError("INVALID_ARGUMENT", "emit an observation or explicit data")
        if isinstance(value, AppInventory):
            text = format_apps(value)
        elif isinstance(value, str):
            text = value
        else:
            if dataclasses.is_dataclass(value):
                value = dataclasses.asdict(value)
            try:
                text = state(session._runtime._core, op="format_data", data=value)
            except (TypeError, ValueError) as error:
                raise MuError(
                    "INVALID_ARGUMENT", "emit expects observation, image, text, or JSON data"
                ) from error
        await session._deliver(dict(type="text", text=text))
