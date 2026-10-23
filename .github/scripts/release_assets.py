"""Validate a release tag and its complete download set before publication."""
import argparse
from email.parser import Parser
import hashlib
from pathlib import Path
import re
import tarfile
import tomllib
import zipfile

PACKAGE = "qwen-intelligence-sdk"
WHEEL_PREFIX = "qwen_intelligence_sdk"
DMG = "OpenGCUA-macOS-arm64.dmg"
PYTHONS = {"cp311", "cp312", "cp313", "cp314"}
PLATFORMS = {"macos-arm64", "linux-x86_64"}


def release_metadata(tag: str, version: str, sha: str) -> dict[str, str]:
    match = re.fullmatch(r"(\d+\.\d+\.\d+)((?:a|b|rc)\d+)?", version)
    if not match or tag != f"v{version}":
        raise ValueError("Tag must be v<SDK version>, for example v0.1.0 or v0.2.0rc1")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Release source must resolve to an exact commit SHA")
    return {"tag": tag, "version": version, "app_version": match[1],
            "prerelease": str(bool(match[2])).lower(), "sha": sha}


def check_package_metadata(text: str, version: str) -> None:
    metadata = Parser().parsestr(text)
    name = re.sub(r"[-_.]+", "-", metadata.get("Name", "")).lower()
    if name != PACKAGE or metadata.get("Version") != version:
        raise ValueError("Artifact package name/version does not match the release source")


def validate_assets(directory: Path, version: str) -> list[Path]:
    expected_pairs = {(python, platform) for python in PYTHONS for platform in PLATFORMS}
    found = set()
    wheels = sorted(directory.glob("*.whl"))
    if len(wheels) != len(expected_pairs):
        raise ValueError("Expected eight SDK wheels: CPython 3.11-3.14 on macOS arm64 and Linux x86_64")
    for wheel in wheels:
        parts = wheel.stem.split("-")
        if len(parts) != 5 or parts[0] != WHEEL_PREFIX or parts[1] != version:
            raise ValueError(f"Unexpected wheel name/version: {wheel.name}")
        python, abi, platforms = parts[2:]
        if python not in PYTHONS or abi != python:
            raise ValueError(f"Unexpected Python/ABI tag: {wheel.name}")
        tags = platforms.split(".")
        if all(tag.startswith("macosx_") and tag.endswith("_arm64") for tag in tags):
            platform = "macos-arm64"
        elif all(tag.startswith("manylinux") and tag.endswith("_x86_64") for tag in tags):
            platform = "linux-x86_64"
        else:
            raise ValueError(f"Unexpected platform: {wheel.name}")
        pair = (python, platform)
        if pair in found:
            raise ValueError(f"Duplicate wheel target: {pair}")
        found.add(pair)
        with zipfile.ZipFile(wheel) as archive:
            if archive.testzip() is not None:
                raise ValueError(f"Corrupt wheel: {wheel.name}")
            metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata) != 1:
                raise ValueError(f"Missing or ambiguous wheel metadata: {wheel.name}")
            check_package_metadata(archive.read(metadata[0]).decode("utf-8"), version)
    if found != expected_pairs:
        raise ValueError("SDK wheel matrix is incomplete")

    sdist = directory / f"{WHEEL_PREFIX}-{version}.tar.gz"
    with tarfile.open(sdist, "r:gz") as archive:
        metadata = [member for member in archive.getmembers()
                    if member.name.endswith("/PKG-INFO") and member.name.count("/") == 1 and member.isfile()]
        if len(metadata) != 1:
            raise ValueError("Missing or ambiguous source distribution metadata")
        with archive.extractfile(metadata[0]) as file:
            check_package_metadata(file.read().decode("utf-8"), version)

    dmg = directory / DMG
    if not dmg.is_file() or dmg.stat().st_size == 0:
        raise ValueError("DMG is missing or empty")
    checksum = directory / f"{DMG}.sha256"
    tokens = checksum.read_text().split()
    digest = hashlib.sha256(dmg.read_bytes()).hexdigest()
    if tokens != [digest, DMG]:
        raise ValueError("DMG checksum is invalid or does not use the portable download filename")
    assets = [*wheels, sdist, dmg, checksum]
    allowed = {path.name for path in assets} | {"SHA256SUMS"}
    if any(path.name not in allowed or not path.is_file() or path.is_symlink() for path in directory.iterdir()):
        raise ValueError("Unexpected files in release artifacts")
    return sorted(assets)


def write_downloads(directory: Path, metadata: dict[str, str], notes: Path) -> None:
    assets = validate_assets(directory, metadata["version"])
    checksums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in assets)
    (directory / "SHA256SUMS").write_text(checksums)
    notes.write_text(f"""OpenGCUA {metadata['tag']}

Built from commit `{metadata['sha']}`. All build jobs and installed-wheel smoke checks passed.

- **macOS app:** download `{DMG}`, open it, and drag OpenGCUA into Applications. Apple Silicon, macOS 14+.
- **Python SDK:** choose the wheel matching CPython 3.11, 3.12, 3.13 or 3.14 and your platform: macOS arm64 or Linux x86_64. Install with `python -m pip install /path/to/downloaded.whl`.
- **SDK source:** `{WHEEL_PREFIX}-{metadata['version']}.tar.gz` (building from source requires Rust and a C toolchain).
- **Checksums:** `SHA256SUMS` covers every download above, including the DMG checksum file.

The DMG is a development build: ad-hoc signed and not notarized by Apple.
SDK package/import names remain `qwen-intelligence-sdk` / `qwen_intelligence`.
""")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["metadata", "assets"])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--project", type=Path, default=Path("packages/sdk/pyproject.toml"))
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--directory", type=Path, default=Path("release-assets"))
    parser.add_argument("--notes", type=Path, default=Path("release-notes.md"))
    args = parser.parse_args()
    with args.project.open("rb") as file:
        version = tomllib.load(file)["project"]["version"]
    metadata = release_metadata(args.tag, version, args.sha)
    if args.command == "metadata":
        if args.github_output:
            with args.github_output.open("a") as file:
                file.writelines(f"{key}={value}\n" for key, value in metadata.items())
        print(f"Validated {metadata['tag']} at {metadata['sha']}")
    else:
        write_downloads(args.directory, metadata, args.notes)
        print(f"Validated complete release downloads in {args.directory}")


if __name__ == "__main__":
    main()
