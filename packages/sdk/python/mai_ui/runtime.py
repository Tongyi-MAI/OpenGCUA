from __future__ import annotations
import asyncio, base64, dataclasses, json, math, time, uuid
from collections.abc import Sequence, Mapping
from types import MappingProxyType
from .types import *
from . import _native
from .adapters import AdbAdapter, _registry
from ._apps import AppMatchError, format_apps, match_app, normalize_label


def uid():
    return uuid.uuid4().hex


def unsupported(message="This adapter does not implement this operation"):
    raise MuError("UNSUPPORTED_CAPABILITY", message)


from ._errors import MuError, ShellError


@dataclasses.dataclass(frozen=True)
class _NodeRef:
    id: str
    observation_id: str
    _owner: object = dataclasses.field(repr=False)
    _revision: str = dataclasses.field(repr=False)
    _scope: str = dataclasses.field(repr=False)

    @property
    def node(self):
        """Metadata from the captured observation, never an implicit refresh."""
        return next(
            n for n in self._owner._observations[self.observation_id]._tree.nodes if n.id == self.id
        )

    @property
    def index(self):
        obs = self._owner._observations[self.observation_id]
        return next(index for index, node_id in obs._indices.items() if node_id == self.id)

    @property
    def name(self):
        return self.node.name

    @property
    def text(self):
        return self.node.text

    @property
    def role(self):
        return self.node.role

    @property
    def checked(self):
        return self.node.checked


@dataclasses.dataclass(frozen=True)
class PointRef:
    observation_id: str
    x: float
    y: float
    _owner: object = dataclasses.field(repr=False)
    _revision: str = dataclasses.field(repr=False)
    _scope: str = dataclasses.field(repr=False)


def _node(row, at, revision, dirty):
    d = dict(row)
    d.pop("target_token", None)
    a = d.pop("selection_start", -1)
    b = d.pop("selection_end", -1)
    text = d.get("text") or ""

    def codepoints(offset):
        return len(text.encode("utf-16-le")[: offset * 2].decode("utf-16-le", "strict"))

    selection = None
    if a >= 0 and b >= a and not d.get("password"):
        try:
            selection = Selection(start=codepoints(a), end=codepoints(b), revision=revision)
        except UnicodeDecodeError:
            pass
    d["bounds"] = Bounds(**d["bounds"]) if d.get("bounds") else None
    d["actions"] = tuple(
        NodeAction(id=x["id"], arguments=(), label=x.get("label")) for x in d["actions"]
    )
    return Node(**d, last_verified_at=at, dirty=dirty, selection=selection)


