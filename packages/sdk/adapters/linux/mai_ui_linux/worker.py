#!/usr/bin/env python3
"""Private JSON-lines provider. stdin cancellation is independent of the desktop queue."""

import base64
import importlib.metadata
import json
import os
import platform
import queue
import shutil
import signal
import sys
import threading
import time
import uuid

from common import Control, Error
from services import Clipboard, file_operation, io_error, run


class Worker:
    def __init__(self):
        self.output_lock = threading.Lock()
        self.controls_lock = threading.Lock()
        self.controls = {}
        self.requests = queue.Queue()
        self.desktop = None
        self.ax = None
        self.clipboard = Clipboard()
        self.stopping = threading.Event()

    def send(self, value):
        with self.output_lock:
            print(json.dumps(value, ensure_ascii=True, separators=(",", ":")), flush=True)

    def event(self, name, data):
        self.send(dict(event=name, data=data))

    def initialize(self):
        if self.desktop is None:
            if sys.platform != "linux":
                raise Error("UNSUPPORTED_CAPABILITY", "Run this adapter inside Ubuntu Linux")
            from x11 import Desktop

            self.desktop = Desktop(self.event)
            from ax import Client

            self.ax = Client(self.event, self.desktop.track)
            self.clipboard.track = self.desktop.track

    def describe(self, control):
        self.initialize()
        caps = [
            "ui.observe.screenshot",
            "ui.click.coordinates",
            "ui.move",
            "ui.drag",
            "ui.key_state",
            "ui.button_state",
            "ui.release_all",
            "ui.wheel",
            "ui.scroll.coordinates",
            "ui.input_text",
            "ui.press_key",
            "device.list_displays",
            "shell.run",
        ]
        caps += [
            "files." + op for op in ("read", "write", "stat", "list", "mkdir", "remove", "move")
        ]
        ax = self.ax.available(control)
        if ax:
            caps += [
                "ui.observe.tree",
                "ui.click.primary",
                "ui.set_text",
                "ui.select_text",
                "ui.perform",
            ]
        if shutil.which("xclip"):
            caps += ["clipboard.read", "clipboard.write"]
        return dict(
            protocol_major=1,
            platform="linux",
            provider="linux.x11",
            desktop_id=self.desktop.desktop_id,
            capabilities={k: dict(state="ready") for k in caps},
            accessibility={"state": "ready" if ax else "unavailable", "reason": self.ax.reason},
            shell_providers=[
                dict(
                    id="local",
                    state="ready",
                    default_interpreter="/bin/sh",
                    interpreters=["/bin/sh"],
                    kind="host_process",
                    default_cwd="/",
                    argv=True,
                    streaming=False,
                    stdin=True,
                    pty=False,
                    cancellation="process_group",
                    identity=dict(
                        platform="linux",
                        principal=os.environ.get("USER", ""),
                        uid=os.getuid(),
                        gid=os.getgid(),
                    ),
                )
            ],
        )

    def dispatch(self, q, control):
        op = q["op"]
        if op == "release_inputs":
            self.initialize()
            self.desktop.release()
            return dict(execution="completed")
        if op == "describe":
            return self.describe(control)
        self.initialize()
        if any(q.get(k) is not None for k in ("app_id", "window_id", "tab_id")):
            raise Error("UNSUPPORTED_CAPABILITY", "Linux supports desktop/display scopes only")
        if op == "observe":
            w, h = self.desktop.refresh_geometry()
            data = dict(
                width=w,
                height=h,
                nodes=[],
                images=[],
                components=[],
                source="linux.x11",
                captured_at=time.time(),
                revision=uuid.uuid4().hex,
                truncated=False,
                age_ms=0,
                pending_invalidations=0,
                identity_mode="snapshot",
                identity_epoch=None,
            )
            if q.get("tree"):
                data.update(self.ax.request({"op": "observe"}, control))
            if q.get("screenshot"):
                data["images"] = [self.desktop.observe(control)]
            return data
        if op == "displays":
            w, h = self.desktop.refresh_geometry()
            return dict(
                displays=[
                    dict(
                        id="display:0",
                        generation=str(self.desktop.geometry_version),
                        width=w,
                        height=h,
                        rotation=0,
                        scale_factor=1,
                        insets=None,
                    )
                ]
            )
        if op == "file":
            return file_operation(q, control)
        if op == "shell":
            return run(
                q.get("argv") or ["/bin/sh", "-c", q["script"]],
                control,
                data=base64.b64decode(q.get("stdin") or ""),
                cwd=q.get("cwd"),
                env=q.get("env"),
                cap=q.get("max_output_bytes", 1048576),
                track=self.desktop.track,
            )
        if op == "clipboard":
            return self.clipboard.read(control)
        if op == "action":
            if q.get("delivery") == "background":
                raise Error("UNSUPPORTED_CAPABILITY", "Linux input is foreground only")
            if q["action"] == "clipboard_write":
                return self.clipboard.write(q["text"], control)
            if q["action"] in ("click", "set_text", "select_text", "perform"):
                return self.ax.request(q, control)
            focus = (
                (lambda: self.ax.request({**q, "action": "focus"}, control))
                if q.get("node_id") and q["action"] == "input_text"
                else None
            )
            return self.desktop.action(q, control, focus)
        raise Error("UNSUPPORTED_CAPABILITY")

    def execute(self):
        while True:
            item = self.requests.get()
            if item is None:
                break
            q, control, key = item
            try:
                control.check()
                result = self.dispatch(q, control)
                self.send({"_request_id": q.get("_request_id"), "result": result})
            except Exception as exc:
                if isinstance(exc, OSError):
                    exc = io_error(exc)
                    if (
                        control.started
                        and q.get("op") == "file"
                        and exc.code not in ("TARGET_NOT_FOUND", "ALREADY_EXISTS")
                    ):
                        exc = Error("ACTION_OUTCOME_UNKNOWN", str(exc))
                code = exc.code if isinstance(exc, Error) else "PROVIDER_ERROR"
                self.send(
                    {
                        "_request_id": q.get("_request_id"),
                        "error": {"code": code, "message": str(exc)},
                    }
                )
            finally:
                with self.controls_lock:
                    self.controls.pop(key, None)
        self.cleanup()

    def cleanup(self):
        if self.ax:
            self.ax.close()
        self.clipboard.close()
        if self.desktop:
            self.desktop.close()

    def stop(self, *args):
        if self.stopping.is_set():
            return
        self.stopping.set()
        with self.controls_lock:
            for control in self.controls.values():
                control.cancelled.set()
        self.requests.put(None)

    def main(self):
        executor = threading.Thread(target=self.execute)
        executor.start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.stop)
        try:
            for line in sys.stdin:
                if self.stopping.is_set():
                    break
                q = json.loads(line)
                if q["op"] == "cancel":
                    with self.controls_lock:
                        control = self.controls.get(q.get("operation_id"))
                        if control:
                            control.cancelled.set()
                    self.send(
                        {
                            "_request_id": q.get("_request_id"),
                            "result": {"cancelled": control is not None},
                        }
                    )
                    continue
                control = Control(q.get("timeout_ms", 5000))
                key = (
                    q.get("_operation_id")
                    or q.get("action_id")
                    or q.get("command_id")
                    or str(q.get("_request_id"))
                )
                with self.controls_lock:
                    self.controls[key] = control
                self.requests.put((q, control, key))
        finally:
            self.stop()
            executor.join(timeout=4)
            if executor.is_alive():
                os._exit(1)


