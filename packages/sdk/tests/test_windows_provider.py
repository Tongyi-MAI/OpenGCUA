"""Offline Windows provider contracts; no native imports or desktop interaction."""

import base64
import copy
import importlib.util
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "adapters/windows/mai_ui_windows"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SOURCE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


transport = load("transport")
provider = load("provider")
Error, Control = transport.Error, transport.Control


class Native:
    desktop_id = "windows:cua:session:1:desktop:default"

    def __init__(self):
        self.started = {44: 12345, 55: 67890}
        self.owners = {101: (44, 7), 202: (55, 8)}

    def process(self, pid):
        if pid not in self.started:
            raise Error("STALE_TARGET")
        return self.started[pid]

    def window(self, hwnd, pid):
        if hwnd not in self.owners or self.owners[hwnd][0] != pid:
            raise Error("STALE_TARGET")
        return self.owners[hwnd][1]

    def close(self):
        pass

    def check_desktop(self):
        pass


class Driver:
    server = {"version": "0.38.0"}
    tools = {
        key: {}
        for key in (
            "list_apps",
            "list_windows",
            "get_window_state",
            "click",
            "set_value",
            "type_text",
            "press_key",
        )
    }
    failed = None
    closed = False

    def __init__(self):
        self.calls = []
        self.snapshot = 0
        self.capture = 0
        self.effect = "unverifiable"
        self.delivery = {"mode": "background"}
        self.error = None
        self.incomplete = False
        self.node_fields = {}
        self.action_result = None
        self.action_reply = None
        self.rows = [
            {
                "pid": 44,
                "window_id": 101,
                "app_name": "Editor",
                "title": "Scratch",
                "bounds": {"x": -800, "y": 20, "width": 800, "height": 600},
            },
            {
                "pid": 55,
                "window_id": 202,
                "app_name": "Sheet",
                "title": "Book",
                "bounds": {"x": 20, "y": 20, "width": 800, "height": 600},
            },
        ]

    def close(self):
        self.closed = True

    def tool(self, name, args, control, *, mutation=False):
        control.check()
        if self.closed:
            raise Error("DEVICE_OFFLINE")
        self.calls.append((name, copy.deepcopy(args), mutation))
        if mutation:
            control.started = True
            if self.action_reply is not None:
                return copy.deepcopy(self.action_reply)
            if self.action_result is not None:
                return {"isError": False, "structuredContent": self.action_result}
            if self.error:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": "Driver refused this exact action"}],
                    "structuredContent": self.error,
                }
            return {
                "structuredContent": {
                    "effect": self.effect,
                    "route": "accessibility",
                    "delivery": self.delivery,
                    "evidence": [],
                }
            }
        if name == "list_apps":
            return {
                "structuredContent": {
                    "apps": [
                        {"pid": 44, "name": "Editor", "path": "editor.exe"},
                        {"pid": 55, "name": "Sheet", "path": "sheet.exe"},
                    ]
                }
            }
        if name == "list_windows":
            rows = [r for r in self.rows if args.get("pid", r["pid"]) == r["pid"]]
            return {"structuredContent": {"windows": copy.deepcopy(rows)}}
        assert name == "get_window_state"
        self.capture += 1
        if args["include_accessibility_tree"]:
            self.snapshot += 1
        sid = f"s{self.snapshot:08x}"
        row = next(r for r in self.rows if r["window_id"] == args["window_id"])
        data = {
            "pid": args["pid"],
            "window_id": args["window_id"],
            "snapshot_id": sid,
            "capture_id": f"capture:{self.capture}",
            "window_bounds": row["bounds"],
            "screenshot_width": 800,
            "screenshot_height": 600,
            "elements_complete": not self.incomplete,
            "truncated": self.incomplete,
            "elements": [
                {
                    "element_index": 0,
                    "element_token": sid + ":0",
                    "role": "Edit",
                    "label": "Document",
                    "value": "before",
                    "enabled": True,
                    "frame": {"x": -790, "y": 30, "w": 750, "h": 500},
                    "actions": ["set_value", "invoke", "expand"],
                }
            ],
        }
        data["elements"][0].update(self.node_fields)
        parts = []
        if args["include_screenshot"]:
            # Header is sufficient for protocol geometry validation; fixture
            # deliberately never asks a GUI/image library to decode pixels.
            png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 800, 600)
            parts = [
                {"type": "image", "mimeType": "image/png", "data": base64.b64encode(png).decode()}
            ]
        return {"structuredContent": data, "content": parts}