class Observation:
    def __init__(self, ui, data, *, tree=True, screenshot=False):
        self._ui = ui
        self._data = data
        self.id = uid()
        self.session_epoch = ui._session.epoch
        self._received = time.monotonic()
        self.surface = ui.surface
        self.foreground_app_id = data.get("foreground_app_id")
        self.windows = ()
        self.display = DisplayInfo(
            id="display:0",
            generation=f"{data['width']}x{data['height']}",
            width=data["width"],
            height=data["height"],
            rotation=0,
            scale_factor=1,
            insets=None,
        )
        raw = data["nodes"] if tree else []
        if self.surface.app_id:
            raw = [n for n in raw if n["app_id"] == self.surface.app_id]
        if self.surface.window_id:
            raw = [n for n in raw if n["window_id"] == self.surface.window_id]
        self._raw = raw
        self._has_tree = bool(tree)
        self._screenshot_requested = screenshot
        pending = data.get("pending_invalidations", 0)
        at = data["captured_at"] / 1000
        nodes = tuple(_node(r, at, data["revision"], bool(pending)) for r in raw)
        age = data.get("age_ms")
        maxage = 250
        fresh = (
            "unknown"
            if age is None
            else "stale"
            if pending or (maxage is not None and age > maxage)
            else "within_budget"
        )
        self._tree = TreeState(
            status="unavailable" if not tree else "partial" if data["truncated"] else "ok",
            nodes=nodes,
            captured_at=at,
            revision=data["revision"],
            truncated=data["truncated"],
            source=data["source"],
            freshness=fresh,
            oldest_verified_age_ms=age,
            pending_invalidations=pending,
        )
        self._delta = None
        baseline = ui._session._shown.get(self.surface.id)
        if (
            tree
            and baseline
            and self._tree.status == "ok"
            and baseline._tree.status == "ok"
            and data.get("identity_epoch") == baseline._data.get("identity_epoch")
        ):
            delta = json.loads(_native.diff_nodes(json.dumps(baseline._raw), json.dumps(raw)))
            byid = {n.id: n for n in nodes}
            self._delta = TreeDelta(
                base_observation_id=baseline.id,
                removed=tuple(delta["removed"]),
                upserted=tuple(byid[n["id"]] for n in delta["upserted"]),
            )
        session = ui._session
        previous = session._indexed.get(self.surface.id)
        self._identity = (
            data.get("identity_mode", "revision"),
            data.get("identity_epoch"),
        )
        stable = (
            self._identity[0] == "native_lifetime"
            and self._identity[1] is not None
            and previous is not None
            and previous._identity == self._identity
        )
        same_revision = (
            previous is not None
            and previous._tree.revision == self._tree.revision
            and previous._raw == raw
        )
        old_indices = (
            {v: k for k, v in previous._indices.items()}
            if previous and (stable or same_revision)
            else {}
        )
        indices = {}
        for node in nodes:
            index = old_indices.get(node.id)
            if index is None:
                session._next_index += 1
                index = session._next_index
            indices[index] = node.id
        self._indices = MappingProxyType(indices)
        if tree:
            session._indexed[self.surface.id] = self
        self._ax_text = (
            AXTextState(
                text=self._format_ax(full=True),
                provider=session.adapter.provider,
                target_ids=tuple(self._indices.values()),
                captured_at=at,
                freshness=fresh,
            )
            if tree
            else None
        )
        self.stability = "unknown"
        self.capture_skew_ms = None
        self.image = ImageState(status="unavailable")
        if screenshot and data.get("png"):
            artifact = uid()
            ui._session._artifacts[artifact] = base64.b64decode(data["png"])
            frame = Frame(
                id=uid(),
                device_id=ui._session._device.id,
                session_epoch=self.session_epoch,
                surface_id=self.surface.id,
                display_id=self.display.id,
                display_generation=self.display.generation,
                captured_at=data["image_at"] / 1000,
                width=self.display.width,
                height=self.display.height,
                artifact_id=artifact,
                mime_type="image/png",
                to_display=(1, 0, 0, 1, 0, 0),
                scope="display",
            )
            self.image = ImageState(status="ok", frame=frame)
            self.capture_skew_ms = abs(data["image_at"] - data["captured_at"])
        ui._session._observations[self.id] = self



    def _ref(self, node_id):
        if type(node_id) is int:
            if node_id not in self._indices:
                raise MuError("STALE_TARGET", "index is not in this observation")
            node_id = self._indices[node_id]
        if node_id not in {n.id for n in self._tree.nodes}:
            raise MuError("TARGET_NOT_FOUND")
        ref = _NodeRef(node_id, self.id, self._ui._session, self._tree.revision, self.surface.id)
        self._ui._session._refs[id(ref)] = ref
        return ref

    def _ax_records(self):
        """Rendered records and final topology; diagnostic geometry stays in Python."""
        by_id = {n.id: n for n in self._tree.nodes}
        index_of = {node_id: index for index, node_id in self._indices.items()}
        records, positions, depths = {}, {}, {}
        siblings = {}
        for index, node_id in self._indices.items():
            node = by_id[node_id]
            parent = index_of.get(node.parent_id)
            positions[index] = (parent, siblings.get(parent))
            siblings[parent] = index
            seen = {node_id}
            ancestor = node.parent_id
            while ancestor in by_id and ancestor not in seen and len(seen) < 32:
                seen.add(ancestor)
                ancestor = by_id[ancestor].parent_id
            depths[index] = len(seen) - 1
            fields = [node.role or "unknown"]
            for key in ("name", "text", "hint"):
                value = getattr(node, key)
                if key == "text" and not node.editable and value == node.name:
                    continue
                if value or (key == "text" and node.editable and not node.password):
                    fields.append(f"{key}={json.dumps(value or '', ensure_ascii=False)}")
            for key in ("editable", "focused", "password"):
                if getattr(node, key):
                    fields.append(key)
            for key in ("enabled", "visible"):
                if getattr(node, key) is False:
                    fields.append(f"{key}=false")
            if node.checked is not None:
                fields.append("checked=" + str(node.checked).lower())
            if node.selection:
                fields.append(f"selection=[{node.selection.start}:{node.selection.end}]")
            if node.actions:
                fields.append(
                    "actions=" + json.dumps([a.id for a in node.actions], separators=(",", ":"))
                )
            records[index] = " ".join(fields)
        return records, positions, depths

    def _format_ax(self, *, full=False):
        header = f"AX {self.surface.id} revision={self._tree.revision} status={self._tree.status} freshness={self._tree.freshness}"
        baseline = self._ui._session._shown.get(self.surface.id)
        records, positions, depths = self._ax_records()
        complete = self._tree.status == "ok" and baseline and baseline._tree.status == "ok"
        stable = (
            baseline
            and self._identity[0] == "native_lifetime"
            and self._identity[1] is not None
            and baseline._identity == self._identity
        )
        same = baseline and baseline._indices == self._indices and baseline._raw == self._raw
        reason = None
        if baseline and not stable and not same:
            reason = "Identity reset: previous indices retired."
        if self._tree.status != "ok":
            reason = "Partial view: missing nodes do not imply removal; reacquire before relying on absent elements."
        full_lines = [header, "Full tree; use only indices listed below."]
        if reason:
            full_lines.append(reason)
        full_lines.extend("  " * depths[i] + f"[{i}] {record}" for i, record in records.items())
        if not records:
            full_lines.append("(empty tree)")
        full_text = "\n".join(full_lines)
        if full or not complete or not (stable or same):
            return full_text
        old, old_positions, old_depths = baseline._ax_records()
        removed = sorted(old.keys() - records.keys(), key=lambda i: (-old_depths[i], i))

        def location(i):
            parent, after = positions[i]
            return f" parent={'null' if parent is None else f'[{parent}]'} after={'null' if after is None else f'[{after}]'}"

        changes = [f"- [{i}]" for i in removed]
        changes.extend(
            f"+ [{i}] {record}" + location(i) for i, record in records.items() if i not in old
        )
        changes.extend(
            f"~ [{i}] {record}" + (location(i) if positions[i] != old_positions[i] else "")
            for i, record in records.items()
            if i in old and (record != old[i] or positions[i] != old_positions[i])
        )
        if not changes:
            return header + "\nNo AX changes. Existing indices remain valid."
        delta_text = (
            header
            + "\nChanges since previous emitted AX; unchanged indices remain valid.\n"
            + "\n".join(changes)
        )
        return (
            delta_text
            if len(delta_text.encode("utf-8")) < len(full_text.encode("utf-8"))
            else full_text
        )

    def point(self, x, y, *, space="pixels", image_size=None):
        frame = self.image.frame
        if frame is None:
            raise MuError("INVALID_ARGUMENT", "point requires a screenshot")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (x, y)):
            raise MuError("INVALID_ARGUMENT", "coordinates must be numbers")
        basis = image_size if image_size is not None else (frame.width, frame.height)
        if (
            not isinstance(basis, (tuple, list))
            or len(basis) != 2
            or any(type(v) is not int or not 0 < v <= 2**32 - 1 for v in basis)
        ):
            raise MuError("INVALID_ARGUMENT", "image_size must be two positive integers")
        try:
            x, y = _native.convert_point(x, y, space, (frame.width, frame.height), tuple(basis))
        except (TypeError, ValueError, OverflowError) as error:
            raise MuError("INVALID_ARGUMENT", str(error)) from error
        a, b, c, d, tx, ty = frame.to_display
        x, y = a * x + c * y + tx, b * x + d * y + ty
        if not all(math.isfinite(v) for v in (x, y)) or not (
            0 <= x < self.display.width and 0 <= y < self.display.height
        ):
            raise MuError("INVALID_ARGUMENT", "point outside frame")
        if self.surface.kind == "window":
            unsupported("window-scoped coordinates; use device.ui")
        ref = PointRef(
            self.id,
            x,
            y,
            self._ui._session,
            self._tree.revision,
            self.surface.id,
        )
        self._ui._session._refs[id(ref)] = ref
        return ref


