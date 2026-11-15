"""Owned, correlated MCP transport; a lost reply never causes action replay."""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

MAX_FRAME = 64 * 1024 * 1024
DRIVER_VERSION = "0.38.0"


class Error(Exception):
    def __init__(self, code, message=None, details=None):
        self.code = code
        self.details = details
        super().__init__(message or code)

    def wire(self):
        result = {"code": self.code, "message": str(self)}
        if self.details is not None:
            result["details"] = self.details
        return result


class Control:
    def __init__(self, timeout_ms=10000):
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 120000:
            raise Error("INVALID_ARGUMENT", "timeout_ms must be in 1..120000")
        self.deadline = time.monotonic() + timeout_ms / 1000
        self.cancelled = threading.Event()
        self.started = False

    def check(self):
        if self.cancelled.is_set():
            raise Error("ACTION_OUTCOME_UNKNOWN" if self.started else "CANCELLED_BEFORE_DISPATCH")
        if time.monotonic() >= self.deadline:
            raise Error("ACTION_OUTCOME_UNKNOWN" if self.started else "DEADLINE_EXCEEDED")


class MCP:
    """One direct driver per provider, with bounded output and independent readers.

    The driver is fenced after cancellation/timeout of an in-flight request: its
    thread may still be mutating the application, so no later request is sent.
    """

    def __init__(self, driver, *, command=None, job_factory=None):
        path = Path(driver)
        if command is None and (not path.is_absolute() or not path.is_file()):
            raise Error(
                "SETUP_REQUIRED", "driver_path must name an existing absolute CUA executable"
            )
        self.lock = threading.Lock()
        self.writes = queue.Queue(maxsize=128)
        self.writing = threading.Event()
        self.pending = {}
        self.sequence = 0
        self.failed = None
        self.closed = False
        self.stderr = collections.deque(maxlen=20)
        self.job = None
        try:
            self.process = subprocess.Popen(
                command or [str(path), "mcp", "--direct"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if job_factory is not None:
                self.job = job_factory(self.process)
        except OSError as exc:
            if hasattr(self, "process"):
                self.process.kill()
                self.process.wait()
            raise Error("SETUP_REQUIRED", f"Cannot start the owned Windows driver: {exc}") from exc
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.drainer = threading.Thread(target=self._stderr, daemon=True)
        self.writer = threading.Thread(target=self._write, daemon=True)
        self.reader.start()
        self.drainer.start()
        self.writer.start()
        try:
            initialized = self.request(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "opengcua-windows", "version": "0.3.1"},
                },
                Control(),
            )
            self.server = initialized.get("serverInfo", {})
            self.notify("notifications/initialized", {})
            catalog = self.request("tools/list", {}, Control())
            self.tools = {item["name"]: item for item in catalog.get("tools", [])}
            if (
                not {"list_apps", "list_windows", "get_window_state", "click", "set_value"}
                <= self.tools.keys()
            ):
                raise Error("PROTOCOL_MISMATCH", "Driver lacks required Windows MCP tools")
        except BaseException:
            self.close()
            raise

    def _stderr(self):
        while True:
            chunk = self.process.stderr.readline(4096)
            if not chunk:
                return
            self.stderr.append(chunk.decode("utf-8", "replace").rstrip()[-400:])

    def _fail(self, message):
        with self.lock:
            if self.failed is None:
                self.failed = message
            pending = list(self.pending.values())
            self.pending.clear()
        for receiver in pending:
            receiver.put(Error("DEVICE_OFFLINE", message))

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_FRAME + 1)
                if not line:
                    raise Error("DEVICE_OFFLINE", "The Windows driver closed its output")
                if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                    raise Error("PROTOCOL_MISMATCH", "Oversized or incomplete driver frame")
                value = json.loads(line)
                if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                    raise Error("PROTOCOL_MISMATCH", "Invalid MCP envelope")
                if "id" not in value:
                    # Notifications are not operation replies.
                    continue
                with self.lock:
                    receiver = self.pending.pop(value["id"], None)
                if receiver is None:
                    raise Error("PROTOCOL_MISMATCH", "Unexpected MCP response identifier")
                receiver.put(value)
        except (OSError, ValueError, TypeError, Error) as exc:
            self._fail(str(exc))

    def _write(self):
        while True:
            item = self.writes.get()
            if item is None:
                return
            data, reply = item
            try:
                # Serialize admission with close: after close sees no writer,
                # no thread may acquire BufferedWriter's lock and block it.
                with self.lock:
                    if self.closed or self.failed:
                        raise Error("DEVICE_OFFLINE", self.failed or "Windows driver is closed")
                    self.writing.set()
                self.process.stdin.write(data)
                self.process.stdin.flush()
                reply.put(None)
            except (OSError, ValueError, Error) as exc:
                error = exc if isinstance(exc, Error) else Error("DEVICE_OFFLINE", str(exc))
                reply.put(error)
                self._fail(str(error))
            finally:
                self.writing.clear()

    def _send(self, value, control=None):
        control = control or Control()
        data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode() + b"\n"
        if len(data) > MAX_FRAME:
            raise Error("INVALID_ARGUMENT", "Driver request exceeds the frame budget")
        try:
            reply = queue.Queue(maxsize=1)
            self.writes.put_nowait((data, reply))
        except queue.Full as exc:
            raise Error("RESOURCE_BUSY", "Windows driver write queue is full") from exc
        while True:
            control.check()
            try:
                error = reply.get(
                    timeout=min(0.05, max(0.001, control.deadline - time.monotonic()))
                )
                if error:
                    raise error
                return
            except queue.Empty:
                continue

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params, control, *, mutation=False):
        control.check()
        receiver = queue.Queue(maxsize=1)
        with self.lock:
            if self.closed or self.failed:
                raise Error("DEVICE_OFFLINE", self.failed or "Windows driver is closed")
            self.sequence += 1
            request_id = self.sequence
            self.pending[request_id] = receiver
        sent = False
        try:
            control.check()
            # Mark uncertainty before writing: a pipe failure may occur after a
            # complete request reached the driver.
            if mutation:
                control.started = True
            sent = True
            self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, control
            )
            while True:
                control.check()
                try:
                    value = receiver.get(
                        timeout=min(0.05, max(0.001, control.deadline - time.monotonic()))
                    )
                    break
                except queue.Empty:
                    continue
            if isinstance(value, Error):
                raise value
            if ("result" in value) == ("error" in value):
                raise Error("PROTOCOL_MISMATCH", "MCP response needs exactly one result or error")
            if "error" in value:
                error = value["error"]
                if not isinstance(error, dict):
                    raise Error("PROTOCOL_MISMATCH", "MCP error must be an object")
                raise Error(
                    "ACTION_OUTCOME_UNKNOWN" if mutation else "PROVIDER_ERROR",
                    str(error.get("message", "MCP request failed")),
                    error,
                )
            if not isinstance(value["result"], dict):
                raise Error("PROTOCOL_MISMATCH", "MCP result must be an object")
            return value["result"]
        except Error as exc:
            if sent and exc.code in {
                "ACTION_OUTCOME_UNKNOWN",
                "CANCELLED_BEFORE_DISPATCH",
                "DEADLINE_EXCEEDED",
                "DEVICE_OFFLINE",
                "PROTOCOL_MISMATCH",
            }:
                self._fail(str(exc))
                self.close()
                if mutation:
                    raise Error(
                        "ACTION_OUTCOME_UNKNOWN",
                        "Driver reply was lost after dispatch; do not replay the action",
                        {"cause": exc.code},
                    ) from exc
            raise
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def tool(self, name, arguments, control, *, mutation=False):
        if name not in self.tools:
            raise Error("UNSUPPORTED_CAPABILITY", f"Driver does not expose {name}")
        return self.request(
            "tools/call", {"name": name, "arguments": arguments}, control, mutation=mutation
        )

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            force = bool(self.failed or self.writing.is_set())
        # A blocked BufferedWriter owns an internal lock. Kill first on failure
        # so closing stdin cannot wait forever for that writer to drain.
        if force:
            # Descendants can inherit the read end of stdin. Close the whole
            # owned Job before waiting for a blocked writer to release it.
            if self.job is not None:
                self.job.close()
            if self.process.poll() is None:
                self.process.kill()
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        if self.job is not None:
            self.job.close()
        try:
            self.writes.put_nowait(None)
        except queue.Full:
            pass
        self._fail("Windows driver closed")