@pytest.fixture
def bound():
    driver, native = Driver(), Native()
    p = provider.Provider(driver, native)
    apps = p.dispatch({"op": "list_apps"}, Control())["apps"]
    app = apps[0]["id"]
    window = p.dispatch({"op": "list_windows", "app_id": app}, Control())["windows"][0]["id"]
    return p, driver, native, {"app_id": app, "window_id": window}


def observe(p, scope, **options):
    return p.dispatch(
        {"op": "observe", **scope, "tree": True, "screenshot": False, **options}, Control()
    )


def action(scope, node, name="set_text", **options):
    return {
        "op": "action",
        "action": name,
        **scope,
        "node_id": node["id"],
        "target_token": node["target_token"],
        "snapshot_id": node["target"]["snapshot_id"],
        "delivery": "background",
        "text": "after",
        **options,
    }


def test_background_value_replacement_preserves_scope_and_reports_unknown_effect(bound):
    p, driver, _, scope = bound
    node = observe(p, scope)["nodes"][0]
    result = p.dispatch(action(scope, node), Control())
    name, args, mutation = driver.calls[-1]
    assert (name, mutation) == ("set_value", True)
    assert args == {
        "pid": 44,
        "window_id": 101,
        "element_token": node["target_token"],
        "value": "after",
        "delivery_mode": "background",
    }
    assert result["effect"] == "unknown"
    assert result["delivery"] == {"mode": "background"}
    assert not driver.closed


def test_new_snapshot_rejects_old_node_without_dispatch(bound):
    p, driver, _, scope = bound
    old = observe(p, scope)["nodes"][0]
    observe(p, scope)
    before = sum(call[2] for call in driver.calls)
    with pytest.raises(Error, match="stale"):
        p.dispatch(action(scope, old), Control())
    assert sum(call[2] for call in driver.calls) == before


def test_screenshot_only_does_not_invalidate_ax_node(bound):
    p, driver, _, scope = bound
    node = observe(p, scope)["nodes"][0]
    image = observe(p, scope, tree=False, screenshot=True)["images"][0]
    assert image["width"] == 800
    p.dispatch(action(scope, node), Control())
    assert driver.calls[-1][0] == "set_value"


def test_cross_window_node_cannot_escape_bound_scope(bound):
    p, driver, _, scope = bound
    node = observe(p, scope)["nodes"][0]
    other_app = next(a for a in p.apps if a != scope["app_id"])
    other = p.list_windows(other_app, Control())["windows"][0]
    with pytest.raises(Error):
        p.dispatch(action({"app_id": other_app, "window_id": other["id"]}, node), Control())
    assert not any(call[2] for call in driver.calls)


def test_pid_reuse_does_not_revive_application_binding(bound):
    p, driver, native, scope = bound
    node = observe(p, scope)["nodes"][0]
    native.started[44] += 1
    with pytest.raises(Error) as exc:
        p.dispatch(action(scope, node), Control())
    assert exc.value.code == "STALE_TARGET"
    assert not any(call[2] for call in driver.calls)


def test_closed_and_reopened_hwnd_gets_new_sdk_identity(bound):
    p, driver, _, scope = bound
    removed = driver.rows.pop(0)
    p.list_windows(scope["app_id"], Control())
    driver.rows.insert(0, removed)
    new = p.list_windows(scope["app_id"], Control())["windows"][0]
    assert new["id"] != scope["window_id"]
    with pytest.raises(Error):
        observe(p, scope)


@pytest.mark.parametrize(
    "options", [{"delivery": "foreground"}, {"modifiers": ["ctrl"]}, {"action": "scroll"}]
)
def test_unsupported_semantics_never_dispatch(bound, options):
    p, driver, _, scope = bound
    node = observe(p, scope)["nodes"][0]
    q = action(scope, node)
    q.update(options)
    with pytest.raises(Error) as exc:
        p.dispatch(q, Control())
    assert exc.value.code == "UNSUPPORTED_CAPABILITY"
    assert not any(call[2] for call in driver.calls)


