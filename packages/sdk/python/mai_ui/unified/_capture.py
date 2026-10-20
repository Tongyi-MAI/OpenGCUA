"""Python values backed by the Rust observation store.

This module materializes typed metadata; it owns no indexing, diff, identity,
coordinate-transform, or output-history policy.
"""

import json
from dataclasses import dataclass, field
from types import MappingProxyType, SimpleNamespace

from .._errors import MuError
from ..types import Bounds, Node, NodeAction, Selection, TreeState
from .runtime import native, uid


@dataclass(frozen=True)
class NodeRef:
    id: str
    observation_id: str
    _owner: object = field(repr=False)

    @property
    def node(self):
        return next(
            n
            for n in self._owner._backend._observations[self.observation_id]._tree.nodes
            if n.id == self.id
        )

    @property
    def index(self):
        return next(
            i
            for i, n in self._owner._backend._observations[self.observation_id]._indices.items()
            if n == self.id
        )


@dataclass(frozen=True)
class PointRef:
    observation_id: str
    x: float
    y: float
    _owner: object = field(repr=False)
    _token: str = field(repr=False)


def _node(raw):
    row = dict(raw)
    row["bounds"] = Bounds(**row["bounds"]) if row.get("bounds") else None
    row["selection"] = Selection(**row["selection"]) if row.get("selection") else None
    row["actions"] = tuple(
        NodeAction(id=a["id"], label=a.get("label"), arguments=()) for a in row["actions"]
    )
    return Node(**row)


class Captured:
    def __init__(self, handle, data, *, tree):
        self._handle = handle
        self.id = uid()
        value = json.loads(
            native(
                handle._session._runtime._core.capture,
                handle._scope,
                self.id,
                json.dumps(data),
                tree,
                handle._app_id,
            )
        )
        self._data = value["data"]
        self.components = tuple(data.get("components", ()))
        self.images = tuple(ImageCapture(handle, part) for part in value.get("images", ()))
        self._indices = MappingProxyType(dict(value["indices"]))
        self.sequence = value["sequence"]
        pending, age = data.get("pending_invalidations", 0), data.get("age_ms")
        self._tree = TreeState(
            status="unavailable" if not tree else "partial" if data["truncated"] else "ok",
            nodes=tuple(_node(n) for n in value["nodes"]),
            captured_at=data["captured_at"] / 1000,
            revision=data["revision"],
            truncated=data["truncated"],
            source=data["source"],
            freshness="unknown"
            if age is None
            else "stale"
            if pending or age > 250
            else "within_budget",
            oldest_verified_age_ms=age,
            pending_invalidations=pending,
        )
        self.image = SimpleNamespace(
            frame=SimpleNamespace(
                id=self.id, width=data["width"], height=data["height"], mime_type="image/png"
            )
            if data.get("png")
            else None
        )
        handle._backend._observations[self.id] = self

    def _ref(self, node):
        return NodeRef(node, self.id, self._handle)

    def point(self, x, y, *, space="pixels"):
        if (
            type(x) not in (int, float)
            or type(y) not in (int, float)
        ):
            raise MuError(
                "INVALID_ARGUMENT", "provide numeric coordinates"
            )
        token = uid()
        h = self._handle
        wire = json.loads(
            native(
                h._session._runtime._core.point, h._scope, self.id, token, x, y, space
            )
        )
        return PointRef(self.id, wire["x"], wire["y"], h, token)


class ImageCapture:
    """A component image already registered by the shared Rust store."""

    point = Captured.point

    def __init__(self, handle, data):
        self._handle, self._data, self.id = handle, data, data["id"]
        self.image = SimpleNamespace(
            frame=SimpleNamespace(
                id=self.id,
                width=data["width"],
                height=data["height"],
                mime_type=data.get("mime_type", "image/png"),
            )
        )
        handle._backend._observations[self.id] = self