class _Unsupported:
    _planned = frozenset()

    def __init__(self, planned=()):
        self._planned = frozenset(planned)

    def __getattr__(self, name):
        if name not in self._planned:
            raise AttributeError(f"{type(self).__name__} has no attribute {name!r}")
        async def method(*args, **kwargs):
            unsupported(name)

        return method


class UI(_Unsupported):
    _planned = frozenset({"press_key", "drag", "stream"})
    def __init__(self, session, *, app_id=None, window_id=None):
        self._session = session
        self.surface = SurfaceInfo(
            id=window_id or app_id or "display:0",
            device_id=session._device.id,
            kind="window" if window_id else "app" if app_id else "display",
            display_id="display:0",
            app_id=app_id,
            window_id=window_id,
        )
        self.pointer = _Unsupported({"move"})
        self.touch = _Unsupported({"long_press", "swipe", "pinch", "gesture"})


    async def observe(self, *, screenshot=False, timeout_ms=1500):
        """Capture AX or one screenshot silently; actions are already serialized."""
        if type(screenshot) is not bool:
            raise MuError("INVALID_ARGUMENT", "screenshot must be boolean")
        return await self._observe(tree=not screenshot, screenshot=screenshot,
                                   mode="latest" if screenshot else "refresh",
                                   timeout_ms=timeout_ms)

    async def _observe(self, *, tree=True, screenshot=False, mode="refresh", timeout_ms=1500):
        """Private acquisition policy for host fixtures, not another agent API."""
        if timeout_ms <= 0 or mode not in ("latest", "cached", "refresh"):
            raise MuError("INVALID_ARGUMENT", "invalid observation timeout or cache mode")
        data = await self._session._request(
            dict(op="observe", mode=mode, screenshot=screenshot, timeout_ms=timeout_ms)
        )
        return Observation(self, data, tree=tree, screenshot=screenshot)

    def _resolve(self, target):
        if type(target) is int:
            obs = self._session._presented_ax.get(self.surface.id)
            if obs is None:
                raise MuError(
                    "STALE_TARGET",
                    "emit an AX observation first: await mu.emit(await ui.observe())",
                )
            return obs._ref(target)
        if isinstance(target, bool):
            raise MuError("INVALID_ARGUMENT", "a boolean is not an AX index")
        return target


    def _point(self, x, y, space):
        s = self._session
        obs = s._turn_image if s._in_turn else s._presented_image
        if obs is None:
            raise MuError(
                "STALE_TARGET", "Emit a screenshot in a previous turn before coordinate actions"
            )
        if self.surface.app_id and obs.foreground_app_id != self.surface.app_id:
            raise MuError(
                "TARGET_NOT_FOREGROUND",
                "The emitted screenshot belongs to a different foreground app",
            )
        point = obs.point(x, y, space=space or s.coordinate_space)
        # Android captures full-display images, including for app.ui.observe().
        point = dataclasses.replace(point, _scope=self.surface.id)
        s._refs[id(point)] = point
        return point



    def _validate(self, target):
        target = self._resolve(target)
        s = self._session
        if (
            not isinstance(target, (_NodeRef, PointRef))
            or target._owner is not s
            or target._scope != self.surface.id
            or s._refs.get(id(target)) is not target
        ):
            raise MuError(
                "INVALID_ARGUMENT",
                f"Target scope {getattr(target, '_scope', None)!r} does not match {self.surface.id!r}, or reference is foreign. Use the UI that captured the observation (obs._ui), or ui.click(index) with AX emitted for this scope",
            )
        if isinstance(target, _NodeRef):
            wire = dict(revision=target._revision, node_id=target.id)
            obs = s._observations[target.observation_id]
            if obs._identity[0] == "native_lifetime":
                row = next(n for n in obs._raw if n["id"] == target.id)
                if row.get("target_token"):
                    wire["target_token"] = row["target_token"]
            return wire
        return dict(revision=target._revision, x=target.x, y=target.y)

    async def _action(self, action, target=None, *, timeout_ms=3000, action_id=None, **args):
        target = self._resolve(target)
        if target is not None:
            args.update(self._validate(target))
        if self.surface.app_id:
            args["app_id"] = self.surface.app_id
        if self.surface.window_id:
            unsupported("window actions")
        return await self._session._action(
            action,
            timeout_ms=timeout_ms,
            action_id=action_id,
            based_on=target.observation_id if target is not None else None,
            **args,
        )

    async def click(
        self,
        target=None,
        *,
        x=None,
        y=None,
        space=None,
        button="primary",
        count=1,
        mode="auto",
        timeout_ms=3000,
        action_id=None,
    ):
        if x is not None or y is not None:
            if target is not None or x is None or y is None:
                raise MuError(
                    "INVALID_ARGUMENT", "Use either click(target) or click(x=..., y=...), not both"
                )
            target = self._point(x, y, space)
        if target is None:
            raise MuError("INVALID_ARGUMENT", "click requires a target or x and y")
        if button != "primary" or count != 1:
            unsupported("only a single primary click is implemented")
        if mode not in ("auto", "semantic", "physical"):
            raise MuError("INVALID_ARGUMENT")
        if isinstance(target, PointRef):
            if mode == "semantic":
                unsupported("point has no semantic action")
            action = "point_click"
        else:
            if mode == "physical":
                unsupported("node physical-click validation")
            action = "click"
        return await self._action(action, target, timeout_ms=timeout_ms, action_id=action_id)

    async def wait_for_stabilize(self):
        """Silent fixed two-second wait; UI stability is not yet measured."""
        if self._session._stopped:
            raise MuError("SESSION_CLOSED")
        await asyncio.sleep(2)

    async def input_text(
        self, text, target=None, *, x=None, y=None, space=None,
        timeout_ms=5000, action_id=None,
    ):
        """ADBKeyboard insertion; optional grounded click, then a 500 ms focus wait."""
        if not isinstance(text, str):
            raise MuError("INVALID_ARGUMENT", "text must be a string")
        if x is not None or y is not None:
            if target is not None or x is None or y is None:
                raise MuError("INVALID_ARGUMENT", "Use target or both x and y, not both")
            target = self._point(x, y, space)
        target = self._resolve(target)
        if target is not None and not isinstance(target, (_NodeRef, PointRef)):
            raise MuError("INVALID_ARGUMENT", "target must be an emitted AX index or screenshot point")
        if timeout_ms <= (500 if target is not None else 0):
            raise MuError("INVALID_ARGUMENT", "timeout must allow the 500 ms target focus wait")
        return await self._action(
            "input_text", target, text=text, timeout_ms=timeout_ms, action_id=action_id,
        )

    async def perform(self, target, action, *, args=None, timeout_ms=3000, action_id=None):
        target = self._resolve(target)
        if args:
            unsupported("arguments to generic node actions")
        if not isinstance(target, _NodeRef) or not str(action).isdigit():
            raise MuError("INVALID_ARGUMENT")
        return await self._action(
            "perform",
            target,
            node_action=action,
            timeout_ms=timeout_ms,
            action_id=action_id,
        )

    async def scroll(
        self,
        *args,
        target=None,
        direction=None,
        start_x=None,
        start_y=None,
        end_x=None,
        end_y=None,
        duration_ms=500,
        space=None,
        amount=1,
        unit="steps",
        timeout_ms=3000,
        action_id=None,
    ):
        """Semantic scroll(target, direction) or physical scroll(sx, sy, ex, ey)."""
        coords = (start_x, start_y, end_x, end_y)
        if args:
            if (
                target is not None
                or (len(args) != 1 and direction is not None)
                or any(v is not None for v in coords)
            ):
                raise MuError(
                    "INVALID_ARGUMENT", "Do not mix positional and keyword scroll targets"
                )
            if len(args) == 4:
                coords = args
            elif len(args) == 2:
                target, direction = args
            elif len(args) == 1:
                target = args[0]
            else:
                raise MuError(
                    "INVALID_ARGUMENT",
                    "Use scroll(target, direction) or scroll(start_x, start_y, end_x, end_y)",
                )
        if any(v is not None for v in coords):
            if target is not None or direction is not None or any(v is None for v in coords):
                raise MuError(
                    "INVALID_ARGUMENT",
                    "Coordinate scrolling requires all four coordinates and no AX target",
                )
            if (
                type(duration_ms) is not int
                or not 50 <= duration_ms <= 5000
                or duration_ms >= timeout_ms
            ):
                raise MuError(
                    "INVALID_ARGUMENT", "duration_ms must be 50..5000 and less than timeout_ms"
                )
            start = self._point(coords[0], coords[1], space)
            end = self._point(coords[2], coords[3], space)
            return await self._action(
                "point_scroll",
                start,
                end_x=end.x,
                end_y=end.y,
                duration_ms=duration_ms,
                timeout_ms=timeout_ms,
                action_id=action_id,
            )
        target = self._resolve(target)
        if (
            direction not in ("up", "down")
            or amount != 1
            or unit != "steps"
            or not isinstance(target, _NodeRef)
        ):
            unsupported(
                "Semantic scrolling requires an AX node and 'up' or 'down'; coordinate scrolling takes four coordinates"
            )
        return await self._action(
            "scroll", target, direction=direction, timeout_ms=timeout_ms, action_id=action_id
        )