def test_node_text_insertion_is_not_silently_converted_to_append(bound):
    p, driver, _, scope = bound
    node = observe(p, scope)["nodes"][0]
    with pytest.raises(Error) as exc:
        p.dispatch(action(scope, node, "input_text"), Control())
    assert exc.value.code == "UNSUPPORTED_CAPABILITY"
    assert not any(call[2] for call in driver.calls)


def test_driver_refusal_is_not_device_offline_or_foreground_retry(bound):
    p, driver, _, scope = bound
    node = observe(p, scope)["nodes"][0]
    driver.error = {"code": "background_unavailable", "reason": "provider ignores messages"}
    with pytest.raises(Error) as exc:
        p.dispatch(action(scope, node), Control())
    assert exc.value.code == "INPUT_REFUSED"
    assert exc.value.details["native"]["reason"] == "provider ignores messages"
    assert len([call for call in driver.calls if call[2]]) == 1
    assert not driver.closed
    driver.error = None
    assert p.dispatch(action(scope, node), Control())["execution"] == "completed"


def test_coordinate_capture_is_exact_and_single_use(bound):
    p, driver, _, scope = bound
    image = observe(p, scope, screenshot=True)["images"][0]
    q = {"op": "action", "action": "point_click", **scope, **image["target"], "x": 100, "y": 200}
    p.dispatch(q, Control())
    assert driver.calls[-1][1]["x"] == 100  # No second DPI multiplier.
    assert driver.calls[-1][1]["capture_id"] == "capture:1"
    with pytest.raises(Error) as exc:
        p.dispatch(q, Control())
    assert exc.value.code == "STALE_TARGET"


def test_geometry_change_rejects_pixel_click(bound):
    p, driver, _, scope = bound
    image = observe(p, scope, screenshot=True)["images"][0]
    driver.rows[0]["bounds"]["width"] = 900
    q = {"op": "action", "action": "point_click", **scope, **image["target"], "x": 100, "y": 200}
    with pytest.raises(Error) as exc:
        p.dispatch(q, Control())
    assert exc.value.code == "GEOMETRY_CHANGED"
    assert not any(call[2] for call in driver.calls)


def test_partial_accessibility_tree_remains_explicit(bound):
    p, driver, _, scope = bound
    driver.incomplete = True
    value = observe(p, scope)
    assert value["truncated"] is True
    assert value["components"][0]["ax_complete"] is False


def test_unexpected_foreground_delivery_fences_provider(bound):
    p, driver, _, scope = bound
    node = observe(p, scope)["nodes"][0]
    driver.delivery = {"mode": "foreground"}
    with pytest.raises(Error) as exc:
        p.dispatch(action(scope, node), Control())
    assert exc.value.code == "ACTION_OUTCOME_UNKNOWN"
    assert driver.closed


@pytest.mark.parametrize(
    "reply",
    [
        {"isError": True, "structuredContent": {"code": "background_unavailable"}},
        {"isError": False, "structuredContent": {"effect": "refused"}},
        {"isError": False, "structuredContent": {"effect": "confirmed"}},
    ],
)
def test_foreground_delivery_fences_even_refused_or_error_replies(bound, reply):
    p, driver, _, scope = bound
    node = observe(p, scope)["nodes"][0]
    reply["structuredContent"]["delivery"] = {"mode": "foreground"}
    driver.action_reply = reply
    with pytest.raises(Error) as exc:
        p.dispatch(action(scope, node), Control())
    assert exc.value.code == "ACTION_OUTCOME_UNKNOWN"
    assert exc.value.details["execution"] == "unknown"
    assert exc.value.details["effect"] == "unknown"
    assert exc.value.details["delivery"] == {"mode": "foreground"}
    assert driver.closed
    before = len(driver.calls)
    with pytest.raises(Error) as retry:
        p.dispatch(action(scope, node), Control())
    assert retry.value.code == "DEVICE_OFFLINE"
    assert len(driver.calls) == before


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"structuredContent": None},
        {"structuredContent": []},
        {"structuredContent": {"execution_state": "unknown", "effect": "confirmed"}},
        {
            "isError": True,
            "structuredContent": {"execution_state": "unknown", "effect": "refused"},
        },
        {"structuredContent": {"code": "tool_output_invalid"}},
        {"isError": True, "structuredContent": {"error": {"code": "tool_output_invalid"}}},
        {"isError": True, "structuredContent": {}},
        {"isError": True, "structuredContent": {"code": "unexpected_driver_error"}},
    ],
)
def test_ambiguous_mutation_reply_fences_before_another_action(bound, reply):
    p, driver, _, scope = bound
    node = observe(p, scope)["nodes"][0]
    driver.action_reply = reply
    with pytest.raises(Error) as exc:
        p.dispatch(action(scope, node), Control())
    assert exc.value.code == "ACTION_OUTCOME_UNKNOWN"
    assert driver.closed
    if exc.value.details is not None:
        assert exc.value.details["execution"] == "unknown"
        assert exc.value.details["effect"] == "unknown"
    before = len(driver.calls)
    with pytest.raises(Error) as retry:
        p.dispatch(action(scope, node), Control())
    assert retry.value.code == "DEVICE_OFFLINE"
    assert len(driver.calls) == before


