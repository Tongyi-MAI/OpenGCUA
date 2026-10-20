import asyncio
import json
import socket
from types import SimpleNamespace as Obj

import pytest
import code_runtime
from overlay_guard import impact
from native_host import TaskControl, NativeHost


def device():
    image = Obj(_data={"target": {"image_token": "image:1"}, "width": 2000, "height": 1000,
                       "geometry": {"x": -100, "y": 50, "width": 1000, "height": 500}})
    node = Obj(id="node:1", bounds=Obj(x=200, y=100, width=40, height=20))
    return Obj(platform="macos", _backend=Obj(_observations={"img": image, "ax": Obj(_tree=Obj(nodes=[node]))}))


def click(**values):
    return dict(op="action", action="point_click", delivery="foreground", image_token="image:1", x=1000, y=500, **values)


def test_geometry_converts_backing_pixels_to_global_points_and_drag_path():
    d = device()
    assert impact(d, click()) == {"kind": "pointer", "points": [{"x": 400, "y": 300}]}
    q = click(); q.update(action="drag", end_x=1800, end_y=900)
    assert impact(d, q)["points"] == [{"x": 400, "y": 300}, {"x": 800, "y": 500}]
    q = click(); q.update(node_id="node:1", action="click")
    assert impact(d, q)["points"] == [{"x": 220, "y": 110}]


@pytest.mark.parametrize("q", [dict(op="observe", screenshot=True), dict(op="observe", tree=True),
    dict(op="list_windows"), dict(op="shell"), dict(op="action", action="press_key", delivery="foreground"),
    dict(op="action", action="input_text", delivery="foreground"),
    dict(op="action", action="point_click", delivery="background")])
def test_captures_typing_background_and_read_only_calls_never_hide(q):
    assert impact(device(), q) is None


def test_android_and_stale_points_do_not_guard_the_mac():
    d = device(); d.platform = "android"
    assert impact(d, click()) is None
    d.platform = "macos"
    q = click(); q["image_token"] = "expired"
    assert impact(d, q) is None


@pytest.mark.parametrize("fail", [False, True])
def test_worker_waits_for_ack_and_releases_overlay_even_on_action_error(monkeypatch, fail):
    d = device(); calls = []
    d._app_id, d._window_id = "app:test", "window:7"
    class Session:
        async def _request(self, dev, message):
            assert calls == ["approval", "begin", "ack"]
            calls.append("dispatch")
            if fail: raise ValueError("provider failed")
            return {"execution": "completed"}
    session = Session()
    worker, parent = socket.socketpair(); worker.settimeout(.1)
    monkeypatch.setattr(code_runtime, "_CONFIG", {"overlay_guard": True, "gui_approval": True})
    monkeypatch.setattr(code_runtime, "_CONTROL_SOCKET", worker)
    monkeypatch.setattr(code_runtime, "_record_tool", lambda *a, **k: None)
    def emit(event):
        if event["type"] == "gui_approval_request":
            calls.append("approval")
            parent.sendall((json.dumps({"kind": "gui_approval_ack", "request_id": event["request_id"], "approved": True})+'\n').encode())
            return
        calls.append(event["phase"])
        if event["phase"] == "begin":
            parent.sendall((json.dumps({"kind": "overlay_ack", "guard_id": event["guard_id"], "ready": True})+'\n').encode())
            calls.append("ack")
    monkeypatch.setattr(code_runtime, "_emit", emit)
    code_runtime._instrument_sdk_session(session)
    try:
        if fail:
            with pytest.raises(ValueError, match="provider failed"): asyncio.run(session._request(d, click()))
        else: asyncio.run(session._request(d, click()))
        assert calls == ["approval", "begin", "ack", "dispatch", "end"]
    finally:
        worker.close(); parent.close()


def test_host_ack_is_fenced_and_reset_does_not_wait(tmp_path):
    async def scenario():
        events = []; host = NativeHost(events.append, tmp_path)
        host.control = TaskControl("active", events.append)
        pending = asyncio.create_task(host.control.overlay({"phase": "begin", "guard_id": "one", "kind": "pointer", "points": []}))
        await asyncio.sleep(0)
        assert events[-1]["event"] == "overlay" and events[-1]["impact"] == "pointer"
        await host.command({"command": "overlay_ack", "task_id": "stale", "guard_id": "one"})
        assert not pending.done()
        await host.command({"command": "overlay_ack", "task_id": "active", "guard_id": "one"})
        await pending
        assert not host.control.overlay_waits
        await host.control.overlay({"phase": "reset"})
        assert events[-1]["phase"] == "reset"
    asyncio.run(scenario())


def test_real_worker_pipe_ack_and_cell_cleanup(tmp_path):
    from pathlib import Path
    from code_executor import CodeExecutor
    async def scenario():
        events = []
        async def handle(event):
            events.append(event["phase"])
        # Keep the real parent/worker pipe but bootstrap no SDK or desktop endpoint.
        worker = tmp_path/'pipe_worker.py'
        worker.write_text("import sys\nsys.path.insert(0, " + repr(str(Path(code_runtime.__file__).parent)) + ")\nimport code_runtime as rt\nrt._bootstrap = lambda loop: {}\nraise SystemExit(rt.main(sys.argv))\n")
        executor = CodeExecutor(runtime_path=worker, work_dir=tmp_path,
            rpc_socket=tmp_path/'unused.sock', timeout=10, rpc_timeout=3, coordinate_range=1000,
            pyautogui_pause=0, enable_bash=False, ui_api='legacy-cua', overlay_handler=handle)
        try:
            result = await executor.run("""import code_runtime as rt
rt._emit({'type':'overlay_guard','phase':'begin','guard_id':'pipe-test','kind':'pointer','points':[{'x':1,'y':2}]})
reply = b''
while not reply.endswith(b'\\n'):
    reply += rt._CONTROL_SOCKET.recv(1)
import json
assert json.loads(reply)['ready'] is True
rt._emit({'type':'overlay_guard','phase':'end','guard_id':'pipe-test'})
print('guard acknowledged before dispatch')
""", [])
            assert result.returncode == 0, result.stderr
            assert events == ['begin','end','reset']
        finally:
            await executor.close()
    asyncio.run(scenario())


def test_worker_timeout_releases_pending_occlusion(tmp_path):
    from pathlib import Path
    from code_executor import CodeExecutor
    async def scenario():
        events = []
        async def handle(event): events.append(event['phase'])
        worker = tmp_path/'pipe_worker.py'
        worker.write_text("import sys\nsys.path.insert(0, " + repr(str(Path(code_runtime.__file__).parent)) + ")\nimport code_runtime as rt\nrt._bootstrap = lambda loop: {}\nraise SystemExit(rt.main(sys.argv))\n")
        executor = CodeExecutor(runtime_path=worker, work_dir=tmp_path,
            rpc_socket=tmp_path/'unused.sock', timeout=1, rpc_timeout=3, coordinate_range=1000,
            pyautogui_pause=0, enable_bash=False, ui_api='legacy-cua', overlay_handler=handle)
        try:
            result = await executor.run("import code_runtime as rt\nrt._emit({'type':'overlay_guard','phase':'begin','guard_id':'timeout','kind':'pointer'})\nimport time\ntime.sleep(60)", [])
            assert result.timed_out
            assert events == ['begin','reset']
        finally: await executor.close()
    asyncio.run(scenario())
