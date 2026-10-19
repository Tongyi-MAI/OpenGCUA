"""Describe foreground pointer footprints; native UI owns overlay geometry."""
from __future__ import annotations

import math


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def impact(device, message):
    if getattr(device, "platform", None) != "macos":
        return None
    if message.get("op") != "action" or message.get("delivery") != "foreground":
        return None
    action = message.get("action")
    if action not in {"click", "point_click", "perform", "move", "drag", "wheel_scroll", "wheel", "button_down"}:
        return None
    observations = getattr(getattr(device, "_backend", None), "_observations", {})
    if message.get("node_id"):
        for observation in reversed(list(observations.values())):
            for node in getattr(getattr(observation, "_tree", None), "nodes", ()):
                if node.id == message["node_id"] and node.bounds is not None:
                    b = node.bounds
                    return {"kind": "pointer", "points": [{"x": b.x + b.width / 2, "y": b.y + b.height / 2}]}
        return None  # SDK will reject an unresolved/stale target before dispatch.
    token = message.get("image_token")
    for observation in reversed(list(observations.values())):
        data = getattr(observation, "_data", {})
        if not token or (data.get("target") or {}).get("image_token") != token:
            continue
        geometry = data.get("geometry") or {}
        if not all(finite(geometry.get(k)) for k in ("x", "y", "width", "height")):
            return {"kind": "unknown_pointer"}
        width, height = data.get("width"), data.get("height")
        if not finite(width) or not finite(height) or width <= 0 or height <= 0:
            return {"kind": "unknown_pointer"}
        def point(x, y):
            return {"x": geometry["x"] + x / width * geometry["width"],
                    "y": geometry["y"] + y / height * geometry["height"]}
        if not finite(message.get("x")) or not finite(message.get("y")):
            return None
        points = [point(message["x"], message["y"])]
        if action == "drag" and finite(message.get("end_x")) and finite(message.get("end_y")):
            points.append(point(message["end_x"], message["end_y"]))
        return {"kind": "pointer", "points": points}
    return None
