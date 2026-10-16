"""Explicit X11 connections and XTEST input; no desktop-tool screenshot fallback."""

import ctypes
import ctypes.util
import hashlib
import io
import json
import math
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections import OrderedDict

from common import Error, b64, completed

KEYS = {
    "enter": "Return",
    "return": "Return",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    "left": "Left",
    "right": "Right",
    "up": "Up",
    "down": "Down",
    "space": "space",
    "shift": "Shift_L",
    "ctrl": "Control_L",
    "control": "Control_L",
    "alt": "Alt_L",
    "super": "Super_L",
    "win": "Super_L",
    "meta": "Super_L",
    "capslock": "Caps_Lock",
    "printscreen": "Print",
    "pause": "Pause",
}
KEYS.update({f"f{i}": f"F{i}" for i in range(1, 25)})


def cursor_rgba(pixels):
    # XFixes returns premultiplied ARGB. Pillow alpha compositing needs straight RGBA.
    data = bytearray()
    for pixel in pixels:
        alpha = (pixel >> 24) & 255
        rgb = [(pixel >> shift) & 255 for shift in (16, 8, 0)]
        data.extend([min(255, round(c * 255 / alpha)) if alpha else 0 for c in rgb] + [alpha])
    return bytes(data)


class Desktop:
    def __init__(self, event):
        from PIL import Image, ImageGrab, features
        from Xlib import XK, X, display
        from Xlib.ext import xtest

        self.X, self.XK, self.xtest = X, XK, xtest
        self.Image, self.ImageGrab = Image, ImageGrab
        self.event = event
        if os.environ.get("XDG_SESSION_TYPE") == "wayland" and not os.environ.get(
            "MAI_UI_EXPLICIT_X11"
        ):
            raise Error(
                "UNSUPPORTED_CAPABILITY", "Native Wayland is deferred; connect to an X11 desktop"
            )
        self.display_name = os.environ.get("DISPLAY", "")
        if not self.display_name:
            raise Error("DEVICE_OFFLINE", "DISPLAY is missing; run in the graphical user session")
        try:
            self.d = display.Display(self.display_name)
        except Exception as exc:
            raise Error("DEVICE_OFFLINE", f"Cannot connect to X11: {exc}") from exc
        self.keys, self.buttons, self.images = set(), set(), OrderedDict()
        try:
            if self.d.screen_count() != 1 or not features.check_feature("xcb"):
                raise Error(
                    "UNSUPPORTED_CAPABILITY", "One X11 screen and Pillow XCB support are required"
                )
            for extension in ("XTEST", "XFIXES", "RANDR"):
                if not self.d.has_extension(extension):
                    raise Error("UNSUPPORTED_CAPABILITY", f"X11 extension {extension} is required")
            # Xwayland exposes this extension even when DISPLAY has been explicitly supplied.
            if self.d.has_extension("XWAYLAND"):
                raise Error("UNSUPPORTED_CAPABILITY", "Xwayland is not a whole-desktop X11 backend")
            self.d.xfixes_query_version()
            self.root = self.d.screen().root
            self.epoch = uuid.uuid4().hex
            self.geometry = None
            self.geometry_version = 0
            self.refresh_geometry()
            # Canonicalize local DISPLAY spellings; root XID and boot identity distinguish servers.
            address = self.d.get_display_name()
            number = address.rsplit(":", 1)[-1].split(".")[0]
            if self.d.display.socket.family != socket.AF_UNIX:
                raise Error(
                    "UNSUPPORTED_CAPABILITY", "Only local Unix-socket X11 connections are supported"
                )
            host = "local"
            try:
                boot = open("/proc/sys/kernel/random/boot_id").read().strip()
            except OSError:
                boot = socket.gethostname()
            self.desktop_id = (
                "linux:x11:"
                + hashlib.sha256(
                    f"{boot}:{os.getuid()}:{host}:{number}:{self.root.id}".encode()
                ).hexdigest()[:32]
            )
            self.keyboard_map()
            self.guard = subprocess.Popen(
                [sys.executable, __file__, "--input-guard"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            if self.guard.stdout.readline().strip() != "ready":
                raise Error("DEVICE_OFFLINE", "Input cleanup guard did not start")
        except BaseException:
            self.d.close()
            raise

    def refresh_geometry(self):
        geom = self.root.get_geometry()
        monitors = self.root.xrandr_get_monitors(True).monitors
        if len(monitors) != 1:
            raise Error("UNSUPPORTED_CAPABILITY", "Exactly one active X11 monitor is supported")
        m = monitors[0]
        if (m.x, m.y, m.width_in_pixels, m.height_in_pixels) != (0, 0, geom.width, geom.height):
            raise Error("UNSUPPORTED_CAPABILITY", "Monitor must cover the complete X11 screen")
        value = (geom.width, geom.height)
        if value != self.geometry:
            self.geometry = value
            self.geometry_version += 1
            self.images.clear()
            self.event(
                "geometry_changed",
                {"generation": self.geometry_version, "width": value[0], "height": value[1]},
            )
        return value

    def observe(self, control):
        control.check()
        width, height = self.refresh_geometry()
        generation = self.geometry_version
        image = self.ImageGrab.grab(xdisplay=self.display_name).convert("RGBA")
        if image.size != (width, height):
            raise Error("GEOMETRY_CHANGED")
        cursor = self.d.xfixes_get_cursor_image(self.root)
        if cursor.width and cursor.height:
            overlay = self.Image.frombytes(
                "RGBA", (cursor.width, cursor.height), cursor_rgba(cursor.cursor_image)
            )
            image.alpha_composite(overlay, (cursor.x - cursor.xhot, cursor.y - cursor.yhot))
        self.refresh_geometry()
        if generation != self.geometry_version:
            raise Error("GEOMETRY_CHANGED")
        control.check()
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="PNG")
        token = uuid.uuid4().hex
        self.images[token] = (generation, time.monotonic())
        while len(self.images) > 256:
            self.images.popitem(last=False)
        return dict(
            width=width,
            height=height,
            png=b64(buf.getvalue()),
            mime_type="image/png",
            captured_at=time.time(),
            rotation=0,
            label="Linux / display:0",
            status="ok",
            geometry=dict(x=0, y=0, width=width, height=height, scale=1, generation=generation),
            target={"image_token": token},
        )

    def point(self, q, end=False):
        width, height = self.refresh_geometry()
        captured = self.images.get(q.get("image_token"))
        if (
            not captured
            or captured[0] != self.geometry_version
            or time.monotonic() - captured[1] > 120
        ):
            raise Error("STALE_TARGET", "Image expired or desktop geometry changed")
        x, y = q.get("end_x" if end else "x"), q.get("end_y" if end else "y")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (x, y)) or not (
            0 <= x < width and 0 <= y < height
        ):
            raise Error("INVALID_ARGUMENT", "Point outside captured screen")
        return int(x), int(y)

    def move(self, point):
        self.xtest.fake_input(self.d, self.X.MotionNotify, x=point[0], y=point[1])
        self.d.sync()

    def track(self, kind, code, down):
        self.guard.stdin.write(json.dumps([kind, code, down]) + "\n")
        self.guard.stdin.flush()

    def button(self, button, down):
        if down:
            self.track("button", button, True)
            self.buttons.add(button)
        self.xtest.fake_input(self.d, self.X.ButtonPress if down else self.X.ButtonRelease, button)
        self.d.sync()
        if not down:
            self.buttons.discard(button)
            self.track("button", button, False)

    def key(self, code, down):
        if down:
            self.track("key", code, True)
            self.keys.add(code)
        self.xtest.fake_input(self.d, self.X.KeyPress if down else self.X.KeyRelease, code)
        self.d.sync()
        if not down:
            self.keys.discard(code)
            self.track("key", code, False)

    def release(self):
        for code in list(self.keys):
            self.key(code, False)
        for button in list(self.buttons):
            self.button(button, False)

    def keyboard_map(self):
        info = self.d.display.info
        rows = self.d.get_keyboard_mapping(
            info.min_keycode, info.max_keycode - info.min_keycode + 1
        )
        mapping = {}
        # Only group 1 unshifted/shifted is portable through core XTEST. Other levels are rejected.
        for code, row in enumerate(rows, info.min_keycode):
            for level, sym in enumerate(row[:2]):
                if sym:
                    mapping.setdefault(sym, (code, bool(level)))
        self.mapping = mapping
        self.shift = self.mapping.get(self.XK.string_to_keysym("Shift_L"), (None, False))[0]
        if self.shift is None:
            raise Error("UNSUPPORTED_CAPABILITY", "Keyboard has no Shift mapping")
        return rows

    def symbol(self, text, named=False):
        name = (
            KEYS.get(text.lower(), text)
            if named
            else {"\n": "Return", "\r": "Return", "\t": "Tab", " ": "space"}.get(text, text)
        )
        sym = self.XK.string_to_keysym(name)
        if not sym and len(name) == 1:
            sym = ord(name) if ord(name) < 256 else 0x01000000 | ord(name)
        found = self.mapping.get(sym)
        if not found:
            raise Error(
                "UNSUPPORTED_CAPABILITY",
                f"Key/text is not mapped by the active layout: {text!r}; use AX set_text or explicit clipboard paste",
            )
        return found

    def keyboard_state(self):
        class State(ctypes.Structure):
            _fields_ = [
                ("group", ctypes.c_ubyte),
                ("locked_group", ctypes.c_ubyte),
                ("base_group", ctypes.c_ushort),
                ("latched_group", ctypes.c_ushort),
                ("mods", ctypes.c_ubyte),
                ("base_mods", ctypes.c_ubyte),
                ("latched_mods", ctypes.c_ubyte),
                ("locked_mods", ctypes.c_ubyte),
                ("compat_state", ctypes.c_ubyte),
                ("grab_mods", ctypes.c_ubyte),
                ("compat_grab_mods", ctypes.c_ubyte),
                ("lookup_mods", ctypes.c_ubyte),
                ("compat_lookup_mods", ctypes.c_ubyte),
                ("ptr_buttons", ctypes.c_ushort),
            ]

        lib = ctypes.CDLL(ctypes.util.find_library("X11"))
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XkbGetState.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(State)]
        lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        connection = lib.XOpenDisplay(self.display_name.encode())
        if not connection:
            raise Error("DEVICE_OFFLINE")
        try:
            state = State()
            if lib.XkbGetState(connection, 0x0100, ctypes.byref(state)) != 0:
                raise Error("UNSUPPORTED_CAPABILITY", "XKB state is unavailable")
            if state.group or state.latched_group or state.latched_mods:
                raise Error(
                    "UNSUPPORTED_CAPABILITY",
                    "Alternate/latched keyboard groups require explicit clipboard or AX text",
                )
            return state.mods
        finally:
            lib.XCloseDisplay(connection)

    def input_plan(self, q):
        rows = self.keyboard_map()
        state = self.keyboard_state()
        for name, mask in (
            ("shift", self.X.ShiftMask),
            ("control", self.X.ControlMask),
            ("alt", self.X.Mod1Mask),
            ("meta", self.X.Mod4Mask),
        ):
            if self.symbol(name, True)[0] in self.keys:
                state &= ~mask
        # Locked modifiers and alternate XKB groups can change core keycode interpretation.
        if state & (
            self.X.LockMask
            | self.X.ControlMask
            | self.X.Mod1Mask
            | self.X.Mod5Mask
            | self.X.ShiftMask
        ):
            raise Error("INPUT_REFUSED", "Release modifiers and Caps Lock before SDK input")
        if q["action"] == "input_text":
            plan = [self.symbol(c) for c in q["text"]]
            modifiers = []
        else:
            plan = [self.symbol(q["key"], True)]
            modifiers = [self.symbol(m, True)[0] for m in q.get("modifiers", [])]
            if any(
                m.lower() not in ("shift", "ctrl", "control", "alt", "super", "meta", "win")
                for m in q.get("modifiers", [])
            ):
                raise Error("UNSUPPORTED_CAPABILITY", "Unsupported keyboard modifier")
        return rows, plan, modifiers

    def type_plan(self, prepared, control):
        rows, plan, modifiers = prepared
        if rows != self.keyboard_map():
            raise Error("INPUT_REFUSED", "Keyboard layout changed before input")
        for code in modifiers:
            control.check()
            control.started = True
            self.key(code, True)
        for code, shift in plan:
            control.check()
            control.started = True
            if code in self.keys:
                raise Error("INPUT_REFUSED", "Release the held key before pressing it")
            if shift and self.shift not in self.keys:
                self.key(self.shift, True)
                temporary = True
            else:
                temporary = False
            self.key(code, True)
            self.key(code, False)
            if temporary:
                self.key(self.shift, False)

    def action(self, q, control, focus=None):
        action = q["action"]
        if action == "release_all":
            control.check()
            control.started = True
            self.release()
            return completed("x11.xtest")
        if action in ("key_down", "key_up"):
            self.keyboard_map()
            code, shift = self.symbol(q["key"], True)
            if shift:
                raise Error("INVALID_ARGUMENT", "Hold Shift explicitly for shifted keys")
            control.check()
            control.started = True
            if action == "key_down" or code in self.keys:
                self.key(code, action == "key_down")
            return completed("x11.xtest")
        if action in ("button_down", "button_up"):
            button = {"primary": 1, "middle": 2, "secondary": 3}[q["button"]]
            point = self.point(q) if action == "button_down" else None
            control.check()
            control.started = True
            if point is not None:
                self.move(point)
            if action == "button_down" or button in self.buttons:
                self.button(button, action == "button_down")
            return completed("x11.xtest")
        previous_keys, previous_buttons = set(self.keys), set(self.buttons)
        try:
            if action in ("input_text", "press_key"):
                prepared = self.input_plan(q)  # Validate all text before any focus/input mutation.
                if focus:
                    focus()
                elif "image_token" in q:
                    if 1 in self.buttons:
                        raise Error(
                            "INPUT_REFUSED", "Release held button before focusing text input"
                        )
                    point = self.point(q)
                    control.check()
                    control.started = True
                    self.move(point)
                    self.button(1, True)
                    self.button(1, False)
                self.type_plan(prepared, control)
            elif action in ("point_click", "move", "drag", "wheel_scroll", "wheel"):
                start = self.point(q)
                end = self.point(q, True) if action == "drag" else None
                button = {"primary": 1, "middle": 2, "secondary": 3}[q.get("button", "primary")]
                if action in ("point_click", "drag") and button in self.buttons:
                    raise Error(
                        "INPUT_REFUSED", "Release the held button before clicking or dragging"
                    )
                if action == "wheel" and (
                    q.get("unit") != "steps"
                    or any(
                        type(q.get(k)) is not int or abs(q[k]) > 100 for k in ("delta_x", "delta_y")
                    )
                ):
                    raise Error("UNSUPPORTED_CAPABILITY", "X11 wheel requires integer steps")
                steps = q.get("steps", 1)
                if type(steps) is not int or not 1 <= steps <= 1000:
                    raise Error("INVALID_ARGUMENT", "steps must be 1..1000")
                control.check()
                control.started = True
                if action == "move":
                    pointer = self.root.query_pointer()
                    for i in range(1, steps + 1):
                        control.check()
                        self.move(
                            (
                                round(pointer.root_x + (start[0] - pointer.root_x) * i / steps),
                                round(pointer.root_y + (start[1] - pointer.root_y) * i / steps),
                            )
                        )
                else:
                    self.move(start)
                if action == "point_click":
                    for i in range(q.get("count", 1)):
                        control.check()
                        button = {"primary": 1, "middle": 2, "secondary": 3}[
                            q.get("button", "primary")
                        ]
                        self.button(button, True)
                        self.button(button, False)
                        if i + 1 < q.get("count", 1):
                            control.wait(0.08)
                elif action == "drag":
                    self.button(1, True)
                    control.wait((q.get("hold_ms") or 0) / 1000)
                    duration = q["duration_ms"] / 1000
                    begin = time.monotonic()
                    while True:
                        control.check()
                        progress = min(1, (time.monotonic() - begin) / duration)
                        self.move(tuple(round(a + (b - a) * progress) for a, b in zip(start, end)))
                        if progress == 1:
                            break
                        control.wait(min(0.01, duration))
                    self.button(1, False)
                elif action == "wheel_scroll":
                    button = {"up": 4, "down": 5, "left": 6, "right": 7}[q["direction"]]
                    for _ in range(q["amount"]):
                        control.check()
                        self.button(button, True)
                        self.button(button, False)
                elif action == "wheel":
                    for delta, negative, positive in ((q["delta_y"], 4, 5), (q["delta_x"], 6, 7)):
                        for _ in range(abs(delta)):
                            control.check()
                            wheel_button = positive if delta > 0 else negative
                            self.button(wheel_button, True)
                            self.button(wheel_button, False)
            else:
                raise Error("UNSUPPORTED_CAPABILITY")
            return completed("x11.xtest")
        finally:
            for code in list(self.keys - previous_keys):
                self.key(code, False)
            for button in list(self.buttons - previous_buttons):
                self.button(button, False)

    def close(self):
        try:
            self.release()
        finally:
            self.d.close()
            if hasattr(self, "guard"):
                self.guard.stdin.close()
                self.guard.wait(timeout=2)
                self.guard.stdout.close()


def input_guard():
    from Xlib import X, display
    from Xlib.ext import xtest

    d = display.Display(os.environ["DISPLAY"])
    held = set()
    print("ready", flush=True)
    try:
        for line in sys.stdin:
            kind, code, down = json.loads(line)
            if down:
                held.add((kind, code))
            else:
                held.discard((kind, code))
    finally:
        for kind, code in held:
            if kind == "process":
                try:
                    os.killpg(code, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                continue
            xtest.fake_input(d, X.KeyRelease if kind == "key" else X.ButtonRelease, code)
        d.sync()
        d.close()


if __name__ == "__main__" and "--input-guard" in sys.argv:
    input_guard()
