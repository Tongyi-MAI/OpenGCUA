"""Explicit, checksum-pinned installation of the optional Windows CUA driver."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import zipfile

DRIVER_VERSION = "0.38.0"
ARCHIVE_NAME = f"cua-driver-rs-{DRIVER_VERSION}-windows-x86_64-binary.zip"
ARCHIVE_URL = f"https://github.com/trycua/cua/releases/download/cua-driver-rs-v{DRIVER_VERSION}/{ARCHIVE_NAME}"
ARCHIVE_SHA256 = "5edaa74d0afa1b55a9668dbc4f32736507bd2bd4d75b18ca1dc8881bb12402b5"
MEMBERS = frozenset(
    {
        "cua-driver.exe",
        "cua-driver-uia.exe",
        "cua-cursor-theme.exe",
        "cua_driver_sdk.dll",
        "cua_driver_node_runtime.node",
        "cua_driver_abi.h",
    }
)
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024


def installation_root() -> Path:
    explicit = os.environ.get("MAI_UI_WINDOWS_HOME")
    if explicit:
        root = Path(explicit).expanduser()
        if not root.is_absolute():
            raise ValueError("MAI_UI_WINDOWS_HOME must be an absolute directory")
        return root
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise ValueError("LOCALAPPDATA is unavailable; set MAI_UI_WINDOWS_HOME")
    return Path(base) / "mai-ui" / "windows" / DRIVER_VERSION


def resolve_driver(driver_path: str | None = None) -> Path:
    value = driver_path if driver_path is not None else os.environ.get("MAI_UI_WINDOWS_DRIVER")
    if value is not None and (not isinstance(value, str) or not value or "\0" in value):
        raise ValueError("driver_path must be an absolute executable path")
    path = Path(value).expanduser() if value is not None else installation_root() / "cua-driver.exe"
    if not path.is_absolute():
        raise ValueError("driver_path must be absolute")
    if not path.is_file():
        raise FileNotFoundError(f"Windows driver not found at {path}; run mu setup windows")
    return path.resolve()


def doctor(driver_path: str | None = None) -> dict:
    """Check executable compatibility without observing or interacting with the desktop."""
    if sys.platform != "win32":
        return dict(state="unsupported", error="The Windows driver requires Windows")
    try:
        driver = resolve_driver(driver_path)
        result = subprocess.run(
            [str(driver), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        expected = f"cua-driver {DRIVER_VERSION}"
        if result.returncode or result.stdout.strip() != expected:
            return dict(
                state="version_mismatch",
                expected=expected,
                actual=result.stdout.strip()[:200],
                driver_path=str(driver),
            )
        return dict(
            state="ready",
            driver_path=str(driver),
            driver_version=DRIVER_VERSION,
            scope="executable only; desktop capabilities are checked on connection",
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return dict(state="needs_setup", error=str(error))


def _download(destination: Path) -> None:
    request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "OpenGCUA-Windows-Setup"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError("Driver archive exceeds the download limit")
            output.write(chunk)


def _extract_verified(archive: Path, destination: Path) -> dict[str, str]:
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Driver archive exceeds the size limit")
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != ARCHIVE_SHA256:
        raise ValueError("Driver archive SHA256 mismatch; no installation files changed")
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) != len(MEMBERS) or {item.filename for item in entries} != MEMBERS:
            raise ValueError("Unexpected files in the pinned driver archive")
        if sum(item.file_size for item in entries) > MAX_EXTRACTED_BYTES:
            raise ValueError("Expanded driver archive exceeds the size limit")
        hashes = {}
        for entry in entries:
            target = destination / entry.filename
            with source.open(entry) as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
            with target.open("rb") as reader:
                hashes[entry.filename] = hashlib.file_digest(reader, "sha256").hexdigest()
        return hashes


def setup(*, archive: str | None = None, replace: bool = False) -> dict:
    """Install only on an explicit setup command; normal SDK imports never download."""
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise ValueError("This preview provides the native Windows x86-64 driver only")
    root = installation_root()
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise ValueError("The driver installation directory must not be a link or junction")
    if root.exists() and not root.is_dir():
        raise ValueError("The driver installation path is not a directory")
    if root.exists() and any(root.iterdir()) and not replace:
        result = doctor(str(root / "cua-driver.exe"))
        if result["state"] == "ready":
            return result
        raise ValueError("An existing installation was preserved; inspect it or pass --replace")
    root.parent.mkdir(parents=True, exist_ok=True)
    lock = root.parent / f".{root.name}.install.lock"
    try:
        lease = lock.open("xb")
    except FileExistsError as error:
        raise ValueError(
            f"Another installation is active; if it was interrupted, inspect {lock}"
        ) from error
    try:
        with (
            lease,
            tempfile.TemporaryDirectory(
                prefix=".windows-driver-",
                dir=root.parent,
                ignore_cleanup_errors=True,
            ) as temporary,
        ):
            staging = Path(temporary)
            source = Path(archive).expanduser().resolve() if archive else staging / ARCHIVE_NAME
            if archive is None:
                _download(source)
            extracted = staging / "files"
            extracted.mkdir()
            hashes = _extract_verified(source, extracted)
            manifest = dict(version=DRIVER_VERSION, archive_sha256=ARCHIVE_SHA256, files=hashes)
            (extracted / "manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
            previous = root.with_name(f".{root.name}.previous-{uuid.uuid4().hex}")
            _publish(extracted, root, previous)
            # Only our own verified backup is eligible for removal. Locked old
            # binaries may remain here until their owning process has exited.
            if (
                previous.exists()
                and previous.parent.resolve() == root.parent.resolve()
                and previous.name.startswith(f".{root.name}.previous-")
                and not previous.is_symlink()
            ):
                try:
                    shutil.rmtree(previous)
                except OSError:
                    pass
    finally:
        lock.unlink(missing_ok=True)
    return doctor(str(root / "cua-driver.exe"))


def _publish(extracted: Path, root: Path, previous: Path) -> None:
    """Publish a complete directory, retaining the old installation on failure."""
    had_previous = root.exists()
    if had_previous:
        if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
            raise ValueError("Refusing to replace a linked installation")
        if any(
            item.name not in MEMBERS | {"manifest.json"} or not item.is_file() or item.is_symlink()
            for item in root.iterdir()
        ):
            raise ValueError(
                "Installation directory contains unrelated files; preserved without changes"
            )
        os.replace(root, previous)
    try:
        os.replace(extracted, root)
    except BaseException:
        if had_previous:
            try:
                os.replace(previous, root)
            except OSError as error:
                # The caller supplies a sibling outside its temporary staging
                # directory, so failed recovery cannot delete the old install.
                raise OSError(
                    f"Installation interrupted; previous files preserved at {previous}"
                ) from error
        raise
