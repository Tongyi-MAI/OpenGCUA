"""Translate the existing SDK provider contract into exact Windows CUA calls."""

from __future__ import annotations

import base64
import collections
import math
import re
import struct
import time
import uuid

from transport import DRIVER_VERSION, Error

MAX_WINDOWS = 8
TOKEN = re.compile(r"^s[0-9a-f]{8}:[0-9]+$")


def now():
    return int(time.time() * 1000)


def rect(value):
    if not isinstance(value, dict):
        return None
    result = {
        "x": value.get("x"),
        "y": value.get("y"),
        "width": value.get("width", value.get("w")),
        "height": value.get("height", value.get("h")),
    }
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in result.values()):
        return None
    return result if result["width"] > 0 and result["height"] > 0 else None


def receipt(data):
    effect = data.get("effect")
    return {
        "execution": "rejected" if effect == "refused" else "completed",
        "effect": "not_applied"
        if effect == "refused"
        else effect
        if effect in {"confirmed", "partial", "suspected_noop"}
        else "unknown",
        "mechanism": data.get("route", data.get("path")),
        # The requested delivery is not evidence of actual delivery.
        "delivery": data.get("delivery"),
        "evidence": data.get("evidence") or [],
    }


class Provider:
    def __init__(self, transport, native):
        self.transport, self.native = transport, native
        version = transport.server.get("version")
        if version != DRIVER_VERSION:
            raise Error(
                "VERSION_MISMATCH",
                f"Windows provider requires CUA Driver {DRIVER_VERSION}; received {version!r}",
            )
        self.apps = {}
        self.windows = {}
        self.nodes = {}
        self.snapshots = collections.OrderedDict()
        self.images = collections.OrderedDict()

    def describe(self):
        caps = {
            name: {"state": "ready"}
            for name in (
                "device.list_apps",
                "device.list_windows",
                "ui.observe.tree",
                "ui.observe.screenshot",
                "ui.click.primary",
                "ui.click.coordinates",
                "ui.set_text",
                "ui.perform",
            )
        }
        for name in ("ui.click.primary", "ui.click.coordinates", "ui.set_text", "ui.perform"):
            caps[name]["delivery"] = "background"
        if "type_text" in self.transport.tools:
            caps["ui.input_text"] = {
                "state": "ready",
                "delivery": "background",
                "node_target": "unsupported",
            }
        if "press_key" in self.transport.tools:
            caps["ui.press_key"] = {
                "state": "ready",
                "delivery": "background",
                "modifiers": "unsupported",
                "node_target": "unsupported",
            }
        return {
            "protocol_major": 1,
            "desktop_id": self.native.desktop_id,
            "platform": "windows",
            "provider": "windows.cua",
            "driver_version": DRIVER_VERSION,
            "capabilities": caps,
            "shell_providers": [],
        }

    def _unknown_outcome(self, message, data=None):
        self.transport.close()
        details = None
        if data is not None:
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            details = {
                "native_code": data.get("code", error.get("code")),
                "native": data,
                **receipt(data),
                "execution": "unknown",
                "effect": "unknown",
            }
        raise Error("ACTION_OUTCOME_UNKNOWN", message, details)

    def tool(self, name, args, control, *, mutation=False):
        result = self.transport.tool(name, args, control, mutation=mutation)
        data = result.get("structuredContent")
        if not isinstance(data, dict):
            message = f"{name} returned no structured result"
            if mutation:
                self._unknown_outcome(message)
            raise Error("PROTOCOL_MISMATCH", message)
        # Error envelopes can still describe input that was delivered. Inspect
        # these before interpreting a refusal as proof that nothing happened.
        actual = data.get("delivery")
        if mutation and isinstance(actual, dict) and actual.get("mode") == "foreground":
            self._unknown_outcome(
                "Driver violated background delivery; provider has been closed", data
            )
        if mutation and data.get("execution_state") == "unknown":
            self._unknown_outcome(
                "Driver reported unknown action execution; provider has been closed", data
            )
        detail = data.get("error") if isinstance(data.get("error"), dict) else {}
        native_code = data.get("code", detail.get("code"))
        refused = (
            data.get("effect") == "refused"
            or "refusal" in data
            or data.get("status") in {"refused", "blocked", "denied"}
        )
        if result.get("isError") is True or isinstance(native_code, str) or refused:
            message = " ".join(
                p.get("text", "") for p in result.get("content", []) if p.get("type") == "text"
            )[:2048]
            mapping = {
                "stale_element": "STALE_TARGET",
                "stale_element_token": "STALE_TARGET",
                "stale_snapshot": "STALE_TARGET",
                "stale_capture": "STALE_TARGET",
                "background_unavailable": "INPUT_REFUSED",
                "permission_denied": "PERMISSION_DENIED",
                "target_not_found": "TARGET_NOT_FOUND",
                "window_not_found": "STALE_TARGET",
                "invalid_argument": "INVALID_ARGUMENT",
                "unsupported": "UNSUPPORTED_CAPABILITY",
                "tool_output_invalid": "ACTION_OUTCOME_UNKNOWN",
            }
            code = mapping.get(native_code, "INPUT_REFUSED" if refused else "PROVIDER_ERROR")
            if data.get("execution_state") == "unknown":
                code = "ACTION_OUTCOME_UNKNOWN"
            message = message or data.get("summary") or f"Windows driver refused {name}"
            if mutation and code in {"ACTION_OUTCOME_UNKNOWN", "PROVIDER_ERROR"}:
                self._unknown_outcome(message, data)
            raise Error(
                code,
                message,
                {"native_code": native_code, "native": data, **(receipt(data) if mutation else {})},
            )
        return result, data

    def list_apps(self, control):
        _, data = self.tool("list_apps", {}, control)
        rows = data.get("apps")
        if not isinstance(rows, list):
            raise Error("PROTOCOL_MISMATCH", "list_apps lacks an apps array")
        current = {}
        for row in rows:
            pid = row.get("pid")
            if type(pid) is not int or pid <= 0:
                continue
            try:
                started = self.native.process(pid)
            except Error:
                continue
            identity = f"app:{pid}:{started}"
            current[identity] = {
                "id": identity,
                "instance_id": identity,
                "pid": pid,
                "started": started,
                "label": row.get("name", row.get("app_name", "")),
                "native_id": row.get("bundle_id", row.get("path", "")),
                "launch_path": row.get("launch_path", row.get("path")),
                "running": True,
                "launchable": bool(row.get("launch_path") or row.get("path")),
            }
        self.apps = current
        for wid, window in list(self.windows.items()):
            if window["app_id"] not in current:
                self._drop_window(wid)
        return {
            "apps": [{k: v for k, v in app.items() if k != "started"} for app in current.values()],
            "complete": False,
        }

    def _app(self, app_id):
        app = self.apps.get(app_id)
        if app is None or self.native.process(app["pid"]) != app["started"]:
            raise Error("STALE_TARGET", "Application instance ended; discover it again")
        return app

    def _drop_window(self, wid):
        self.windows.pop(wid, None)
        self.snapshots.pop(wid, None)
        self.nodes = {key: node for key, node in self.nodes.items() if node["window_id"] != wid}
        self.images = collections.OrderedDict(
            (key, image) for key, image in self.images.items() if image["window_id"] != wid
        )

    def list_windows(self, app_id, control):
        args = {"on_screen_only": False}
        if app_id is not None:
            args["pid"] = self._app(app_id)["pid"]
        else:
            self.list_apps(control)
        _, data = self.tool("list_windows", args, control)
        rows = data.get("windows")
        if not isinstance(rows, list):
            raise Error("PROTOCOL_MISMATCH", "list_windows lacks a windows array")
        alive = set()
        output = []
        complete = True
        for row in rows:
            pid, hwnd = row.get("pid"), row.get("window_id")
            if type(pid) is not int or type(hwnd) is not int or hwnd <= 0:
                complete = False
                continue
            app = next((a for a in self.apps.values() if a["pid"] == pid), None)
            if app is None or (app_id is not None and app["id"] != app_id):
                complete = False
                continue
            try:
                self._app(app["id"])
                thread = self.native.window(hwnd, pid)
            except Error:
                complete = False
                continue
            existing = next(
                (
                    w
                    for w in self.windows.values()
                    if w["native_id"] == hwnd and w["app_id"] == app["id"] and w["thread"] == thread
                ),
                None,
            )
            wid = existing["id"] if existing else f"window:{hwnd}:{uuid.uuid4().hex}"
            window = {
                "id": wid,
                "app_id": app["id"],
                "native_id": hwnd,
                "thread": thread,
                "title": row.get("title", ""),
                "bounds": rect(row.get("bounds")),
                "minimized": row.get("minimized"),
                "is_on_screen": row.get("is_on_screen"),
            }
            self.windows[wid] = window
            alive.add(wid)
            output.append({k: window[k] for k in ("id", "app_id", "title", "bounds")})
        for wid, w in list(self.windows.items()):
            if (app_id is None or w["app_id"] == app_id) and wid not in alive:
                self._drop_window(wid)
        return {"windows": output, "complete": complete}

    def _target(self, q, control):
        app_id, wid = q.get("app_id"), q.get("window_id")
        if app_id is None and wid is None:
            raise Error("UNSUPPORTED_CAPABILITY", "Bind an application or exact window first")
        if wid is not None:
            known = self.windows.get(wid)
            if known is None:
                raise Error("STALE_TARGET", "Unknown or ended window instance")
            if app_id is not None and known["app_id"] != app_id:
                raise Error("PERMISSION_DENIED", "Window belongs to another application binding")
            app_id = known["app_id"]
        rows = self.list_windows(app_id, control)["windows"]
        if wid is None:
            if len(rows) != 1:
                raise Error(
                    "AMBIGUOUS_TARGET",
                    "Bind an exact window when an app has zero or multiple windows",
                )
            wid = rows[0]["id"]
        window = self.windows.get(wid)
        if window is None:
            raise Error("STALE_TARGET", "Window ended during target validation")
        self._app(window["app_id"])
        return window

    def observe(self, q, control):
        tree, screenshot = q.get("tree") is True, q.get("screenshot") is True
        if not tree and not screenshot:
            raise Error("INVALID_ARGUMENT", "Observation requires tree or screenshot")
        if q.get("window_id"):
            windows = [self._target(q, control)]
        elif q.get("app_id"):
            rows = self.list_windows(q["app_id"], control)["windows"]
            windows = [self.windows[row["id"]] for row in rows]
        else:
            raise Error(
                "UNSUPPORTED_CAPABILITY", "Windows observations require a bound app or window"
            )
        result = {
            "nodes": [],
            "images": [],
            "components": [],
            "width": 1,
            "height": 1,
            "captured_at": now(),
            "age_ms": 0,
            "source": "windows.cua",
            "revision": uuid.uuid4().hex,
            "truncated": False,
            "pending_invalidations": 0,
            "identity_mode": "snapshot",
            "identity_epoch": None,
        }
        for index, window in enumerate(windows):
            wid = window["id"]
            component = {
                "id": wid,
                "title": window["title"],
                "bounds": window["bounds"],
                "ax_complete": None,
            }
            if index >= MAX_WINDOWS:
                component.update(status="not_captured_limit", limit=MAX_WINDOWS, ax_complete=False)
                result["components"].append(component)
                result["truncated"] = True
                continue
            app = self._app(window["app_id"])
            if tree:
                self.snapshots.pop(wid, None)
                self.nodes = {
                    key: value for key, value in self.nodes.items() if value["window_id"] != wid
                }
            args = {
                "pid": app["pid"],
                "window_id": window["native_id"],
                "include_accessibility_tree": tree,
                "include_screenshot": screenshot,
                "full_output": True,
                "tree_format": "elements",
                "max_elements": 5000,
                "max_image_dimension": 0,
                "timeout_ms": max(
                    100, min(5000, int((control.deadline - time.monotonic()) * 1000) - 100)
                ),
            }
            raw, data = self.tool("get_window_state", args, control)
            if data.get("pid") != app["pid"] or data.get("window_id") != window["native_id"]:
                raise Error("PROTOCOL_MISMATCH", "Capture does not belong to the requested window")
            component.update(status="ok", captured_at=now())
            if tree:
                snapshot = data.get("snapshot_id")
                rows = data.get("elements")
                if not isinstance(snapshot, str) or not isinstance(rows, list):
                    raise Error(
                        "OBSERVATION_FAILED",
                        "Driver did not return a structured accessibility snapshot",
                    )
                self.snapshots[wid] = snapshot
                while len(self.snapshots) > MAX_WINDOWS:
                    expired, _ = self.snapshots.popitem(last=False)
                    self.nodes = {
                        key: value
                        for key, value in self.nodes.items()
                        if value["window_id"] != expired
                    }
                component["ax_complete"] = (
                    data.get("elements_complete") is True and data.get("truncated") is not True
                )
                result["truncated"] |= not component["ax_complete"]
                for row in rows:
                    token = row.get("element_token")
                    if (
                        not isinstance(token, str)
                        or not TOKEN.fullmatch(token)
                        or not token.startswith(snapshot + ":")
                    ):
                        raise Error(
                            "PROTOCOL_MISMATCH",
                            "Node token does not belong to the returned snapshot",
                        )
                    node_id = f"{wid}:{token}"
                    role = row.get("role", "")
                    actions = [
                        a
                        for a in (row.get("actions") or [])
                        if a in {"invoke", "expand", "set_value"}
                    ]
                    node = {
                        "id": node_id,
                        "parent_id": f"{wid}:{snapshot}:{row['parent_index']}"
                        if type(row.get("parent_index")) is int
                        else None,
                        "app_id": app["id"],
                        "window_id": wid,
                        "class_name": role,
                        "automation_id": None,
                        "role": role,
                        "name": row.get("label", ""),
                        "text": row.get("value"),
                        "hint": None,
                        "text_status": "readable" if "value" in row else "unavailable",
                        "bounds": rect(row.get("frame")),
                        "enabled": row.get("enabled"),
                        "visible": None,
                        "focused": None,
                        "checked": row.get("selected"),
                        "editable": role in {"Edit", "Document", "ComboBox"},
                        "editable_source": "role_hint",
                        "value_settable": None,
                        "value_settable_source": "unknown",
                        "password": None,
                        # perform() only exposes operations with an exact
                        # mapping; click and set_text have their own methods.
                        "actions": [{"id": "windows.uia.expand"}] if "expand" in actions else [],
                        "actions_source": "native" if row.get("actions") else "unknown",
                        "target_token": token,
                        "target": {"app_id": app["id"], "window_id": wid, "snapshot_id": snapshot},
                    }
                    self.nodes[node_id] = node
                    result["nodes"].append(node)
            if screenshot:
                image = self._image(raw, data, window)
                result["images"].append(image)
                result["width"], result["height"] = image["width"], image["height"]
            result["components"].append(component)
        return result

    def _image(self, raw, data, window):
        image = next((part for part in raw.get("content", []) if part.get("type") == "image"), None)
        if image is None or image.get("mimeType") != "image/png":
            raise Error("OBSERVATION_FAILED", "Driver did not return the requested PNG")
        try:
            binary = base64.b64decode(image["data"], validate=True)
        except (ValueError, KeyError) as exc:
            raise Error("PROTOCOL_MISMATCH", "Invalid screenshot encoding") from exc
        if len(binary) < 24 or binary[:8] != b"\x89PNG\r\n\x1a\n":
            raise Error("PROTOCOL_MISMATCH", "Invalid PNG signature")
        width, height = struct.unpack(">II", binary[16:24])
        if (
            not width
            or not height
            or data.get("screenshot_width", width) != width
            or data.get("screenshot_height", height) != height
        ):
            raise Error("PROTOCOL_MISMATCH", "PNG dimensions contradict capture metadata")
        bounds = rect(data.get("window_bounds"))
        token = uuid.uuid4().hex
        # A fresh image replaces this window's coordinate groundings only. An
        # image-only observation does not invalidate accessibility references.
        self.images = collections.OrderedDict(
            (key, item) for key, item in self.images.items() if item["window_id"] != window["id"]
        )
        self.images[token] = {
            "window_id": window["id"],
            "app_id": window["app_id"],
            "capture_id": data.get("capture_id"),
            "bounds": bounds,
            "width": width,
            "height": height,
        }
        while len(self.images) > MAX_WINDOWS:
            self.images.popitem(last=False)
        return {
            "width": width,
            "height": height,
            "png": image["data"],
            "mime_type": "image/png",
            "captured_at": now(),
            "geometry": bounds,
            "status": "ok",
            "label": window["title"],
            "target": {"image_token": token, "app_id": window["app_id"], "window_id": window["id"]},
        }

    def action(self, q, control):
        if q.get("delivery", "auto") not in {"auto", "background"}:
            raise Error(
                "UNSUPPORTED_CAPABILITY", "This Windows preview supports background delivery only"
            )
        action = q.get("action")
        if action not in {"click", "point_click", "set_text", "perform", "input_text", "press_key"}:
            raise Error("UNSUPPORTED_CAPABILITY", f"Windows action {action!r} is not implemented")
        window = self._target(q, control)
        app = self._app(window["app_id"])
        args = {"pid": app["pid"], "window_id": window["native_id"], "delivery_mode": "background"}
        node = None
        if q.get("node_id") is not None:
            node = self.nodes.get(q["node_id"])
            if (
                node is None
                or node["target_token"] != q.get("target_token")
                or node["window_id"] != window["id"]
                or self.snapshots.get(window["id"]) != q.get("snapshot_id")
            ):
                raise Error(
                    "STALE_TARGET", "Accessibility reference is stale or belongs to another window"
                )
            args["element_token"] = node["target_token"]
        if q.get("modifiers"):
            raise Error(
                "UNSUPPORTED_CAPABILITY", "Background modifier-key semantics are not supported"
            )
        if action in {"click", "set_text", "perform"} and node is None:
            raise Error("INVALID_ARGUMENT", "This operation requires a snapshot-bound node")
        if action == "set_text" and node["role"] not in {"Edit", "Document"}:
            raise Error(
                "UNSUPPORTED_CAPABILITY",
                "String replacement is not established for this control type",
            )
        if action in {"input_text", "press_key"} and (
            node is not None or q.get("image_token") is not None or q.get("x") is not None
        ):
            raise Error(
                "UNSUPPORTED_CAPABILITY",
                "Targeted insertion/key delivery cannot preserve caret semantics; use current window editing context",
            )
        tool = {
            "click": "click",
            "point_click": "click",
            "set_text": "set_value",
            "perform": "click",
            "input_text": "type_text",
            "press_key": "press_key",
        }[action]
        if action in {"click", "point_click"}:
            if q.get("button", "primary") != "primary" or q.get("count", 1) != 1:
                raise Error(
                    "UNSUPPORTED_CAPABILITY", "Windows preview supports single primary clicks only"
                )
            args.update(button="left", count=1)
        if action == "point_click":
            if node is not None:
                raise Error("INVALID_ARGUMENT", "A coordinate click cannot also carry an AX target")
            image = self.images.get(q.get("image_token"))
            if image is None or image["window_id"] != window["id"] or not image["capture_id"]:
                raise Error(
                    "STALE_TARGET",
                    "Coordinate action requires the latest capture of this exact window",
                )
            if (
                image["bounds"] is None
                or window["bounds"] is None
                or image["bounds"] != window["bounds"]
            ):
                raise Error("GEOMETRY_CHANGED", "Window moved or resized; capture it again")
            x, y = q.get("x"), q.get("y")
            if (
                any(type(v) not in (int, float) or not math.isfinite(v) for v in (x, y))
                or not 0 <= x < image["width"]
                or not 0 <= y < image["height"]
            ):
                raise Error("INVALID_ARGUMENT", "Point falls outside the captured image")
            args.update(x=x, y=y, capture_id=image["capture_id"])
            # Driver capture IDs are consumable. Never promise their reuse even
            # if the action later has an unknown outcome.
            self.images.pop(q["image_token"])
        if action in {"set_text", "input_text"}:
            if not isinstance(q.get("text"), str):
                raise Error("INVALID_ARGUMENT", "text must be a string")
            args["value" if action == "set_text" else "text"] = q["text"]
        if action == "perform":
            named = q.get("node_action")
            if named != "windows.uia.expand" or {"id": named} not in node["actions"]:
                raise Error(
                    "UNSUPPORTED_CAPABILITY", "Only an observed native expand action is supported"
                )
            args["action"] = "expand"
        if action == "press_key":
            key = q.get("key")
            if not isinstance(key, str):
                raise Error("INVALID_ARGUMENT", "key must be a string")
            key = {
                "enter": "return",
                "esc": "escape",
                "page_up": "pageup",
                "page_down": "pagedown",
                "backspace": "backspace",
            }.get(key, key)
            if not (
                key
                in {
                    "return",
                    "escape",
                    "tab",
                    "space",
                    "delete",
                    "backspace",
                    "home",
                    "end",
                    "pageup",
                    "pagedown",
                    "up",
                    "down",
                    "left",
                    "right",
                }
                or re.fullmatch(r"[a-z0-9]|f(?:[1-9]|1[0-2])", key)
            ):
                raise Error("UNSUPPORTED_CAPABILITY", "Unsupported Windows key")
            args["key"] = key
        _, data = self.tool(tool, args, control, mutation=True)
        return receipt(data)

    def dispatch(self, q, control):
        control.check()
        if any(q.get(k) is not None for k in ("tab_id", "display_id")):
            raise Error(
                "UNSUPPORTED_CAPABILITY", "Windows provider supports application/window scopes only"
            )
        op = q.get("op")
        if op == "describe":
            return self.describe()
        if op == "keepalive":
            if self.transport.closed or self.transport.failed:
                raise Error("DEVICE_OFFLINE", "Windows driver is closed")
            return {"alive": True}
        self.native.check_desktop()
        if op == "list_apps":
            return self.list_apps(control)
        if op == "list_windows":
            return self.list_windows(q.get("app_id"), control)
        if op == "observe":
            return self.observe(q, control)
        if op == "action":
            return self.action(q, control)
        if op in {"release_inputs", "stop"}:
            return {"execution": "completed", "stopped": True}
        raise Error("UNSUPPORTED_CAPABILITY", f"Windows operation {op!r} is not implemented")

    def close(self):
        self.transport.close()
        self.native.close()
