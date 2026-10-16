"""Wire errors and interruptible platform execution, independent of either binding."""

import base64
import threading
import time


class Error(Exception):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def b64(data):
    return base64.b64encode(data).decode("ascii")


class Control:
    def __init__(self, timeout_ms):
        self.deadline = time.monotonic() + timeout_ms / 1000
        self.cancelled = threading.Event()
        self.started = False

    def check(self):
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise Error("ACTION_OUTCOME_UNKNOWN" if self.started else "CANCELLED_BEFORE_DISPATCH")

    def remaining(self):
        self.check()
        return max(0.001, self.deadline - time.monotonic())

    def wait(self, seconds):
        self.cancelled.wait(min(seconds, self.remaining()))
        self.check()


def completed(mechanism):
    return dict(execution="completed", effect="unknown", delivery="foreground", mechanism=mechanism)