class Images:
    def __init__(self, session):
        self._session = session

    async def read(self, frame):
        if (
            frame.session_epoch != self._session.epoch
            or frame.artifact_id not in self._session._artifacts
        ):
            raise MuError("INVALID_ARGUMENT", "foreign artifact")
        return self._session._artifacts[frame.artifact_id]


from ._shell import ShellClient

class Shell(ShellClient, _Unsupported):
    _planned = frozenset({"spawn", "get_process"})


class App:
    def __init__(self, device, app_id):
        self._device = device
        self.id = app_id
        self.ui = UI(device._session, app_id=app_id)

    async def launch(self, *, timeout_ms=3000, action_id=None):
        return await self._device._session._action(
            "launch", app_id=self.id, timeout_ms=timeout_ms, action_id=action_id
        )


class Display:
    def __init__(self, device):
        self.id = "display:0"
        self.ui = device.ui


class Window(_Unsupported):
    _planned = frozenset({"activate"})
    def __init__(self, device, window_id):
        self.id = window_id
        self.ui = UI(device._session, window_id=window_id)


class AndroidControls:
    def __init__(self, session):
        self._session = session

    async def press(self, button, *, timeout_ms=3000, action_id=None):
        if self._session.adapter.platform != "android" or button not in (
            "back",
            "home",
            "recents",
        ):
            unsupported("system button")
        return await self._session._action(
            "system", button=button, timeout_ms=timeout_ms, action_id=action_id
        )


