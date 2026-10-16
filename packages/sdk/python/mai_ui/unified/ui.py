"""Small modality facades sharing target validation and dispatch."""

from __future__ import annotations

import json

from .._errors import MuError
from ._capture import PointRef
from .observations import AXNode, AXObservation, Image, VisionObservation
from .runtime import native, timeout


class ActionBase:
    def __init__(self, handle):
        self._handle = handle

    def _input_timeout(self, timeout_ms, delivery, *, default=3000):
        # Native macOS delivery can include activation and focus restoration.
        # Preview exceeds the ordinary three-second budget, including scrolling.
        if timeout_ms is None:
            return 15000 if self._handle.platform == "macos" else default
        return timeout_ms

    def _node(self, target):
        h = self._handle
        h._check()
        if type(target) is int:
            if target < 1:
                raise MuError("STALE_TARGET")
            value = json.loads(
                native(
                    h._session._runtime._core.resolve_index, h._scope, target, h._session._in_turn
                )
            )
            target = AXNode(
                h._backend._observations[value["observation_id"]]._ref(value["node_id"])
            )
        if not isinstance(target, AXNode) or target._ref._owner is not h:
            raise MuError("INVALID_ARGUMENT", "expected an AX node from this UI scope")
        ref = target._ref
        native(h._session._runtime._core.node_wire, h._scope, ref.observation_id, ref.id)
        return ref

    def _point(self, x, y=None, *, image=None):
        h = self._handle
        h._check()
        if isinstance(x, PointRef):
            if y is not None or image is not None:
                raise MuError("INVALID_ARGUMENT", "a point already contains its image basis")
            if x._owner is not h:
                raise MuError("INVALID_ARGUMENT", "point belongs to a different UI scope")
            native(h._session._runtime._core.point_wire, h._scope, x._token)
            return x
        if isinstance(x, AXNode):
            if y is not None:
                raise MuError("INVALID_ARGUMENT", "a node target takes no y coordinate")
            return self._node_point(x, image=image)
        image = self._resolve_image(image)
        return image.point(x, y, space=h._session.coordinate_space)

    def _resolve_image(self, image):
        h = self._handle
        images = h._turn_images if h._session._in_turn else h._presented_images
        if image is None:
            if len(images) != 1:
                raise MuError(
                    "AMBIGUOUS_TARGET" if images else "STALE_TARGET",
                    "emit one image in a previous cell, or supply a captured Image",
                )
            image = next(iter(images.values()))
        elif isinstance(image, str):
            image = images.get(image)
            if image is None:
                raise MuError("STALE_TARGET", "image ID is not in the delivered view")
        if not isinstance(image, Image) or image._handle is not h:
            raise MuError("INVALID_ARGUMENT", "image belongs to a different UI scope")
        return image

    def _node_point(self, node, *, image=None):
        """Click a node with no reliable AX action by mapping its screen bounds into
        the captured window image (host does the origin/scale math, not the model)."""
        h = self._handle
        if node._ref._owner is not h:
            raise MuError("INVALID_ARGUMENT", "node belongs to a different UI scope")
        bounds = node.bounds
        if bounds is None:
            raise MuError("INVALID_ARGUMENT", "node has no bounds; click via image.point()")
        image = self._resolve_image(image)
        geo = image.geometry
        if not isinstance(geo, dict) or any(geo.get(k) is None for k in ("x", "y", "width", "height")):
            raise MuError(
                "UNSUPPORTED_CAPABILITY",
                "image carries no window geometry; click via image.point() pixels",
            )
        gw, gh = geo["width"], geo["height"]
        if not (gw > 0 and gh > 0):
            raise MuError("STALE_TARGET", "window geometry is not positive; observe again")
        scale_x, scale_y = image.width / gw, image.height / gh
        px = (bounds.x + bounds.width / 2 - geo["x"]) * scale_x
        py = (bounds.y + bounds.height / 2 - geo["y"]) * scale_y
        for value, size, axis in ((px, image.width, "x"), (py, image.height, "y")):
            if not 0 <= value <= size - 1:
                raise MuError(
                    "INVALID_ARGUMENT",
                    f"node maps to {axis}={value:.1f}, outside the image (0-{size - 1}); "
                    "it may have scrolled out of view\u2014scroll and re-observe",
                )
        return image.point(px, py, space="pixels")

    async def _action(
        self,
        action,
        target=None,
        *,
        capability,
        timeout_ms=None,
        action_id=None,
        delivery="auto",
        **args,
    ):
        timeout_ms = self._input_timeout(timeout_ms, delivery)
        timeout(timeout_ms)
        h = self._handle
        h._check(write=True)
        h._require(capability)
        if delivery not in ("auto", "background", "foreground"):
            raise MuError("INVALID_ARGUMENT", "unknown delivery policy")
        if h.platform in ("macos", "browser"):
            args["delivery"] = "background" if delivery == "auto" else delivery
        elif delivery == "background":
            raise MuError("UNSUPPORTED_CAPABILITY", "background input is unavailable")
        if target is not None:
            if target._owner is not h:
                raise MuError("INVALID_ARGUMENT")
            core = h._session._runtime._core
            wire = (
                native(core.point_wire, h._scope, target._token)
                if isinstance(target, PointRef)
                else native(core.node_wire, h._scope, target.observation_id, target.id)
            )
            args.update(json.loads(wire))
        if h._app_id:
            args["app_id"] = h._app_id
        args["_capability"] = capability
        return await h._backend._action(
            action,
            timeout_ms=timeout_ms,
            action_id=action_id,
            based_on=target.observation_id if target is not None else None,
            **args,
        )


