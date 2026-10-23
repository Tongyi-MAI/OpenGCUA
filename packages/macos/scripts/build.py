#!/usr/bin/env python3
"""Build a relocatable native .app with Python, SDK and the pinned signed driver."""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from signing import resolve_identity, pin_identity, sign
from browser_storage_patch import patch_storage_helpers

ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "packages/macos"
BUILD = APP_ROOT / "build"


def run(*args, **kwargs):
    subprocess.run([str(x) for x in args], check=True, **kwargs)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_archive(url: str, archive: Path, expected: str, *,
                     chunk_size: int = 4 * 1024 * 1024, workers: int = 4) -> None:
    """分段下载锁定依赖；只在完整 SHA256 验证通过后把残片提升为缓存。"""
    if archive.is_file() and file_sha256(archive) == expected:
        return
    if chunk_size < 1 or not 1 <= workers <= 4:
        raise ValueError("Invalid archive download bounds")
    partial = archive.with_name(archive.name + ".part")
    if archive.exists() and not partial.exists():
        # 旧 urlretrieve 可能留下截断文件，沿用连续前缀但不把它当完整缓存。
        archive.replace(partial)
    request = urllib.request.Request(url, headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"})
    with urllib.request.urlopen(request, timeout=30) as response:
        match = re.fullmatch(r"bytes 0-0/(\d+)", response.headers.get("Content-Range", ""))
        if response.status != 206 or not match:
            raise RuntimeError("Archive server did not honor Content-Range; partial cache was not used")
        total = int(match[1])
        if not 0 < total <= 512 * 1024 * 1024:
            raise RuntimeError("Archive size is outside download bounds")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > total:
        raise RuntimeError("Partial archive exceeds remote size; remove the stale .part before retrying")

    def segment(start: int, end: int) -> bytes:
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={
                    "Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    actual = response.headers.get("Content-Range", "")
                    if response.status != 206 or actual != f"bytes {start}-{end}/{total}":
                        raise RuntimeError(f"Unexpected Content-Range for archive segment {start}-{end}: {actual}")
                    data = response.read(end - start + 2)
                    if len(data) != end - start + 1:
                        raise RuntimeError(f"Incomplete archive segment {start}-{end}: got {len(data)} bytes")
                    return data
            except OSError:
                if attempt == 2:
                    raise
        raise RuntimeError("Archive segment retry budget exhausted")

    positions = iter(range(offset, total, chunk_size))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = deque()
        def submit_next() -> None:
            start = next(positions, None)
            if start is not None:
                pending.append(pool.submit(segment, start, min(start + chunk_size - 1, total - 1)))
        for _ in range(workers):
            submit_next()
        with partial.open("ab") as handle:
            while pending:
                # 按位置落盘，断开后仅留下连续前缀；待处理数据最多四个分段。
                handle.write(pending.popleft().result())
                handle.flush()
                print(f"Driver download: {handle.tell()}/{total} bytes", flush=True)
                submit_next()
    if file_sha256(partial) != expected:
        raise RuntimeError("CuaDriver archive checksum mismatch; partial download was not promoted")
    partial.replace(archive)


def copy_bundled_skills(source: Path, destination: Path) -> None:
    """Copy only repository-shipped Skills into this build's app resources."""
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"bundled Skill directory missing or symlinked: {source}")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError(f"bundled Skills contain a symlink: {source}")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=False)