def doctor():
    checks = {}
    worker = Worker()
    worker.event = lambda *args: None
    try:
        desc = worker.describe(Control(10000))
        worker.desktop.observe(Control(3000))
        checks = dict(
            x11=True,
            xtest=True,
            xfixes=True,
            xcb_capture=True,
            keyboard=True,
            monitor=True,
            accessibility=desc["accessibility"],
            clipboard=shutil.which("xclip") is not None,
        )
        state = (
            "ready"
            if desc["accessibility"]["state"] == "ready" and checks["clipboard"]
            else "needs_setup"
        )
    except Exception as exc:
        state = "needs_setup"
        checks["error"] = {"code": getattr(exc, "code", "SETUP_FAILED"), "message": str(exc)}
    finally:
        worker.cleanup()
    versions = {}
    for name in ("python-xlib", "Pillow", "six"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    print(
        json.dumps(
            dict(
                state=state,
                checks=checks,
                versions=versions,
                python=sys.executable,
                os_release=platform.freedesktop_os_release() if sys.platform == "linux" else {},
                session_type=os.environ.get("XDG_SESSION_TYPE"),
                display=os.environ.get("DISPLAY"),
                system_packages="python3-venv python3-pyatspi python3-gi gir1.2-atspi-2.0 libxcb1 xclip",
            )
        )
    )


if __name__ == "__main__":
    if "--doctor" in sys.argv:
        doctor()
    else:
        Worker().main()
