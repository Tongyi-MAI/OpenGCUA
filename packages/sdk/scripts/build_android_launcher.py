"""Build the small Android credential launcher with an installed Android NDK."""

import argparse
import os
import platform
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ndk", type=Path, default=os.environ.get("ANDROID_NDK_HOME"))
    parser.add_argument("--abi", choices=["arm64-v8a", "x86_64"], default="arm64-v8a")
    args = parser.parse_args()
    if not args.ndk:
        parser.error("Pass --ndk or set ANDROID_NDK_HOME")
    root = Path(__file__).resolve().parents[1]
    host = "darwin-x86_64" if platform.system() == "Darwin" else "linux-x86_64"
    target = {"arm64-v8a": "aarch64-linux-android", "x86_64": "x86_64-linux-android"}[args.abi]
    compiler = args.ndk / "toolchains/llvm/prebuilt" / host / "bin" / f"{target}30-clang"
    output = root / "adapters/android/out" / f"mu-android-shell-{args.abi}"
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(compiler),
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fPIE",
            "-pie",
            "-Wl,-z,relro,-z,now",
            str(root / "adapters/android/launcher/main.c"),
            "-o",
            str(output),
        ],
        check=True,
    )
    print(output)


if __name__ == "__main__":
    main()
