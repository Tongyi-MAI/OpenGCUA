#!/usr/bin/env python3
"""Build a relocatable native .app with Python, SDK and the pinned signed driver."""
import argparse
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

ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "packages/macos"
BUILD = APP_ROOT / "build"


def run(*args, **kwargs):
    subprocess.run([str(x) for x in args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-runtime", action="store_true", help="Recompile UI in an existing local bundle")
    parser.add_argument("--identity", default=os.environ.get("SIGNING_IDENTITY"), help="Certificate name/hash; '-' explicitly selects ad-hoc signing")
    parser.add_argument("--require-stable-signing", action="store_true", help="Fail instead of using an ad-hoc identity")
    parser.add_argument("--app-version", default="0.1.0", help="Numeric MAJOR.MINOR.PATCH bundle version")
    parser.add_argument("--build-number", default="1", help="Numeric bundle build number")
    args = parser.parse_args()
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
        "LSUIElement": True, "NSHighResolutionCapable": True,
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
        lock = json.loads((ROOT / "packages/sdk/adapters/macos/cua-driver.lock.json").read_text())
        cache = BUILD / "cache"
        cache.mkdir(exist_ok=True)
        archive = cache / f"cua-driver-{lock['version']}.tar.gz"
        if not archive.exists():
            urllib.request.urlretrieve(lock["archive"]["url"], archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != lock["archive"]["sha256"]:
            raise SystemExit("CuaDriver archive checksum mismatch")
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