class AX(ActionBase):
    async def observe(self, *, timeout_ms=None, max_nodes=None, max_depth=None):
        timeout_ms = (
            (20000 if self._handle.platform == "macos" else 1500)
            if timeout_ms is None
            else timeout_ms
        )
        timeout(timeout_ms)
        self._handle._require("ui.observe.tree")
        if max_nodes is not None or max_depth is not None:
            raise MuError("UNSUPPORTED_CAPABILITY", "AX capture limits are WIP")
        captured = await self._handle._backend.capture(tree=True, timeout_ms=timeout_ms)
        return AXObservation(self._handle, captured)

    async def click(
        self, target, *, button="primary", count=1, timeout_ms=None, action_id=None, delivery="auto"
    ):
        if self._handle.platform == "android" and (button != "primary" or count != 1):
            raise MuError("UNSUPPORTED_CAPABILITY", "Android AX supports one primary click")
        return await self._action(
            "click",
            self._node(target),
            capability="ui.click.primary",
            button=button,
            count=count,
            timeout_ms=self._input_timeout(timeout_ms, delivery),
            action_id=action_id,
            delivery=delivery,
        )

    async def set_text(self, target, text, *, timeout_ms=None, action_id=None, delivery="auto"):
        if not isinstance(text, str):
            raise MuError("INVALID_ARGUMENT", "text must be a string")
        return await self._action(
            "set_text",
            self._node(target),
            capability="ui.set_text",
            text=text,
            timeout_ms=self._input_timeout(timeout_ms, delivery, default=5000),
            action_id=action_id,
            delivery=delivery,
        )

    async def select_text(
        self, target, start, end, *, timeout_ms=None, action_id=None, delivery="auto"
    ):
        ref = self._node(target)
        text = ref.node.text or ""
        if (
            ref.node.password
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start <= end <= len(text)
        ):
            raise MuError("INVALID_ARGUMENT", "selection requires a readable code-point range")
        a, b = start, end
        return await self._action(
            "select_text",
            ref,
            capability="ui.select_text",
            start=a,
            end=b,
            selection_text=text,
            offset_unit="code_points",
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )

    async def perform(
        self, target, action, *, args=None, timeout_ms=None, action_id=None, delivery="auto"
    ):
        ref = self._node(target)
        if args or str(action) not in {a.id for a in ref.node.actions}:
            raise MuError(
                "UNSUPPORTED_CAPABILITY", "use an advertised node action; generic arguments are WIP"
            )
        return await self._action(
            "perform",
            ref,
            capability="ui.perform",
            node_action=str(action),
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )

    async def scroll(
        self,
        target,
        direction,
        *,
        amount=1,
        unit="viewport",
        timeout_ms=None,
        action_id=None,
        delivery="auto",
    ):
        ref = self._node(target)
        if direction not in ("up", "down", "left", "right"):
            raise MuError("INVALID_ARGUMENT", "unknown direction")
        if unit == "native_steps":
            if direction not in ("up", "down") or amount != 1:
                raise MuError("UNSUPPORTED_CAPABILITY", "one native up/down step is supported")
            return await self._action(
                "scroll",
                ref,
                capability="ui.scroll.steps",
                direction=direction,
                timeout_ms=timeout_ms,
                action_id=action_id,
                delivery=delivery,
            )
        if (
            unit != "viewport"
            or isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not 0 < amount <= 1
        ):
            raise MuError(
                "UNSUPPORTED_CAPABILITY", "viewport scrolling supports an amount in (0, 1]"
            )
        return await self._action(
            "node_scroll",
            ref,
            capability="ui.scroll.viewport",
            direction=direction,
            amount=amount,
            duration_ms=500,
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )


class Vision(ActionBase):
    async def observe(self, *, timeout_ms=None):
        timeout_ms = 10000 if timeout_ms is None else timeout_ms
        timeout(timeout_ms)
        self._handle._require("ui.observe.screenshot")
        if self._handle._app_id and self._handle.platform == "android":
            raise MuError(
                "UNSUPPORTED_CAPABILITY",
                "isolated application screenshots are WIP; use the whole-device UI",
            )
        captured = await self._handle._backend.capture(
            tree=False, screenshot=True, timeout_ms=timeout_ms
        )
        return VisionObservation(self._handle, captured)

    async def click(
        self,
        x,
        y=None,
        *,
        image=None,
        button="primary",
        count=1,
        timeout_ms=None,
        action_id=None,
        delivery="auto",
    ):
        if self._handle.platform == "android" and (button != "primary" or count != 1):
            raise MuError("UNSUPPORTED_CAPABILITY", "Android supports one primary tap")
        return await self._action(
            "point_click",
            self._point(x, y, image=image),
            capability="ui.click.coordinates",
            button=button,
            count=count,
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )

    async def swipe(
        self, start, end, *, duration_ms=500, timeout_ms=None, action_id=None, delivery="auto"
    ):
        timeout_ms = self._input_timeout(timeout_ms, delivery)
        start, end = self._point(start), self._point(end)
        if start.observation_id != end.observation_id:
            raise MuError("INVALID_ARGUMENT", "swipe points must use the same captured image")
        if (
            type(duration_ms) is not int
            or not 50 <= duration_ms <= 5000
            or duration_ms >= timeout_ms
        ):
            raise MuError("INVALID_ARGUMENT", "duration must be 50..5000 ms and less than timeout")
        return await self._action(
            "point_scroll",
            start,
            capability="ui.scroll.coordinates",
            end_x=end.x,
            end_y=end.y,
            duration_ms=duration_ms,
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )

    async def long_press(
        self,
        x,
        y=None,
        *,
        image=None,
        duration_ms=800,
        timeout_ms=None,
        action_id=None,
        delivery="auto",
    ):
        timeout_ms = self._input_timeout(timeout_ms, delivery)
        if (
            type(duration_ms) is not int
            or not 50 <= duration_ms <= 5000
            or duration_ms >= timeout_ms
        ):
            raise MuError("INVALID_ARGUMENT", "duration must fit within the action timeout")
        return await self._action(
            "point_long_press",
            self._point(x, y, image=image),
            capability="ui.long_press",
            duration_ms=duration_ms,
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )

    async def scroll(
        self,
        direction,
        *,
        x,
        y,
        image=None,
        amount=1,
        unit="viewport",
        timeout_ms=None,
        action_id=None,
        delivery="auto",
    ):
        point = self._point(x, y, image=image)
        if self._handle.platform in ("macos", "browser", "linux"):
            if direction not in ("up", "down", "left", "right"):
                raise MuError("INVALID_ARGUMENT", "unknown direction")
            return await self._action(
                "wheel_scroll",
                point,
                capability="ui.scroll.coordinates",
                direction=direction,
                amount=amount,
                unit=unit,
                timeout_ms=timeout_ms,
                action_id=action_id,
                delivery=delivery,
            )
        if (
            direction not in ("up", "down", "left", "right")
            or unit != "viewport"
            or type(amount) not in (int, float)
            or not 0 < amount <= 1
        ):
            raise MuError("INVALID_ARGUMENT", "use up/down/left/right and viewport amount in (0,1]")
        frame = self._handle._backend._observations[point.observation_id].image.frame
        horizontal = direction in ("left", "right")
        delta = 0.8 * amount * ((frame.width - 1) if horizontal else (frame.height - 1))
        sign = -1 if direction in ("down", "right") else 1
        # Center the requested trajectory around the supplied point, then shift it
        # inside the image while preserving distance (never silently shorten it).
        extent = (frame.width - 1) if horizontal else (frame.height - 1)
        center = point.x if horizontal else point.y
        low = min(max(0, center - delta / 2), extent - delta)
        a, b = (low + delta, low) if sign < 0 else (low, low + delta)
        captured = self._handle._backend._observations[point.observation_id]
        start = captured.point(
            a if horizontal else point.x, point.y if horizontal else a, max_age_ms=120000
        )
        end = captured.point(
            b if horizontal else point.x, point.y if horizontal else b, max_age_ms=120000
        )
        return await self.swipe(
            start, end, timeout_ms=timeout_ms, action_id=action_id, delivery=delivery
        )

    async def move(
        self, x, y=None, *, image=None, steps=1, timeout_ms=None, action_id=None, delivery="auto"
    ):
        return await self._action(
            "move",
            self._point(x, y, image=image),
            capability="ui.move",
            steps=steps,
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )

    async def drag(
        self,
        start,
        end,
        *,
        hold_ms=None,
        duration_ms=500,
        timeout_ms=5000,
        action_id=None,
        delivery="auto",
    ):
        start, end = self._point(start), self._point(end)
        if (
            start.observation_id != end.observation_id
            or type(duration_ms) is not int
            or not 50 <= duration_ms < timeout_ms
        ):
            raise MuError(
                "INVALID_ARGUMENT",
                "drag requires two points in the same capture and a bounded duration",
            )
        return await self._action(
            "drag",
            start,
            capability="ui.drag",
            end_x=end.x,
            end_y=end.y,
            duration_ms=duration_ms,
            hold_ms=hold_ms,
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )

    async def button_down(
        self,
        x,
        y=None,
        *,
        image=None,
        button="primary",
        timeout_ms=None,
        action_id=None,
        delivery="auto",
    ):
        return await self._action(
            "button_down",
            self._point(x, y, image=image),
            capability="ui.button_state",
            button=button,
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )

    async def button_up(
        self, button="primary", *, timeout_ms=None, action_id=None, delivery="auto"
    ):
        return await self._action(
            "button_up",
            capability="ui.button_state",
            button=button,
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )

    async def wheel(
        self,
        delta_x,
        delta_y,
        *,
        unit,
        x,
        y,
        image=None,
        timeout_ms=None,
        action_id=None,
        delivery="auto",
    ):
        return await self._action(
            "wheel",
            self._point(x, y, image=image),
            capability="ui.wheel",
            delta_x=delta_x,
            delta_y=delta_y,
            unit=unit,
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )


class Input(ActionBase):
    async def key_down(self, key, *, timeout_ms=None, action_id=None, delivery="auto"):
        return await self._action(
            "key_down",
            capability="ui.key_state",
            key=key,
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )

    async def key_up(self, key, *, timeout_ms=None, action_id=None, delivery="auto"):
        return await self._action(
            "key_up",
            capability="ui.key_state",
            key=key,
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )

    async def release_all(self, *, timeout_ms=None, action_id=None, delivery="auto"):
        return await self._action(
            "release_all",
            capability="ui.release_all",
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )

    async def text(self, text, *, target=None, timeout_ms=None, action_id=None, delivery="auto"):
        if not isinstance(text, str):
            raise MuError("INVALID_ARGUMENT", "text must be a string")
        if target is not None:
            target = self._point(target) if isinstance(target, PointRef) else self._node(target)
        timeout_ms = self._input_timeout(timeout_ms, delivery, default=5000)
        if target is not None and timeout_ms <= 500:
            raise MuError("INVALID_ARGUMENT", "timeout must allow the target focus wait")
        return await self._action(
            "input_text",
            target,
            capability="ui.input_text",
            text=text,
            timeout_ms=timeout_ms,
            action_id=action_id,
            delivery=delivery,
        )

    async def press_key(
        self, key, *, modifiers=(), target=None, timeout_ms=None, action_id=None, delivery="auto"
    ):
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(modifiers, (list, tuple))
            or not all(isinstance(m, str) for m in modifiers)
        ):
            raise MuError("INVALID_ARGUMENT", "provide a key and modifier names")
        if target is not None:
            target = self._point(target) if isinstance(target, PointRef) else self._node(target)
        return await self._action(
            "press_key",
            target,
            capability="ui.press_key",
            key=key if self._handle.platform == "browser" else key.lower(),
            modifiers=list(modifiers),
            timeout_ms=self._input_timeout(timeout_ms, delivery),
            action_id=action_id,
            delivery=delivery,
        )


class UI:
    def __init__(self, handle):
        self._handle = handle
        self.ax = AX(handle)
        self.vision = Vision(handle)
        self.input = Input(handle)

    async def wait_for_stabilize(self, *, timeout_ms=5000) -> None:
        """Wait using the adapter's policy; Android currently delays two seconds.

        This does not capture, emit, or guarantee that the application is stable.
        """
        timeout(timeout_ms)
        self._handle._require("ui.wait_for_stabilize")
        message = dict(op="wait_for_stabilize", timeout_ms=timeout_ms)
        if self._handle._app_id:
            message["app_id"] = self._handle._app_id
        await self._handle._backend._request(message)
