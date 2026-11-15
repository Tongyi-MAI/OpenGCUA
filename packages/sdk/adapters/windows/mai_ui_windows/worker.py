#!/usr/bin/env python3
"""Private SDK JSON-lines worker. The reader can cancel a blocked driver call."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading

from native import Job, Native, contain_worker
from provider import Provider
from transport import MAX_FRAME, MCP, Control, Error


class Worker:
    def __init__(self, driver):
        self.driver = driver
        self.provider = None
        self.requests = queue.Queue(maxsize=128)
        self.controls = {}
        self.lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.stopping = threading.Event()

    def send(self, value):
        with self.output_lock:
            sys.stdout.write(
                json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
            )
            sys.stdout.flush()

    def initialize(self):
        if self.provider is None:
            native = Native()
            native.acquire()
            try:
                contain_worker()
                transport = MCP(self.driver, job_factory=Job)
                self.provider = Provider(transport, native)
            except BaseException:
                if "transport" in locals():
                    transport.close()
                native.close()
                raise

    def execute(self):
        while True:
            item = self.requests.get()
            if item is None:
                return
            request, control, key = item
            try:
                control.check()
                self.initialize()
                value = {"result": self.provider.dispatch(request, control)}
            except Error as exc:
                value = {"error": exc.wire()}
            except Exception as exc:
                value = {
                    "error": {
                        "code": "ACTION_OUTCOME_UNKNOWN" if control.started else "PROVIDER_ERROR",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                }
            try:
                self.send({"_request_id": request.get("_request_id"), **value})
            except (OSError, ValueError):
                self.stopping.set()
                return
            finally:
                with self.lock:
                    self.controls.pop(key, None)

    def main(self):
        executor = threading.Thread(target=self.execute, daemon=True)
        executor.start()
        try:
            while not self.stopping.is_set():
                line = sys.stdin.buffer.readline(MAX_FRAME + 1)
                if not line:
                    break
                if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                    raise Error("PROTOCOL_MISMATCH", "Oversized or incomplete SDK request")
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise Error("PROTOCOL_MISMATCH", "SDK request must be an object")
                key = (
                    request.get("_operation_id")
                    or request.get("action_id")
                    or request.get("_request_id")
                )
                if request.get("op") == "cancel":
                    with self.lock:
                        control = self.controls.get(request.get("operation_id"))
                        if control:
                            control.cancelled.set()
                    self.send(
                        {
                            "_request_id": request.get("_request_id"),
                            "result": {"cancelled": control is not None},
                        }
                    )
                    continue
                control = None
                inserted = False
                try:
                    control = Control(request.get("timeout_ms", 10000))
                    with self.lock:
                        if key in self.controls:
                            raise Error(
                                "INVALID_ARGUMENT", "Duplicate in-flight operation identifier"
                            )
                        self.controls[key] = control
                        inserted = True
                    self.requests.put_nowait((request, control, key))
                except (Error, queue.Full) as exc:
                    with self.lock:
                        if inserted and self.controls.get(key) is control:
                            self.controls.pop(key, None)
                    error = (
                        exc
                        if isinstance(exc, Error)
                        else Error("RESOURCE_BUSY", "Windows provider request queue is full")
                    )
                    self.send({"_request_id": request.get("_request_id"), "error": error.wire()})
        finally:
            self.stopping.set()
            with self.lock:
                for control in self.controls.values():
                    control.cancelled.set()
            # All queued controls are cancelled. The worker will reject them
            # without dispatching mutations, then release its owned child.
            try:
                self.requests.put_nowait(None)
            except queue.Full:
                pass
            executor.join(timeout=4)
            if self.provider:
                self.provider.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--driver", required=True)
    parser.add_argument("--doctor", action="store_true")
    args = parser.parse_args()
    worker = Worker(args.driver)
    if args.doctor:
        try:
            worker.initialize()
            worker.send({"state": "ready", **worker.provider.describe()})
        except Error as exc:
            worker.send({"state": "unavailable", "error": exc.wire()})
        finally:
            if worker.provider:
                worker.provider.close()
    else:
        worker.main()


if __name__ == "__main__":
    main()
