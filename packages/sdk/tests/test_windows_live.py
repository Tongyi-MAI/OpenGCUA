"""Opt-in real SDK/driver integration against one disposable native test window.

Set MAI_UI_RUN_WINDOWS_LIVE=1 and MAI_UI_WINDOWS_DRIVER to the pinned driver.
Run in a dedicated interactive test session without moving the physical pointer.
This is a deterministic SDK test, not a model-driven Office acceptance result.
"""

import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("MAI_UI_RUN_WINDOWS_LIVE") != "1",
    reason="requires opt-in isolated interactive Windows session and pinned CUA Driver",
)


def test_real_window_background_sdk_and_verified_results():
    from qwen_intelligence import Runtime
    from mai_ui import MuError

    fixture = subprocess.Popen(
        [sys.executable, "-u", str(Path(__file__).with_name("windows_fixture.py"))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    output = queue.Queue()
    threading.Thread(
        target=lambda: [output.put(line) for line in fixture.stdout], daemon=True
    ).start()
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]

    def current():
        point = wintypes.POINT()
        assert user.GetCursorPos(ctypes.byref(point))
        return int(user.GetForegroundWindow() or 0), point.x, point.y

    stop = threading.Event()
    changes = []
    try:
        ready = json.loads(output.get(timeout=10))
        original = current()

        def monitor():
            while not stop.wait(0.005):
                value = current()
                if value != original:
                    changes.append(value)

        sampler = threading.Thread(target=monitor, daemon=True)
        sampler.start()

        async def run():
            async with Runtime() as runtime:
                resource = await runtime.add_windows(name="fixture-desktop")
                async with runtime.session() as session:
                    device = await session.register(resource.id, name="windows")
                    inventory = await device.windows.list(timeout_ms=20000)
                    matches = [
                        window for window in inventory.windows if window.title == ready["title"]
                    ]
                    assert len(matches) == 1, "Controlled fixture must be discovered exactly once"
                    window = await device.windows.get(matches[0].id, timeout_ms=20000)
                    observation = await window.ui.ax.observe(timeout_ms=20000)
                    edits = [
                        node
                        for node in observation.tree.nodes
                        if node.role.lower() in {"edit", "text", "textfield", "text field"}
                        and "Initial fixture text" in str(node.name or node.text)
                    ]
                    assert len(edits) == 1, [
                        (node.role, node.name, node.text) for node in observation.tree.nodes
                    ]
                    await window.ui.ax.set_text(
                        edits[0],
                        "Verified Windows SDK text",
                        delivery="background",
                        timeout_ms=20000,
                    )
                    observation = await window.ui.ax.observe(timeout_ms=20000)
                    with pytest.raises(MuError) as stale:
                        await window.ui.ax.set_text(
                            edits[0], "Must never be applied", delivery="background"
                        )
                    assert stale.value.code == "STALE_TARGET"
                    buttons = [
                        node
                        for node in observation.tree.nodes
                        if node.name == "Increment fixture counter"
                    ]
                    assert len(buttons) == 1
                    await window.ui.ax.click(buttons[0], delivery="background", timeout_ms=20000)
                    fixture.stdin.write("state\n")
                    fixture.stdin.flush()
                    verified = json.loads(await asyncio.to_thread(output.get, True, 10))
                    assert verified == dict(text="Verified Windows SDK text", clicks=1)
                    observation = await window.ui.ax.observe(timeout_ms=20000)
                    button = next(
                        node
                        for node in observation.tree.nodes
                        if node.name == "Increment fixture counter"
                    )
                    # The image click exercises actual screen-to-window geometry;
                    # the desktop cursor must remain unchanged throughout.
                    vision = await window.ui.vision.observe(timeout_ms=20000)
                    assert len(vision.images) == 1
                    assert vision.images[0].width > 0 and vision.images[0].height > 0
                    assert await vision.images[0].read()
                    await window.ui.vision.click(
                        button, image=vision.images[0], delivery="background", timeout_ms=20000
                    )
                    fixture.stdin.write("state\n")
                    fixture.stdin.flush()
                    verified = json.loads(await asyncio.to_thread(output.get, True, 10))
                    assert verified == dict(text="Verified Windows SDK text", clicks=2)

        asyncio.run(run())
        assert current() == original
        assert not changes, f"Foreground/pointer changed during background operation: {changes[:5]}"
    finally:
        stop.set()
        if fixture.poll() is None:
            try:
                fixture.stdin.write("close\n")
                fixture.stdin.flush()
                fixture.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                fixture.kill()
                fixture.wait(timeout=5)