def test_driver_version_must_match_pinned_contract():
    driver = Driver()
    driver.server = {"version": "0.39.0"}
    with pytest.raises(Error) as exc:
        provider.Provider(driver, Native())
    assert exc.value.code == "VERSION_MISMATCH"


def test_success_envelope_with_refused_effect_is_still_refused(bound):
    p, driver, _, scope = bound
    node = observe(p, scope)["nodes"][0]
    driver.action_result = {
        "effect": "refused",
        "route": "accessibility",
        "error": {"code": "background_delivery_unavailable"},
        "delivery": {"mode": "background", "delivered_count": 0},
    }
    with pytest.raises(Error) as exc:
        p.dispatch(action(scope, node), Control())
    assert exc.value.code == "INPUT_REFUSED"
    assert exc.value.details["native_code"] == "background_delivery_unavailable"
    assert exc.value.details["execution"] == "rejected"
    assert exc.value.details["effect"] == "not_applied"
    assert len([call for call in driver.calls if call[2]]) == 1
    assert not driver.closed


def test_nullable_native_fields_are_not_invented(bound):
    p, driver, _, scope = bound
    driver.node_fields = {"actions": None, "parent_index": None, "value": None}
    node = observe(p, scope)["nodes"][0]
    assert node["actions"] == []
    assert node["parent_id"] is None
    assert node["focused"] is None
    assert node["value_settable"] is None
    assert node["password"] is None


@pytest.mark.parametrize("role", ["Slider", "ComboBox", "TitleBar"])
def test_string_replacement_does_not_coerce_nontext_controls(bound, role):
    p, driver, _, scope = bound
    driver.node_fields = {"role": role}
    node = observe(p, scope)["nodes"][0]
    with pytest.raises(Error) as exc:
        p.dispatch(action(scope, node), Control())
    assert exc.value.code == "UNSUPPORTED_CAPABILITY"
    assert not any(call[2] for call in driver.calls)


def test_windows_inventory_uses_public_dataclass_field_contract(bound):
    p, _, _, scope = bound
    row = p.list_windows(scope["app_id"], Control())["windows"][0]
    assert set(row) == {"id", "app_id", "title", "bounds"}


def test_capture_timestamps_are_sdk_milliseconds(bound):
    p, _, _, scope = bound
    before = int(time.time() * 1000)
    result = observe(p, scope, screenshot=True)
    assert before <= result["captured_at"] <= int(time.time() * 1000)
    assert result["images"][0]["captured_at"] >= before


FAKE_DRIVER = """
import json, sys, time
for line in sys.stdin:
    q=json.loads(line)
    if 'id' not in q: continue
    if q['method']=='initialize': value={'serverInfo':{'version':'0.38.0'},'protocolVersion':'2025-06-18'}
    elif q['method']=='tools/list': value={'tools':[{'name':n} for n in ['list_apps','list_windows','get_window_state','click','set_value']]}
    else:
        mode=q['params']['arguments'].get('mode')
        if mode=='exit': sys.exit(0)
        if mode=='delay': time.sleep(5)
        if mode=='bad_error':
            print(json.dumps({'jsonrpc':'2.0','id':q['id'],'error':'malformed'}),flush=True)
            continue
        value={'structuredContent':{'effect':'confirmed','route':'accessibility','delivery':{'mode':'background'},'evidence':[]}}
    print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':value}),flush=True)
"""


