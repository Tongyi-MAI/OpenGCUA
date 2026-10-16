"""Additional real application coverage; optional packages are explicit test dependencies."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest
from test_desktop import ROOT, Client, eventually

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not os.environ.get("DISPLAY"), reason="Requires X11"
)


def capture_until(client, predicate):
    deadline = time.monotonic() + 15
    while True:
        tree = client.call(op="observe", tree=True, timeout_ms=4000)
        matches = [n for n in tree["nodes"] if predicate(n)]
        if matches:
            return tree, matches[0]
        assert time.monotonic() < deadline, tree
        time.sleep(0.05)


def test_qt_unicode_and_native_identity(tmp_path):
    pytest.importorskip("PyQt5")
    journal = tmp_path / "events"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "tests/qt_fixture.py"), str(journal)],
        env={**os.environ, "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1", "QT_ACCESSIBILITY": "1"},
    )
    client = Client()
    try:
        client.call(op="describe", timeout_ms=9000)
        _, node = capture_until(client, lambda n: n["name"] == "Qt fixture input")
        client.call(
            op="action", action="set_text", node_id=node["id"], text="Qt 中😀", **node["target"]
        )
        eventually(
            lambda: (
                journal.exists()
                and any(
                    json.loads(s).get("text") == "Qt 中😀" for s in journal.read_text().splitlines()
                )
            )
        )
        _, after = capture_until(client, lambda n: n["name"] == "Qt fixture input")
        assert after["id"] == node["id"]
        proc.terminate()
        proc.wait(timeout=3)
        client.send(
            op="action", action="set_text", node_id=node["id"], text="stale", **node["target"]
        )
        assert client.read()["error"]["code"] == "STALE_TARGET"
    finally:
        client.close()
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)


@pytest.mark.parametrize("kind", ["writer", "calc"])
def test_libreoffice_bounded_desktop_capture(tmp_path, kind):
    executable = shutil.which("libreoffice")
    if not executable:
        pytest.skip("Install LibreOffice and its GTK3 accessibility backend")
    proc = subprocess.Popen(
        [
            executable,
            "-env:UserInstallation=" + (tmp_path / "profile").as_uri(),
            "--" + kind,
            "--norestore",
            "--nofirststartwizard",
        ],
        env={**os.environ, "SAL_USE_VCLPLUGIN": "gtk3"},
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = Client()
    try:
        client.call(op="describe", timeout_ms=9000)
        tree, _ = capture_until(client, lambda n: "LibreOffice" in n["name"])
        assert len(tree["nodes"]) <= 2048
        assert isinstance(tree["truncated"], bool)
        assert client.call(op="observe", screenshot=True)["images"]
    finally:
        client.close()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=3)


def test_chromium_desktop_accessibility(tmp_path):
    executable = next(
        (p for n in ["google-chrome", "chromium", "chromium-browser"] if (p := shutil.which(n))),
        None,
    )
    executable = os.environ.get("MAI_UI_TEST_LINUX_CHROMIUM", executable)
    if not executable:
        pytest.skip("Set MAI_UI_TEST_LINUX_CHROMIUM to a Linux Chromium executable")
    page = tmp_path / "fixture.html"
    page.write_text('<input aria-label="Chromium fixture input"><button>Fixture button</button>')
    proc = subprocess.Popen(
        [
            executable,
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--force-renderer-accessibility",
            "--no-first-run",
            "--user-data-dir=" + str(tmp_path / "profile"),
            page.as_uri(),
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = Client()
    try:
        client.call(op="describe", timeout_ms=9000)
        _, node = capture_until(client, lambda n: n["name"] == "Chromium fixture input")
        client.call(
            op="action",
            action="set_text",
            node_id=node["id"],
            text="Chromium 中😀",
            **node["target"],
        )
        _, after = capture_until(
            client, lambda n: n["name"] == "Chromium fixture input" and n["text"] == "Chromium 中😀"
        )
        assert after["id"] == node["id"]
    finally:
        client.close()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=3)
