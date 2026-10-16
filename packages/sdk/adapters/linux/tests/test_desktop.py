"""Real X11/AT-SPI effects; run inside dbus-run-session and xvfb-run."""

import base64
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not os.environ.get("DISPLAY"),
    reason="Needs Linux X11 and accessibility bus",
)


class Client:
    def __init__(self):
        self.p = subprocess.Popen(
            [sys.executable, str(ROOT / "mai_ui_linux/worker.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.seq = 0
        self.events = []

    def send(self, **q):
        self.seq += 1
        q["_request_id"] = self.seq
        self.p.stdin.write(json.dumps(q) + "\n")
        self.p.stdin.flush()
        return self.seq

    def read(self):
        # TextIO may buffer more than one line; no select on its underlying FD.
        value = json.loads(self.p.stdout.readline())
        while "event" in value:
            self.events.append(value)
            value = json.loads(self.p.stdout.readline())
        return value

    def call(self, **q):
        seq = self.send(**q)
        value = self.read()
        assert value["_request_id"] == seq
        assert "error" not in value, value
        return value["result"]

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=8)
        assert self.p.returncode == 0


@pytest.fixture
def desktop(tmp_path):
    wm = subprocess.Popen(["openbox"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    journal = tmp_path / "events.jsonl"
    fixture = subprocess.Popen([sys.executable, str(ROOT / "tests/fixture.py"), str(journal)])
    client = Client()
    try:
        desc = client.call(op="describe", timeout_ms=9000)
        assert desc["accessibility"]["state"] == "ready", desc
        deadline = time.monotonic() + 8
        while True:
            tree = client.call(op="observe", tree=True, timeout_ms=3000)
            if any(n["name"] == "Fixture input" for n in tree["nodes"]):
                break
            assert time.monotonic() < deadline, tree
            time.sleep(0.05)
        yield client, journal
    finally:
        client.close()
        fixture.terminate()
        fixture.wait(timeout=2)
        wm.terminate()
        wm.wait(timeout=2)


def node(client, name):
    tree = client.call(op="observe", tree=True, timeout_ms=3000)
    return next(n for n in tree["nodes"] if n["name"] == name)


def point(client, name):
    n = node(client, name)
    image = client.call(op="observe", screenshot=True, timeout_ms=3000)["images"][0]
    b = n["bounds"]
    return dict(x=b["x"] + b["width"] // 2, y=b["y"] + b["height"] // 2, **image["target"])


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def eventually(predicate):
    until = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < until
        time.sleep(0.01)


def test_vision_keyboard_and_pointer_effects(desktop):
    client, journal = desktop
    from PIL import Image

    capture = client.call(op="observe", screenshot=True, timeout_ms=3000)
    assert Image.open(io.BytesIO(base64.b64decode(capture["images"][0]["png"]))).size == (1280, 720)
    p = point(client, "Fixture button")
    client.call(op="action", action="point_click", count=2, button="primary", **p)
    eventually(lambda: sum(e.get("clicked", False) for e in events(journal)) == 2)
    p = point(client, "Fixture input")
    text = 'Hello < > : _ + { } " | ~ !'
    client.call(op="action", action="input_text", text=text, **p)
    eventually(lambda: any(e.get("text") == text for e in events(journal)))
    client.send(op="action", action="input_text", text="prefix😀")
    assert client.read()["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    assert node(client, "Fixture input")["text"] == text
    p = point(client, "Pointer area")
    for button in ("secondary", "middle"):
        client.call(op="action", action="point_click", button=button, **p)
    client.call(
        op="action", action="wheel_scroll", direction="down", unit="native_steps", amount=3, **p
    )
    eventually(lambda: sum("scroll" in e for e in events(journal)) == 3)
    client.call(
        op="action", action="drag", duration_ms=80, end_x=p["x"] + 25, end_y=p["y"] + 25, **p
    )
    image = client.call(op="observe", screenshot=True)["images"][0]
    client.call(op="action", action="move", x=0, y=0, **image["target"])


def test_accessibility_unicode_clipboard_and_stale_targets(desktop):
    client, journal = desktop
    n = node(client, "Fixture input")
    text = "a😀中éz"
    client.call(op="action", action="set_text", node_id=n["id"], text=text, **n["target"])
    eventually(lambda: any(e.get("text") == text for e in events(journal)))
    after = node(client, "Fixture input")
    assert after["id"] == n["id"]
    client.call(
        op="action",
        action="select_text",
        node_id=n["id"],
        start=1,
        end=3,
        selection_text=text,
        **n["target"],
    )
    selected = node(client, "Fixture input")
    assert selected["selection_start"] == 1 and selected["selection_end"] == 4
    client.call(op="action", action="clipboard_write", text="clipboard 中😀")
    assert client.call(op="clipboard")["formats"]["text/plain"] == "clipboard 中😀"
    client.call(op="action", action="press_key", key="v", modifiers=["ctrl"])
    eventually(lambda: any("clipboard 中😀" in e.get("text", "") for e in events(journal)))
    # An invalid epoch cannot rebind a previously captured object.
    client.send(op="action", action="set_text", node_id=n["id"], text="wrong", ax_epoch="retired")
    assert client.read()["error"]["code"] == "STALE_TARGET"
    assert client.call(op="observe", screenshot=True)["images"]


def test_services_and_cancellation(desktop, tmp_path):
    client, journal = desktop
    path = str(tmp_path / "bytes")
    payload = base64.b64encode(b"\x00\xfffile").decode()
    client.call(op="file", operation="write", path=path, data=payload)
    assert client.call(op="file", operation="read", path=path)["data"] == payload
    client.send(op="file", operation="write", path=path, data=payload)
    assert client.read()["error"]["code"] == "ALREADY_EXISTS"
    client.call(op="file", operation="move", path=path, destination=path + "2")
    assert client.call(op="file", operation="stat", path=path + "2")["size"] == 6
    result = client.call(
        op="shell", argv=["/bin/sh", "-c", "printf 123456789; printf error >&2"], max_output_bytes=3
    )
    assert base64.b64decode(result["stdout"]) == b"123" and result["stdout_truncated"]
    p = point(client, "Pointer area")
    seq = client.send(
        op="action",
        action="drag",
        duration_ms=4000,
        end_x=p["x"] + 40,
        end_y=p["y"],
        _operation_id="drag",
        **p,
    )
    time.sleep(0.05)
    cancel = client.send(op="cancel", operation_id="drag")
    replies = [client.read(), client.read()]
    assert {r["_request_id"] for r in replies} == {seq, cancel}
    assert (
        next(r for r in replies if r["_request_id"] == seq)["error"]["code"]
        == "ACTION_OUTCOME_UNKNOWN"
    )
    from Xlib.display import Display

    d = Display()
    assert not (d.screen().root.query_pointer().mask & 256)
    d.close()
    assert client.call(op="observe", screenshot=True)["images"]


def test_worker_crash_releases_drag_and_private_children(desktop):
    client, _ = desktop
    p = point(client, "Pointer area")
    client.send(
        op="action",
        action="drag",
        duration_ms=4000,
        end_x=p["x"] + 20,
        end_y=p["y"],
        _operation_id="crash-drag",
        **p,
    )
    from Xlib.display import Display

    d = Display()
    eventually(lambda: d.screen().root.query_pointer().mask & 256)
    client.p.kill()
    client.p.wait(timeout=2)
    eventually(lambda: not (d.screen().root.query_pointer().mask & 256))
    d.close()
    # The fixture finalizer still verifies normal cleanup through a replacement worker.
    client.p.stdin.close()
    replacement = Client()
    client.p = replacement.p
    client.seq = 0
    assert client.call(op="describe")["platform"] == "linux"


def test_accessibility_hang_does_not_disable_vision(desktop):
    import signal

    client, _ = desktop
    n = node(client, "Fixture input")
    # Locate only the fixture spawned by this test, not any user's application.
    from Xlib.display import Display

    d = Display()
    windows = d.screen().root.get_full_property(d.intern_atom("_NET_CLIENT_LIST"), 0).value
    pid = None
    for wid in windows:
        window = d.create_resource_object("window", wid)
        if window.get_wm_name() == "mai-ui Linux fixture":
            pid = int(window.get_full_property(d.intern_atom("_NET_WM_PID"), 0).value[0])
    d.close()
    assert pid
    os.kill(pid, signal.SIGSTOP)
    try:
        begin = time.monotonic()
        client.send(
            op="action",
            action="set_text",
            node_id=n["id"],
            text="blocked",
            timeout_ms=150,
            **n["target"],
        )
        assert client.read()["error"]["code"] == "ACTION_OUTCOME_UNKNOWN"
        assert time.monotonic() - begin < 1
        assert client.call(op="observe", screenshot=True)["images"]
    finally:
        os.kill(pid, signal.SIGCONT)
    client.send(op="action", action="set_text", node_id=n["id"], text="old", **n["target"])
    assert client.read()["error"]["code"] == "STALE_TARGET"


def test_held_inputs_wheel_steps_and_release(desktop):
    from Xlib import X, display

    client, journal = desktop
    p = point(client, "Pointer area")
    client.call(op="action", action="key_down", key="shift")
    client.call(op="action", action="button_down", button="primary", **p)
    client.call(op="action", action="move", steps=5, **{**p, "x": p["x"] + 40, "y": p["y"] + 10})
    native = display.Display()
    assert native.screen().root.query_pointer().mask & X.Button1Mask
    assert native.screen().root.query_pointer().mask & X.ShiftMask
    client.call(op="action", action="button_up", button="primary")
    assert not native.screen().root.query_pointer().mask & X.Button1Mask
    assert native.screen().root.query_pointer().mask & X.ShiftMask
    before = sum("scroll" in e for e in events(journal))
    client.call(op="action", action="wheel", delta_x=2, delta_y=-3, unit="steps", **p)
    eventually(lambda: sum("scroll" in e for e in events(journal)) == before + 5)
    client.send(op="action", action="wheel", delta_x=1, delta_y=1, unit="pixels", **p)
    assert client.read()["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    client.call(op="release_inputs")
    assert not native.screen().root.query_pointer().mask & (X.ShiftMask | X.Button1Mask)
    native.close()
