"""Adapter boundary: normalized messages, independent of Python placement and OS.

The built-in transport starts an owned Rust broker. Host integrations can register
another async Adapter factory (desktop, on-device IPC, etc.) without changing UI code.
This prototype is a trusted-program API, not a sandbox for untrusted Python.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable, Protocol

from . import _native


class Adapter(Protocol):
    platform: str
    provider: str

    async def start(self) -> None: ...
    async def request(self, message: dict) -> dict: ...
    async def close(self) -> None: ...


_registry: dict[str, Callable[[str], Adapter]] = {}


def register_adapter(name: str, factory: Callable[[str], Adapter]) -> None:
    """Trusted embedding host registration, before connect(adapter_ref=...)."""
    if not name or name == "adb" or name in _registry:
        raise ValueError("adapter name is empty, reserved, or already registered")
    _registry[name] = factory


class AdbAdapter:
    platform = "android"
    provider = "android.uiautomation"

    def __init__(self, serial: str, *, observe_on_connect=True):
        self.serial = serial
        self.observe_on_connect = observe_on_connect
        self.process = None

    async def start(self):
        root = Path(__file__).resolve().parents[2]
        binary = (
            os.environ.get("MAI_UI_BROKER")
            or shutil.which("mu-broker")
            or str(root / "target/debug/mu-broker")
        )
        jar = os.environ.get("MAI_UI_AGENT_JAR") or str(root / "adapters/android/out/agent.jar")
        if not Path(binary).is_file() or not Path(jar).is_file():
            raise RuntimeError(
                "Build the native broker and agent first; see README. For wheels set MAI_UI_BROKER and MAI_UI_AGENT_JAR."
            )
        self.process = await _native.connect_process(
            binary,
            [self.serial, jar],
            dict(os.environ, MAI_UI_DEFER_OBSERVATION="0" if self.observe_on_connect else "1"),
        )

    async def request(self, message):
        if self.process is None:
            raise ConnectionError("adapter has not started")
        return json.loads(await self.process.request(json.dumps(message, ensure_ascii=False)))

    async def close(self):
        if self.process is not None:
            await self.process.close()


class MacosAdapter:
    """Thin lifecycle wrapper; native acquisition and translation live in Rust."""

    platform, provider = "macos", "macos.cua"

    def __init__(self, *, app_path=None, socket=None):
        self.client = None
        self.app_path, self.socket = app_path, socket

    async def start(self):
        try:
            self.client = await _native.connect_macos(self.app_path, self.socket)
        except ValueError as error:
            from ._errors import MuError

            code, _, message = str(error).partition(": ")
            raise MuError(code, message) from error

    async def request(self, message):
        if self.client is None:
            raise ConnectionError("adapter has not started")
        return json.loads(await self.client.request(json.dumps(message, ensure_ascii=False)))

    async def close(self):
        if self.client is not None:
            await self.client.close()