@pytest.fixture
def mcp(tmp_path):
    script = tmp_path / "fake_driver.py"
    script.write_text(FAKE_DRIVER, encoding="utf-8")
    value = transport.MCP(script, command=[sys.executable, "-u", str(script)])
    try:
        yield value
    finally:
        value.close()


def test_real_subprocess_transport_correlates_requests(mcp):
    result = mcp.tool("click", {}, Control(), mutation=True)
    assert result["structuredContent"]["effect"] == "confirmed"


@pytest.mark.parametrize("mode", ["exit", "delay"])
def test_lost_action_reply_fences_transport_and_reports_uncertainty(mcp, mode):
    with pytest.raises(Error) as exc:
        mcp.tool("click", {"mode": mode}, Control(100), mutation=True)
    assert exc.value.code == "ACTION_OUTCOME_UNKNOWN"
    assert mcp.closed
    assert mcp.process.poll() is not None
    with pytest.raises(Error) as retry:
        mcp.tool("click", {}, Control(), mutation=True)
    assert retry.value.code == "DEVICE_OFFLINE"


def test_cancellation_before_dispatch_does_not_close_driver(mcp):
    control = Control()
    control.cancelled.set()
    with pytest.raises(Error) as exc:
        mcp.tool("click", {}, control, mutation=True)
    assert exc.value.code == "CANCELLED_BEFORE_DISPATCH"
    assert not mcp.closed


def test_malformed_rpc_error_fences_mutation_transport(mcp):
    with pytest.raises(Error) as exc:
        mcp.tool("click", {"mode": "bad_error"}, Control(), mutation=True)
    assert exc.value.code == "ACTION_OUTCOME_UNKNOWN"
    assert mcp.closed


def test_blocked_driver_stdin_is_bounded_by_request_deadline(tmp_path):
    script = tmp_path / "blocked_driver.py"
    script.write_text(FAKE_DRIVER + "", encoding="utf-8")
    # Stop reading immediately after returning the tool catalog. A multi-MiB
    # action then fills the OS pipe, exercising writer cancellation rather than
    # merely timing out while waiting for a reply.
    source = FAKE_DRIVER.replace(
        "    print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':value}),flush=True)",
        "    print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':value}),flush=True)\n    if q['method']=='tools/list': time.sleep(30)",
    )
    script.write_text(source, encoding="utf-8")
    mcp = transport.MCP(script, command=[sys.executable, "-u", str(script)])
    started = time.monotonic()
    try:
        with pytest.raises(Error) as exc:
            mcp.tool("click", {"payload": "x" * (2 * 1024 * 1024)}, Control(100), mutation=True)
        assert exc.value.code == "ACTION_OUTCOME_UNKNOWN"
        assert time.monotonic() - started < 5
        assert mcp.process.poll() is not None
    finally:
        mcp.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job lifetime contract")
def test_abrupt_worker_exit_terminates_owned_descendant():
    import ctypes
    from ctypes import wintypes

    source = (
        "import sys, subprocess, time; "
        f"sys.path.insert(0, {str(SOURCE)!r}); "
        "from native import contain_worker; contain_worker(); "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "print(child.pid, flush=True); time.sleep(30)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-u", "-c", source],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = None
    try:
        line = parent.stdout.readline()
        assert line, parent.stderr.read()
        child_pid = int(line)
        # Hold the process handle throughout: cleanup cannot target a reused PID.
        handle = kernel.OpenProcess(0x00100000 | 0x0001, False, child_pid)
        assert handle
        parent.kill()
        parent.wait(timeout=5)
        assert kernel.WaitForSingleObject(handle, 5000) == 0
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if handle:
            if kernel.WaitForSingleObject(handle, 0) != 0:
                kernel.TerminateProcess(handle, 1)
            kernel.CloseHandle(handle)