def main():
    global BUILD
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-runtime", action="store_true", help="Recompile UI in an existing local bundle")
    parser.add_argument("--identity", default=os.environ.get("SIGNING_IDENTITY"), help="Certificate name/hash; '-' explicitly selects ad-hoc signing")
    parser.add_argument("--require-stable-signing", action="store_true", help="Fail instead of using an ad-hoc identity")
    parser.add_argument("--app-version", default="0.3.0", help="Numeric MAJOR.MINOR.PATCH bundle version")
    parser.add_argument("--build-number", default="1", help="Numeric bundle build number")
    parser.add_argument("--build-dir", type=Path, default=APP_ROOT / "build", help="Separate output directory; leaves other builds untouched")
    args = parser.parse_args()
    BUILD = args.build_dir.expanduser().resolve()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", args.app_version):
        parser.error("--app-version must be MAJOR.MINOR.PATCH")
    if not re.fullmatch(r"[1-9][0-9]*", args.build_number):
        parser.error("--build-number must be a positive integer")
    BUILD.mkdir(parents=True, exist_ok=True)
    try:
        args.identity = resolve_identity(args.identity, BUILD, args.require_stable_signing)
    except (ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc
    bundle = BUILD / "OpenGCUA.app"
    if args.skip_runtime and not (bundle / "Contents/Resources/python/bin/python3").exists():
        legacy = BUILD / "Generalist.app"
        if not (legacy / "Contents/Resources/python/bin/python3").exists():
            raise SystemExit("--skip-runtime requires an existing runtime; run a full build first.")
        shutil.copytree(legacy, bundle, symlinks=True, dirs_exist_ok=True)
    resources = bundle / "Contents/Resources"
    macos = bundle / "Contents/MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    for localization in (APP_ROOT / "Resources").glob("*.lproj"):
        shutil.copytree(localization, resources / localization.name, dirs_exist_ok=True)
    run("swiftc", "-parse-as-library", "-swift-version", "5", "-O", "-target", "arm64-apple-macosx14.0",
        *sorted((APP_ROOT / "Sources").glob("*.swift")), "-o", macos / "OpenGCUA",
        "-framework", "AppKit", "-framework", "SwiftUI", "-framework", "Security",
        "-framework", "Speech", "-framework", "AVFoundation", "-framework", "Carbon")
    logo = APP_ROOT / "Resources/OpenGCUALogo.png"
    shutil.copy2(logo, resources / logo.name)
    run("swift", APP_ROOT / "scripts/make_icon.swift", BUILD / "OpenGCUA.iconset", logo)
    run("iconutil", "-c", "icns", BUILD / "OpenGCUA.iconset", "-o", resources / "OpenGCUA.icns")
    info = {
        "CFBundleDevelopmentRegion": "en", "CFBundleLocalizations": ["en", "zh-Hans"],
        "CFBundleExecutable": "OpenGCUA", "CFBundleIdentifier": "app.generalist.mac",
        "CFBundleName": "OpenGCUA", "CFBundleDisplayName": "OpenGCUA", "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": args.app_version, "CFBundleVersion": args.build_number, "LSMinimumSystemVersion": "14.0",
        "LSUIElement": False, "NSHighResolutionCapable": True,
        "CFBundleIconFile": "OpenGCUA.icns",
        "NSMicrophoneUsageDescription": "OpenGCUA uses your microphone for voice conversations and to carry out or stop tasks you request.",
        "NSScreenCaptureUsageDescription": "OpenGCUA views the apps you select to carry out your tasks.",
        "NSSpeechRecognitionUsageDescription": "OpenGCUA uses on-device speech recognition to turn speech into task text.",
    }
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    # Retire only obsolete copied build assets, never an installed app.
    (macos / "Generalist").unlink(missing_ok=True)
    (resources / "Generalist.icns").unlink(missing_ok=True)
    runtime = resources / "runtime"
    runtime.mkdir(exist_ok=True)
    # Runtime source only: never copy local keys, configs, recordings, or runs.
    for source in (ROOT / "packages/harness/runtime").glob("*.py"):
        shutil.copy2(source, runtime / source.name)
    copy_bundled_skills(ROOT / "packages/harness/skills", resources / "skills")
    if not args.skip_runtime:
        uv = shutil.which("uv")
        if not uv:
            raise SystemExit("Build requires uv; end users do not need it.")
        run(uv, "python", "install", "3.12")
        python = Path(subprocess.check_output([uv, "python", "find", "--managed-python", "3.12"], text=True).strip()).resolve()
        python_home = python.parent.parent
        destination = resources / "python"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(python_home, destination, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # This copy belongs to the application, not uv's managed installation.
        (destination / "lib/python3.12/EXTERNALLY-MANAGED").unlink(missing_ok=True)
        wheels = BUILD / "wheels"
        wheels.mkdir(exist_ok=True)
        run(uv, "build", "--wheel", ROOT / "packages/sdk", "--python", "3.12", "--out-dir", wheels)
        wheel = max(wheels.glob("qwen_intelligence_sdk-*-cp312-*-macosx*.whl"), key=lambda p: p.stat().st_mtime)
        run(uv, "pip", "install", "--python", destination / "bin/python3", "--require-hashes", "-r", APP_ROOT / "requirements.lock")
        run(uv, "pip", "install", "--python", destination / "bin/python3", "--no-deps", wheel, ROOT / "packages/harness/runtime")
        run(uv, "pip", "check", "--python", destination / "bin/python3")
        # SDK browser worker ships inside the app and runs on the Node bundled with
        # Playwright for Python: no dependency on the user's Node or ~/.cache/mai-ui.
        adapter = ROOT / "packages/sdk/adapters/browser-playwright"
        worker_root = resources / "browser-worker"
        if worker_root.exists():
            shutil.rmtree(worker_root)
        (worker_root / "dist").mkdir(parents=True)
        for name in ("package.json", "package-lock.json"):
            shutil.copy2(adapter / name, worker_root / name)
        for script in (adapter / "dist").glob("*.js"):
            shutil.copy2(script, worker_root / "dist" / script.name)
        npm = shutil.which("npm")
        if not npm:
            raise SystemExit("Build requires npm to bundle the browser worker; end users do not need it.")
        run(npm, "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund", cwd=worker_root)
        expected = json.loads((adapter / "package.json").read_text())["dependencies"]["playwright"]
        bundled = json.loads((worker_root / "node_modules/playwright/package.json").read_text())["version"]
        if bundled != expected or not (worker_root / "dist/background.js").is_file():
            raise SystemExit(f"Bundled browser worker is incomplete (playwright {bundled}, expected {expected})")
        # 只修补构建副本，不改用户 npm/Python 环境。两条浏览器路径的登录态
        # 内部临时页都必须按后台 target 创建，防止每步快照再次抢前台。
        patch_storage_helpers(worker_root / "node_modules/playwright-core")
        driver_package = destination / "lib/python3.12/site-packages/playwright/driver/package"
        patch_storage_helpers(driver_package)
        lock = json.loads((ROOT / "packages/sdk/adapters/macos/cua-driver.lock.json").read_text())
        cache = BUILD / "cache"
        cache.mkdir(exist_ok=True)
        archive = cache / f"cua-driver-{lock['version']}.tar.gz"
        if not archive.exists():
            shared_archive = APP_ROOT / "build/cache" / archive.name
            if shared_archive.is_file() and file_sha256(shared_archive) == lock["archive"]["sha256"]:
                shutil.copy2(shared_archive, archive)
        download_archive(lock["archive"]["url"], archive, lock["archive"]["sha256"])
        # Extract to a fresh staging directory: never modify a previously registered
        # signed companion in place (macOS protects its CodeResources).
        stage = tempfile.TemporaryDirectory(prefix="driver-", dir=cache)
        with tarfile.open(archive) as tar:
            tar.extractall(stage.name, filter="data")
        driver = Path(stage.name) / f"cua-driver-rs-{lock['version']}-darwin-universal/CuaDriver.app"
        requirement = '=anchor apple generic and identifier "com.trycua.driver" and certificate leaf[subject.OU] = "YCK386LBJ7"'
        run("codesign", "--verify", "--deep", "--strict", "-R", requirement, driver)
        helpers = bundle / "Contents/Helpers"
        helpers.mkdir(exist_ok=True)
        target = helpers / "CuaDriver.app"
        if target.exists():
            shutil.rmtree(target)
        run("ditto", driver, target)
        stage.cleanup()
        notices = resources / "Notices"
        notices.mkdir(exist_ok=True)
        shutil.copy2(ROOT / "packages/sdk/python/mai_ui/THIRD_PARTY_NOTICES.md", notices)
        shutil.copy2(ROOT / "packages/sdk/adapters/macos/cua-driver.lock.json", notices)
        # Keep dependency metadata/licenses in site-packages. No absolute console-script
        # shebangs: the app invokes its interpreter directly; only preserve python bins.
        for entry in (destination / "bin").iterdir():
            if not entry.name.startswith("python"):
                if entry.is_file() or entry.is_symlink(): entry.unlink()
        run(destination / "bin/python3", "-I", "-B", "-c", "import mai_ui, qwen_intelligence, mac_agent, native_host, terminal_tui, httpx; assert qwen_intelligence.Runtime is mai_ui.Runtime; print('Bundled SDK and runtime import OK')")
    # Sign nested Mach-O files before the outer bundle. Preserve upstream driver's
    # original Developer ID: SDK validates it, so never use codesign --deep --force.
    macho_magic = {b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xfe\xed\xfa\xcf'}
    signing = ["--force", "--sign", args.identity]
    if args.identity != "-": signing += ["--options", "runtime", "--timestamp"]
    entitlements = APP_ROOT / "entitlements.plist"
    for path in (resources / "python").rglob("*"):
        if path.is_symlink() or not path.is_file(): continue
        # DWARF files have Mach-O headers but are debug data, not executable
        # code. Some PyObjC wheels include these companion symbol bundles.
        if any(part.endswith(".dSYM") for part in path.parts): continue
        with path.open("rb") as f: magic = f.read(4)
        if magic in macho_magic:
            sign(*signing, "--entitlements", entitlements, path)
    sign(*signing, "--entitlements", entitlements, bundle)
    run("codesign", "--verify", "--deep", "--strict", bundle)
    pin_identity(args.identity, BUILD)
    print(bundle)


if __name__ == "__main__":
    main()
