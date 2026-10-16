"""Owned, loopback-only web ADB bridge used by the recording studio."""
from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
from pathlib import Path

MOBILE_ROOT = Path(__file__).resolve().parent / "mobile"
SERVER_VERSION = "3.3.3"


class MobileBridge:
    def __init__(self, origin: str) -> None:
        self.proc = None
        self.ws_url = None
        node, adb = shutil.which("node"), shutil.which("adb")
        if not node or not adb:
            raise RuntimeError("Mobile streaming requires Node.js and adb on PATH")
        for asset in ("dist/mobile.js", f"vendor/scrcpy-server-v{SERVER_VERSION}", "node_modules/ws/package.json"):
            if not (MOBILE_ROOT / asset).is_file():
                raise RuntimeError("Prepare mobile streaming: cd packages/harness/runtime/mobile && npm ci && npm run setup && npm run build")
        # The SDK's adb subprocesses and the web bridge use this same local server.
        if os.environ.get("ADB_SERVER_SOCKET"):
            raise RuntimeError("Mobile studio uses local ADB; unset ADB_SERVER_SOCKET and use ANDROID_ADB_SERVER_PORT if needed")
        subprocess.run([adb, "start-server"], check=True, timeout=15, capture_output=True)
        self.proc = subprocess.Popen(
            [node, str(MOBILE_ROOT / "bridge.mjs")], stdout=subprocess.PIPE,
            env={**os.environ, "DEMO_HUB_ORIGIN": origin}, text=True,
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.proc.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=10):
                    raise RuntimeError("Local web ADB bridge did not start")
                self.ws_url = json.loads(self.proc.stdout.readline())["ws_url"]
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            self.proc.stdout.close()