class Device(_Unsupported):
    _planned = frozenset({"open_url"})
    def __init__(self, session):
        self._session = session
        self.id = "device:0"
        self.ui = UI(session)
        self.shell = Shell(session)
        self.android = AndroidControls(session)
        self._open_app_requests = {}
        self._open_app_lock = asyncio.Lock()


    def get_app(self, app_id):
        if not isinstance(app_id, str) or not app_id.startswith("app:") or len(app_id) <= 4:
            raise MuError("INVALID_ARGUMENT", "use normalized app:<native-id>")
        return App(self, app_id)

    async def open_app(self, label, *, timeout_ms=5000, action_id=None):
        """Open the best unambiguous installed app-label match, without emitting UI."""
        if not isinstance(label, str) or not normalize_label(label):
            raise MuError("INVALID_ARGUMENT", "label must be a nonempty app name")
        if type(timeout_ms) is not int or timeout_ms <= 0:
            raise MuError("INVALID_ARGUMENT", "timeout_ms must be a positive integer")
        if self._session._stopped:
            raise MuError("SESSION_CLOSED")
        if action_id is not None and (not isinstance(action_id, str) or not action_id):
            raise MuError("INVALID_ARGUMENT", "action_id must be a nonempty string")
        async with self._open_app_lock:
            request = (label, timeout_ms)
            cached = self._open_app_requests.get(action_id) if action_id is not None else None
            if cached:
                previous, app_id = cached
                if previous != request:
                    raise MuError("INVALID_ARGUMENT", "reused action ID with different app request")
            else:
                inventory = await self.list_apps(timeout_ms=timeout_ms)
                try:
                    app_id = match_app(label, inventory).id
                except AppMatchError as error:
                    raise MuError(error.code, str(error)) from error
                if action_id is not None:
                    self._open_app_requests[action_id] = (request, app_id)
            return await self.get_app(app_id).launch(timeout_ms=timeout_ms, action_id=action_id)

    async def list_apps(self, *, launchable_only=True, timeout_ms=5000):
        """List apps visible to the current Android user, without observing the UI."""
        if type(launchable_only) is not bool or type(timeout_ms) is not int or timeout_ms <= 0:
            raise MuError(
                "INVALID_ARGUMENT", "launchable_only must be boolean and timeout_ms positive"
            )
        data = await self._session._request(
            dict(op="list_apps", launchable_only=launchable_only, timeout_ms=timeout_ms)
        )
        return AppInventory(
            apps=tuple(AppInfo(**row) for row in data["apps"]), complete=data["complete"]
        )

    async def list_displays(self):
        data = await self._session._request(dict(op="displays"))
        return tuple(DisplayInfo(**d) for d in data["displays"])

    def get_display(self, display_id):
        if display_id != "display:0":
            unsupported("only the primary display is implemented")
        return Display(self)

    async def list_windows(self, *, app_id=None):
        unsupported("complete window inventory")

    def get_window(self, window_id, *, display_id=None):
        unsupported("window-scoped actions")

    async def action_status(self, action_id):
        r = await self._session._request(dict(op="status", action_id=action_id))
        prev = self._session._receipts.get(action_id)
        if not prev:
            raise MuError("ACTION_OUTCOME_UNKNOWN", "unknown action id")
        return dataclasses.replace(prev, execution=r.get("execution", "unknown"))




