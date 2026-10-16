"""Parent-side lifecycle shim for the SDK-backed macOS Agent.

The real mai-ui Runtime and macOS provider live in the persistent code worker.  The
parent keeps model orchestration, trajectories and task generations, but no longer
owns Cua Driver or translates native actions.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Observation:
    image_bytes: bytes
    mime_type: str
    width: int
    height: int
    driver_text: str = ""
    structured: dict[str, Any] = field(default_factory=dict)
    visual_ref: str | None = None
    target: dict[str, Any] = field(default_factory=dict)


class SdkDesktop:
    """Compatibility lifecycle object used by ``mac_agent.run_agent``.

    UI authority is intentionally not duplicated in the parent process: the worker
    creates one mai_ui.Runtime, registers its macOS endpoint and retains the Session
    across cells.  This object only coordinates task generations and shutdown.
    """

    def __init__(
        self,
        *,
        session_id: str,
        driver_mode: str = "sdk",
        driver_socket: str | None = None,
        call_timeout: float = 30.0,
        coordinate_range: float = 1000.0,
    ) -> None:
        self.session_id = session_id
        self.driver_mode = "sdk"
        self.driver_socket = driver_socket
        self.call_timeout = call_timeout
        self.coordinate_range = coordinate_range
        self.permission_status = {"provider": "mai-ui-sdk", "state": "worker_owned"}
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def close(self) -> None:
        """Compatibility alias used by the existing Agent cleanup path."""
        await self.shutdown()

    async def wait_for_idle(self) -> None:
        return None

    def invalidate_references(self) -> dict[str, int]:
        self._generation += 1
        return {"generation": self._generation}

    async def observe(self) -> Observation:
        """Capture one lifecycle desktop frame without opening a second SDK session.

        The persistent worker owns CuaDriver. The parent process only needs a single
        auditable startup/recovery frame, so it uses macOS ``screencapture`` and never
        performs UI actions or creates competing SDK endpoints.
        """
        command = shutil.which("screencapture") or "/usr/sbin/screencapture"
        fd, name = tempfile.mkstemp(prefix="mac_agent_desktop_", suffix=".png")
        os.close(fd)
        path = Path(name)
        try:
            process = await asyncio.create_subprocess_exec(
                command, "-x", "-t", "png", str(path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=max(10.0, self.call_timeout)
                )
            except asyncio.TimeoutError as exc:
                # wait_for cancels communicate(), not the child.  Reap it explicitly
                # or every failed lifecycle capture leaves a screencapture process
                # and pipe transports behind in this long-lived agent.
                process.kill()
                await process.communicate()
                raise RuntimeError(
                    "screencapture timed out; the launching app may lack macOS "
                    "Screen Recording permission"
                ) from exc
            if process.returncode != 0 or not path.is_file():
                raise RuntimeError(
                    f"screencapture failed rc={process.returncode}: "
                    f"{stderr.decode('utf-8', 'replace')[-500:]}"
                )
            image_bytes = path.read_bytes()
            from PIL import Image
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.load()
                width, height = image.size
            return Observation(
                image_bytes=image_bytes, mime_type="image/png", width=width, height=height,
                driver_text="", structured={"lifecycle_capture": True},
                visual_ref=f"desktop-lifecycle-{self._generation}",
                target={"kind": "desktop", "display": "main"},
            )
        finally:
            path.unlink(missing_ok=True)

    def visual_observation(self, _reference: str) -> Observation | None:
        return None


class SdkRpcServer:
    """No-op compatibility shell.

    SDK actions execute directly in the worker through mai_ui's Rust RuntimeCore.
    ``calls`` remains for trajectory schema compatibility; SDK receipt/event export
    can be added without reintroducing the old Cua RPC authority.
    """

    def __init__(self, desktop: SdkDesktop, socket_path: Path) -> None:
        self.desktop = desktop
        self.socket_path = socket_path
        self.calls: list[dict[str, Any]] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


# Preserve source names so the migrated Agent needs only one import change.
CuaDesktop = SdkDesktop
CuaRpcServer = SdkRpcServer
