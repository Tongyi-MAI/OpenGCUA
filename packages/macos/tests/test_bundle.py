"""Standalone relocation and native protocol smoke test; no GUI or model calls."""
import json
import os
import plistlib
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = ROOT / "build/OpenGCUA.app"
    with tempfile.TemporaryDirectory(prefix="OpenGCUA relocation ", dir=ROOT / "build") as stage:
        bundle = Path(stage) / "Moved App/OpenGCUA.app"
        subprocess.run(["ditto", str(source), str(bundle)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)
        resources = bundle / "Contents/Resources"
        info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
        assert info["CFBundleDevelopmentRegion"] == "en"
        assert set(info["CFBundleLocalizations"]) >= {"en", "zh-Hans"}
        for language in ("en", "zh-Hans"):
            for name in ("Localizable.strings", "InfoPlist.strings"):
                assert (resources / f"{language}.lproj" / name).is_file()
        python = resources / "python/bin/python3"
        script = """
import asyncio, json, sys
from pathlib import Path
import mai_ui, httpx, mac_agent
assert Path(mai_ui.__file__).is_relative_to(Path(sys.prefix))
async def check():
    async with mai_ui.Runtime() as runtime:
        async with runtime.session() as session:
            assert not (await session.discover()).resources
asyncio.run(check())
print('Relocated Python, SDK native library and harness OK')
"""
        subprocess.run([str(python), "-I", "-B", "-c", script], cwd=stage, check=True)
        env = {**os.environ, "GENERALIST_DATA_DIR": str(Path(stage) / "user data")}
        for key in ("PYTHONPATH", "PYTHONHOME", "GENERALIST_DRIVER_APP", "GENERALIST_DRIVER_SOCKET"):
            env.pop(key, None)
        # Deliberately set an unrelated CLI credential: native host must not use it.
        env["API_KEY"] = "must-not-use-this-key"
        proc = subprocess.Popen([str(python), "-I", "-B", "-u", str(resources / "runtime/native_host.py")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, cwd=stage, text=True)
        try:
            assert json.loads(proc.stdout.readline()) == {"event": "ready", "protocol": 1}
            proc.stdin.write(json.dumps({"command": "start", "task_id": "relocation", "text": "test", "config": {
                "endpoint": "https://example.invalid/v1", "model": "test", "api_key": ""}}) + "\n")
            proc.stdin.flush()
            event = json.loads(proc.stdout.readline())
            assert event["event"] == "finished" and event["state"] == "failed", event
            assert "API Key" in event["text"], event
            proc.stdin.write('{"command":"shutdown"}\n'); proc.stdin.flush()
            out, err = proc.communicate(timeout=10)
            assert proc.returncode == 0, err
            assert "must-not-use-this-key" not in out + err
            print("Relocated host handshake, missing-key failure, shutdown and credential isolation OK")
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()


if __name__ == "__main__":
    main()