class Session:
    def __init__(self, adapter):
        self.adapter = adapter
        self.epoch = uid()
        self._device = object.__new__(Device)
        self._device.id = "device:0"
        Device.__init__(self._device, self)
        self._action_requests = {}
        self._description = {}
        self._stopped = False
        self._stop_receipt = None
        self._receipts = {}
        self._shown = {}
        self._indexed = {}
        self._presented_ax = {}
        self._presented_image = None
        self._turn_image = None
        self._in_turn = False
        self.coordinate_space = "pixels"
        self._next_index = 0
        self._artifacts = {}
        self._observations = {}
        self._refs = {}
        self._sequence = 0
        self.images = Images(self)
        from .host import HostDiagnostics
        self.host = HostDiagnostics(self)
        self.image_sink = None

    async def _request(self, message):
        if self._stopped and message["op"] != "stop":
            raise MuError("SESSION_CLOSED")
        try:
            r = await self.adapter.request(message)
        except ConnectionError as e:
            raise MuError("ACTION_OUTCOME_UNKNOWN", str(e)) from e
        if "error" in r:
            msg = r["error"]
            code = msg.get("code", "PROVIDER_ERROR") if isinstance(msg, dict) else msg.split(":")[0]
            msg = msg.get("message", code) if isinstance(msg, dict) else msg
            if not code.isupper():
                code = (
                    "SHELL_START_FAILED" if message["op"] == "shell" else "ACTION_OUTCOME_UNKNOWN"
                )
            raise MuError(code, msg)
        return r["result"]

    async def _action(self, action, *, action_id=None, timeout_ms=3000, based_on=None, **args):
        if timeout_ms <= 0:
            raise MuError("INVALID_ARGUMENT")
        aid = action_id or uid()
        wire = dict(op="action", action=action, action_id=aid, timeout_ms=timeout_ms, **args)
        if self._stopped:
            raise MuError("SESSION_CLOSED")
        if aid in self._action_requests:
            if self._action_requests[aid] != wire:
                raise MuError("INVALID_ARGUMENT", "reused action ID with different arguments")
            if aid in self._receipts:
                return self._receipts[aid]
        self._action_requests[aid] = wire
        self._sequence += 1
        r = Receipt(
            action_id=aid,
            session_epoch=self.epoch,
            sequence=self._sequence,
            execution="unknown",
            effect="unknown",
            provider=self.adapter.provider,
            resolved_method=action,
            evidence=(),
            based_on=based_on,
        )
        try:
            data = await self._request(wire)
        except MuError as e:
            rejected = e.code in (
                "STALE_TARGET",
                "UNSUPPORTED_CAPABILITY",
                "INVALID_ARGUMENT",
                "SESSION_CLOSED",
                "TARGET_NOT_FOREGROUND",
                "TARGET_OCCLUDED",
            )
            r = dataclasses.replace(
                r,
                execution="rejected" if rejected else "unknown",
                effect="not_applied" if rejected else "unknown",
                error=ActionError(
                    code=e.code,
                    message=str(e),
                    retry="observe" if e.code == "STALE_TARGET" else "never",
                ),
            )
            self._receipts[aid] = r
            e.receipt = r
            raise
        r = dataclasses.replace(
            r,
            execution=data["execution"],
            evidence=(Evidence(kind="device_receipt", id=aid),),
        )
        self._receipts[aid] = r
        return r

    async def get_state(self):
        return Inventory(
            devices=(
                DeviceInfo(
                    id=self._device.id,
                    label="Selected device",
                    platform=self.adapter.platform,
                    adapter=self.adapter.provider,
                    connection="ready",
                ),
            )
        )

    async def get_device(self, device_id=None):
        if device_id not in (None, self._device.id):
            raise MuError("TARGET_NOT_FOUND")
        return self._device

    def help(self, topic="core"):
        return "Capture silently with obs = await ui.observe(); deliver with await mu.emit(obs). Use indices from a completed emission in later actions. Screenshot: observe(screenshot=True), then emit the observation. Emit full AX with full=True. Actions, shell, and reads never auto-emit. See docs/prototype.md."

    def begin_turn(self):
        self._turn_image = self._presented_image
        self._in_turn = True

    def end_turn(self):
        self._in_turn = False

    def _present_ax(self, observation):
        if observation._has_tree:
            self._shown[observation.surface.id] = observation
            self._presented_ax[observation.surface.id] = observation

    def _write(self, text):
        print(text, flush=True)

    def _check_image(self, data):
        if not isinstance(data, bytes) or not (
            data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")
        ):
            raise MuError("INVALID_ARGUMENT", "emit image bytes as PNG or JPEG")
        if self.image_sink is None:
            raise MuError("OUTPUT_UNAVAILABLE", "the embedding host must configure an image sink")

    async def emit(self, value, *, full=False, caption=None):
        """Send captured content to the agent; never acquire device state.

        Observation: the requested AX text/diff or captured image.
        Frame/PNG/JPEG bytes: image. String: plain text. JSON data: JSON.
        No return value. Only successful AX delivery advances integer targeting.
        """
        if type(full) is not bool:
            raise MuError("INVALID_ARGUMENT", "full must be boolean")
        if full and not (isinstance(value, Observation) and value._has_tree):
            raise MuError("INVALID_ARGUMENT", "full=True requires an AX observation")
        if caption is not None and not isinstance(caption, str):
            raise MuError("INVALID_ARGUMENT", "caption must be text")
        if isinstance(value, Observation):
            if value._ui._session is not self:
                raise MuError("INVALID_ARGUMENT", "foreign observation")
            data = None
            if value.image.frame is not None:
                data = await self.images.read(value.image.frame)
                self._check_image(data)
            if caption is not None and data is None:
                raise MuError("INVALID_ARGUMENT", "caption requires an image")
            if value._has_tree:
                self._write(value._format_ax(full=full))
                self._present_ax(value)
            if data is not None:
                self.image_sink(data, caption)
                self._presented_image = value
            elif value._screenshot_requested:
                self._write("Screenshot unavailable.")
        elif isinstance(value, (Frame, bytes)):
            data = await self.images.read(value) if isinstance(value, Frame) else value
            self._check_image(data)
            self.image_sink(data, caption)
            # Raw/processed bytes have no trustworthy coordinate mapping.
            self._presented_image = (
                next((o for o in self._observations.values() if o.image.frame is value), None)
                if isinstance(value, Frame)
                else None
            )
        else:
            if caption is not None:
                raise MuError("INVALID_ARGUMENT", "caption requires an image")
            if isinstance(value, AppInventory):
                self._write(format_apps(value))
                return
            if isinstance(value, str):
                self._write(value)
                return
            if isinstance(value, (_NodeRef, PointRef)):
                raise MuError(
                    "INVALID_ARGUMENT",
                    "emit the observation or explicit data, not a target handle",
                )
            if dataclasses.is_dataclass(value) and not isinstance(value, type):
                value = dataclasses.asdict(value)
            elif isinstance(value, Mapping):
                value = dict(value)
            try:
                text = json.dumps(value, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise MuError(
                    "INVALID_ARGUMENT",
                    "emit expects an observation, image, text, or JSON data",
                ) from error
            self._write(text)

    async def close(self):
        if self._stop_receipt:
            return self._stop_receipt
        self._stopped = True
        try:
            r = await self._request(dict(op="stop", timeout_ms=1000))
            ok = r.get("stopped", False)
            result = StopReceipt(
                state="stopped" if ok else "unknown",
                queue_fenced=True,
                in_flight_drained=ok,
                input_released=ok and r.get("input_released", False),
                unresolved_actions=tuple(
                    k for k, v in self._receipts.items() if v.execution == "unknown"
                ),
            )
        except Exception:
            result = StopReceipt(
                state="unknown",
                queue_fenced=True,
                in_flight_drained=False,
                input_released=False,
                unresolved_actions=tuple(self._receipts),
            )
        finally:
            await self.adapter.close()
        self._stop_receipt = result
        return result


class Connection:
    def __init__(
        self,
        *,
        backend=None,
        serial=None,
        adapter_ref=None,
        credential_ref=None,
        endpoint=None,
        protocol_major=1,
        observe_on_connect=True,
    ):
        self.observe_on_connect = observe_on_connect
        if protocol_major != 1:
            raise MuError("PROTOCOL_MISMATCH")
        if endpoint:
            unsupported("network broker transport")
        if adapter_ref:
            if adapter_ref not in _registry:
                unsupported("unregistered adapter")
            self.adapter = _registry[adapter_ref](credential_ref)
        elif backend == "adb":
            if not serial:
                raise MuError("AMBIGUOUS_DEVICE", "prototype requires an explicit ADB serial")
            self.adapter = AdbAdapter(serial, observe_on_connect=observe_on_connect)
        else:
            raise MuError("INVALID_ARGUMENT", "select backend or registered adapter")

    async def __aenter__(self):
        await self.adapter.start()
        self.session = Session(self.adapter)
        try:
            self.session._description = await self.session._request({"op": "describe"})
            if self.session._description.get("platform") != self.adapter.platform:
                raise MuError("PROTOCOL_MISMATCH", "adapter platform mismatch")
            if self.observe_on_connect:
                await self.session._device.ui.observe()
        except BaseException:
            await self.adapter.close()
            raise
        return self.session

    async def __aexit__(self, *args):
        await self.session.close()


def connect(**kwargs):
    return Connection(**kwargs)
