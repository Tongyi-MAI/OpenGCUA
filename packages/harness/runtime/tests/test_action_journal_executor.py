from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import types
from pathlib import Path

import pytest

from action_journal import ActionJournal
from code_executor import CodeExecutor


# 真实 worker 协议/执行循环，仅 SDK 和 bootstrap 替换为无 GUI、无网络实现。
FAKE_WORKER = r"""
import asyncio, json, sys, types, uuid
from pathlib import Path
import code_runtime as rt

class MuError(Exception):
    def __init__(self, code, message="", receipt=None):
        self.code, self.receipt = code, receipt
        super().__init__(code + ": " + message)

for name in ("mai_ui", "mai_ui.unified", "mai_ui.unified.runtime",
             "mai_ui.unified.browser", "mai_ui.unified.resources", "mai_ui.unified.services"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["mai_ui"].MuError = MuError
runtime_module = sys.modules["mai_ui.unified.runtime"]

async def call(core, request):
    return await core.call(request)
runtime_module.call = call
runtime_module.uid = lambda: uuid.uuid4().hex

class Core:
    def __init__(self):
        self.receipts, self.cached = {}, {}
    async def call(self, request):
        message = request.get("wire", request)
        action_id = message.get("action_id")
        if action_id in self.cached:
            return self.cached[action_id]
        if action_id:
            self.receipts[action_id] = dict(action_id=action_id, session_id=mu.id, resource_id="resource",
                                           based_on="obs", execution="queued", effect="unknown")
        if message.get("delay"):
            await asyncio.sleep(0.1)
        if action_id:
            self.receipts[action_id]["execution"] = "running"
        with open(Path(rt._CONFIG["work_dir"]) / "native_calls.jsonl", "a") as stream:
            stream.write(json.dumps(message) + "\n")
        if message.get("delay"):
            await asyncio.sleep(message["delay"])
        if message.get("failure"):
            code = message["failure"]
            if code in ("EXACT_REJECTED", "CANCELLED_BEFORE_DISPATCH"):
                self.receipts[action_id].update(execution="rejected" if code == "EXACT_REJECTED"
                                               else "cancelled_before_dispatch", effect="not_applied")
            else:
                self.receipts[action_id].update(execution="unknown", effect="unknown")
            if code == "CANCELLED_BEFORE_DISPATCH":
                raise asyncio.CancelledError()
            raise MuError(code, "原始失败")
        if action_id:
            self.receipts[action_id].update(execution="completed", effect="applied")
        result = {"ok": True, "native_value": "原始回执业务正文", "execution": "completed", "effect": "applied"}
        self.cached[action_id] = result
        return result

class Session:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self._runtime = types.SimpleNamespace(_core=Core())
        self._devices = {}
        self.auto_reconnect = True
    async def _request(self, device, message):
        return await runtime_module.call(self._runtime._core, dict(op="request", wire=message))
    async def action_status(self, action_id):
        return dict(self._runtime._core.receipts[action_id])
    def begin_turn(self): pass
    def end_turn(self): pass
    def reset_output_context(self): pass
    async def unregister(self, name):
        self._devices.pop(name, None)
        return await runtime_module.call(self._runtime._core, dict(op="unregister", id=name))

class Device:
    platform = "macos"
    resource_id, id = "resource", "attachment"
    _app_id, _window_id = "app", "window"
    def __init__(self, session):
        self._session = session
    async def act(self, action_id, **options):
        return await self._session._request(self, dict(op="action", action="click", action_id=action_id, **options))
    async def browser(self, action_id, **options):
        return await self._session._request(self, dict(op="browser", operation="evaluate", action_id=action_id, **options))

# 用 SDK 现有两种绑定方式测试：browser 的 from-import 别名，以及 resources 的延迟导入。
exec('''from mai_ui.unified.runtime import call
async def create(mu, action_id=None):
    return await call(mu._runtime._core, dict(op="browser_create", session=mu.id, name="private", action_id=action_id))
async def upload(mu, action_id=None):
    return await call(mu._runtime._core, dict(op="upload_files", session=mu.id, action_id=action_id))
''', vars(sys.modules["mai_ui.unified.browser"]))
exec('''async def launch(mu, action_id=None):
    from mai_ui.unified.runtime import call
    return await call(mu._runtime._core, dict(op="apps_launch", session=mu.id, name="fake", action_id=action_id))
''', vars(sys.modules["mai_ui.unified.resources"]))
exec('''from mai_ui.unified.runtime import call
async def compose(mu, action_id=None):
    return await call(mu._runtime._core, dict(op="copy_files", session=mu.id, action_id=action_id))
''', vars(sys.modules["mai_ui.unified.services"]))

mu = Session()
device = Device(mu)
mu._devices[device.id] = device

def bootstrap(loop):
    rt._MAI_SESSION = mu
    rt._instrument_sdk_session(mu)
    return dict(mu=mu, device=device, asyncio=asyncio,
                factories=sys.modules["mai_ui.unified.browser"],
                apps=sys.modules["mai_ui.unified.resources"],
                compose=sys.modules["mai_ui.unified.services"])
rt._bootstrap = bootstrap
rt._close_sdk_session = lambda **kwargs: asyncio.sleep(0)
raise SystemExit(rt.main(sys.argv))
"""


REAL_PUBLIC_WORKER = r'''
import asyncio, json, sys, types
import code_runtime as rt
from mai_ui.unified import runtime as sdk_rt, browser as sdk_browser, resources as sdk_resources
from mai_ui.unified.services import Files, Transfers

class Core:
    def __init__(self): self.receipts = {}
    def state(self, wire):
        request = json.loads(wire)
        if request["op"] == "status":
            if request["id"] not in self.receipts: raise ValueError("UNKNOWN_ACTION")
            return json.dumps(self.receipts[request["id"]])
        return "{}"
    def freeze(self, scope): pass
    async def call(self, wire):
        request = json.loads(wire)
        message = request.get("wire", request)
        action_id = message.get("action_id")
        if action_id and request["op"] != "unregister":
            self.receipts[action_id] = dict(action_id=action_id, execution="completed", effect="applied",
                                           resource_id="r", based_on="o")
        op = request["op"]
        if op == "apps_launch":
            result = dict(id="app:fake", label="Fake", running=True, pid=1)
        elif op == "browser_create":
            result = dict(resource=dict(id="web"), attachment=dict(id="web:a", name="web", resource_id="web", access="control"))
        elif op == "unregister":
            result = dict(attachment_id=request["id"], state="detached", in_flight_drained=True)
        else:
            result = dict(ok=True)
        return json.dumps(result)

class Device:
    platform = "macos"
    _app_id = _window_id = _display_id = _tab_id = None
    _handles = ()
    def __init__(self, session, endpoint=None, info=None):
        self._session = session
        self.id = info.id if info else "a"
        self.name = info.name if info else "fake"
        self.resource_id = info.resource_id if info else "r"
        self._endpoint = endpoint
    def _require(self, name): pass
    def _check(self): pass
    def _whole_device(self): pass

core = Core()
runtime = types.SimpleNamespace(_core=core, _closed=False, _sessions=set(),
                                _endpoints={"web": object()})
runtime._cache_endpoint = lambda info: types.SimpleNamespace(id=info["id"])
mu = sdk_rt.Session(runtime)
runtime._sessions.add(mu)
device = Device(mu)
mu._devices[device.id] = device
apps = sdk_resources.Apps(device)
apps._bind = lambda info: info
# 使用真实 BrowserFactories.create；只替换返回 handle 构造，避免碰 native provider。
sdk_browser.Device = Device
files = Files(device)
transfers = Transfers(mu)

def bootstrap(loop):
    rt._MAI_SESSION = mu
    rt._instrument_sdk_session(mu)
    return dict(mu=mu, apps=apps, factories=mu.browsers, transfers=transfers,
                source=files.path("/source"), destination=files.path("/destination"), asyncio=asyncio)
rt._bootstrap = bootstrap
rt._close_sdk_session = lambda **kwargs: asyncio.sleep(0)
raise SystemExit(rt.main(sys.argv))
'''


# 真实 mai_ui Runtime/Session/Files/Shell/Processes 与 Rust authority；仅把设备端换成纯 Python bridge
# adapter（不连驱动、不碰 GUI/TCC/文件系统）。adapter 收到的就是 native 发出的真实 wire。
BRIDGE_WORKER = r'''
import asyncio, base64, json, sys
from pathlib import Path
import code_runtime as rt
import mai_ui

SHELL = dict(id="local", state="ready", default_interpreter="/bin/sh", interpreters=["/bin/sh"],
             kind="host_process", default_cwd="/", argv=True, streaming=False, stdin=True, pty=False,
             cancellation="process_group", identity=dict(platform="macos", principal="fake", uid=1, gid=1))
DESCRIPTION = dict(protocol_major=1, platform="macos", provider="fake.bridge", shell_providers=[SHELL],
                   capabilities={name: dict(state="ready") for name in ["shell.run"] + [
                       "files." + op for op in ("read", "write", "stat", "list", "mkdir", "remove", "move")]})

class Adapter:
    platform, provider = "macos", "fake.bridge"
    async def start(self): pass
    async def close(self): pass
    async def request(self, message):
        if message["op"] == "describe":
            return dict(result=DESCRIPTION)
        with open(Path(rt._CONFIG["work_dir"]) / "native_calls.jsonl", "a") as stream:
            stream.write(json.dumps(message) + "\n")
        path = message.get("path")
        if path == "/hang":
            await asyncio.sleep(30)
        if path == "/exists":
            return dict(error=dict(code="ALREADY_EXISTS", message="exists"))
        if path == "/unknown":
            return dict(error=dict(code="TIMEOUT", message="device lost reply"))
        if message["op"] == "shell":
            return dict(result=dict(state="exited", exit_code=0, stdout=base64.b64encode(b"ok").decode(),
                                    stderr="", stdout_truncated=False, stderr_truncated=False,
                                    duration_ms=1, cleanup="complete"))
        if message.get("operation") == "read":
            return dict(result=dict(data=base64.b64encode(b"private").decode()))
        return dict(result=dict(ok=True))

def bootstrap(loop):
    async def open_session():
        runtime = mai_ui.Runtime()
        info = await runtime.add_adapter(Adapter(), name="fake", resource_id="fake")
        # bridge pump 必须跨 cell 存活；与正式 keepalive 一样不被回复末尾清理。
        rt._SDK_KEEPALIVE = runtime._endpoints["fake"].pump
        mu = runtime.session()
        return mu, await mu.register(info, name="fake")
    mu, device = loop.run_until_complete(open_session())
    rt._MAI_SESSION = mu
    rt._instrument_sdk_session(mu)
    return dict(mu=mu, device=device, files=device.files, shell=device.shell,
                processes=device.processes, asyncio=asyncio)
rt._bootstrap = bootstrap
rt._close_sdk_session = lambda **kwargs: asyncio.sleep(0)
raise SystemExit(rt.main(sys.argv))
'''


def executor(tmp_path, journal=None, real_public=False, source=None, **options):
    worker = tmp_path / "fake_worker.py"
    source = source or (REAL_PUBLIC_WORKER if real_public else FAKE_WORKER)
    worker.write_text("import sys\nsys.path.insert(0, " + repr(str(Path(__file__).parents[1])) + ")\n" + source)
    instance = CodeExecutor(runtime_path=worker, work_dir=tmp_path,
                            rpc_socket=tmp_path / "unused.sock", timeout=5, rpc_timeout=1,
                            coordinate_range=1000, pyautogui_pause=0, enable_bash=False,
                            action_journal=journal, **options)
    instance.set_action_context({"session_id": "host", "task_id": "task", "turn_id": "turn",
                                 "step_id": "step", "step": 1, "policy_revision": 9})
    return instance


def calls(tmp_path):
    path = tmp_path / "native_calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def stages(journal):
    return [json.loads(line)["journal_stage"] for line in journal.path.read_text().splitlines()]


def test_default_none_retains_old_worker_behavior(tmp_path):
    async def scenario():
        instance = executor(tmp_path)
        assert instance.action_journal is None and instance.action_event_handler is None
        assert instance._config()["journal_actions"] is False
        try:
            result = await instance.run("await device.act('old')", [])
            assert result.returncode == 0
            assert len(calls(tmp_path)) == 1
        finally:
            await instance.close()
    asyncio.run(scenario())


def test_action_one_success_two_failure_immediate_raw_receipts(tmp_path):
    async def scenario():
        published = []
        journal = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, journal, action_event_handler=published.append)
        try:
            result = await instance.run("await device.act('a', text='私有请求')\n"
                                        "await device.act('b', failure='EXACT_REJECTED')\n"
                                        "await device.act('c')", [])
            assert result.returncode == 1
            assert [call["action_id"] for call in calls(tmp_path)] == ["a", "b"]
            one, two = journal.snapshot()
            assert one["raw_result"]["native_value"] == "原始回执业务正文"
            assert one["receipt"]["execution"] == "completed" and one["receipt"]["effect"] == "applied"
            assert two["receipt"]["execution"] == "rejected" and two["error"]["code"] == "EXACT_REJECTED"
            assert two["journal_stage"] == "rejected"
            assert all(item["verification"] == "unverified" for item in journal.snapshot())
            assert published[0]["policy_revision"] == 9 and published[0]["step_id"] == "step"
            assert one["cell_request_id"] != one["action_id"]
            assert "私有请求" not in json.dumps(result.control, ensure_ascii=False)
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("fail_at", ["intent_recorded", "dispatching", "receipt_received"])
def test_fsync_failure_prevents_dispatch_or_next_action(tmp_path, monkeypatch, fail_at):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, journal)
        original_transition, fsync = journal.transition, os.fsync
        def transition(event, context):
            if event["journal_stage"] == fail_at:
                def broken(fd): raise OSError("模拟落盘失败")
                monkeypatch.setattr(os, "fsync", broken)
            try:
                return original_transition(event, context)
            finally:
                monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(journal, "transition", transition)
        try:
            result = await instance.run("await device.act('a')\nawait device.act('b')", [])
            assert result.returncode != 0 and result.session_reset
            assert "JOURNAL_UNAVAILABLE" in result.stderr
            assert len(calls(tmp_path)) == (1 if fail_at == "receipt_received" else 0)
            assert "原始回执业务正文" not in result.feedback()
            assert "自动动作事实反馈" in result.feedback()
            if fail_at == "receipt_received":
                assert "派发结果未知" in result.feedback() and "派发前被拒绝，未执行" not in result.feedback()
            else:
                assert "派发前被拒绝，未执行" in result.feedback()
            second = await instance.run("await device.act('c')", [])
            assert second.returncode != 0
            assert len(calls(tmp_path)) == (1 if fail_at == "receipt_received" else 0)
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


def test_same_id_memory_replay_conflict_and_restart_block(tmp_path):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, journal)
        try:
            assert (await instance.run("await device.act('a', text='same')", [])).returncode == 0
            assert (await instance.run("await device.act('a', text='same')", [])).returncode == 0
            assert len(calls(tmp_path)) == 1 and stages(journal) == ["intent_recorded", "dispatching", "receipt_received"]
            conflict = await instance.run("await device.act('a', text='changed')", [])
            assert conflict.session_reset and "ACTION_REPLAY_BLOCKED" in conflict.stderr
            assert len(calls(tmp_path)) == 1
        finally:
            await instance.close()
            journal.close()
        reopened = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, reopened)
        try:
            result = await instance.run("await device.act('a', text='same')", [])
            assert result.session_reset and "ACTION_REPLAY_BLOCKED" in result.stderr
            assert len(calls(tmp_path)) == 1
        finally:
            await instance.close()
            reopened.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [True, False])
def test_inflight_cancellation_or_timeout_recovers_unknown(tmp_path, cancel):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, journal)
        instance.timeout = 0.6 if not cancel else 5
        try:
            run = asyncio.create_task(instance.run("await device.act('a', delay=10)", []))
            for _ in range(200):
                if calls(tmp_path): break
                await asyncio.sleep(0.01)
            assert calls(tmp_path)
            if cancel:
                instance.interrupt_active()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(run, 1)
                notice = await instance.run("await device.act('not-to-replay')", [])
                assert notice.session_reset and notice.control["reset_reason"] == "interrupted"
                assert "上一轮执行被中止" in notice.stderr and "空闲时退出" not in notice.stderr
                assert "本轮代码未执行" in notice.stderr
                assert [item["action_id"] for item in calls(tmp_path)] == ["a"]
            else:
                result = await run
                assert result.timed_out and result.session_reset
            assert journal.recover()[0]["recovery_state"] == "outcome_unknown"
            assert stages(journal) == ["intent_recorded", "dispatching"]
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


def test_receipt_durable_even_when_cell_never_sends_done(tmp_path):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, journal)
        instance.timeout = 0.7
        try:
            result = await instance.run("await device.act('a')\nawait asyncio.sleep(10)", [])
            assert result.timed_out
            assert journal.recover()[0]["recovery_state"] == "receipt_unverified"
            assert journal.snapshot()[0]["raw_result"]["ok"] is True
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


def test_factory_direct_call_aliases_and_browser_request_are_not_double_recorded(tmp_path):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, journal)
        try:
            result = await instance.run("await apps.launch(mu, 'launch')\n"
                                        "await factories.create(mu, 'factory')\n"
                                        "await factories.upload(mu, 'upload')\n"
                                        "await compose.compose(mu, 'compose')\n"
                                        "await device.browser('browser')\n"
                                        "await mu.unregister('attachment')", [])
            assert result.returncode == 0, result.stderr
            assert len(calls(tmp_path)) == 6 and len(journal.snapshot()) == 6
            assert stages(journal) == ["intent_recorded", "dispatching", "receipt_received"] * 6
            assert {record["operation"] for record in journal.snapshot()} == {
                "apps_launch", "browser_create", "upload_files", "copy_files", "browser.evaluate", "unregister",
            }
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


def test_native_progress_is_fact_and_publication_failure_does_not_retry(tmp_path):
    async def scenario():
        published = []
        def publish(event):
            published.append(event)
            raise RuntimeError("发布失败")
        journal = ActionJournal(tmp_path / "j", "host", lambda event: (_ for _ in ()).throw(RuntimeError()))
        instance = executor(tmp_path, journal, action_event_handler=publish)
        try:
            result = await instance.run("await device.act('a', delay=0.2)", [])
            assert result.returncode == 0
            progress = [event for event in published if event.get("type") == "action_progress"]
            assert [event["stage"] for event in progress] == ["waiting_resource", "running"]
            assert all(event["action_id"] == "a" and event["sdk_session_id"] for event in progress)
            assert len(calls(tmp_path)) == 1
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


def test_transition_thread_does_not_block_loop_or_read_stop(tmp_path):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        entered, release = threading.Event(), threading.Event()
        original = journal.transition
        def slow(event, context):
            entered.set()
            release.wait(3)
            return original(event, context)
        journal.transition = slow
        instance = executor(tmp_path, journal)
        try:
            run = asyncio.create_task(instance.run("await device.act('a')", []))
            for _ in range(200):
                if entered.is_set(): break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            instance.interrupt_active()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(run, 0.5)
            assert calls(tmp_path) == []
        finally:
            release.set()
            await instance.close()
            await asyncio.to_thread(journal.close)
    asyncio.run(scenario())


def test_actual_sdk_public_wrappers_use_fake_core_without_native_dispatch(tmp_path):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, journal, real_public=True)
        try:
            result = await instance.run("await apps.launch('Fake', action_id='launch')\n"
                                        "await factories.create(name='web', provider='fake', action_id='factory')\n"
                                        "await transfers.copy(source, destination, action_id='copy')\n"
                                        "await mu.unregister('a')", [])
            assert result.returncode == 0, result.stderr
            assert [item["operation"] for item in journal.snapshot()] == [
                "apps_launch", "browser_create", "copy_files", "unregister",
            ]
            assert stages(journal) == ["intent_recorded", "dispatching", "receipt_received"] * 4
            assert journal.snapshot()[-1]["receipt"] is None
            assert journal.snapshot()[-1]["raw_result"]["state"] == "detached"
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure,stage,code", [
    ("CANCELLED_BEFORE_DISPATCH", "cancelled_before_dispatch", "CANCELLED_BEFORE_DISPATCH"),
    ("TIMEOUT", "outcome_unknown", "TIMEOUT"),
])
def test_native_cancel_code_and_timeout_are_not_guessed_not_dispatched(tmp_path, failure, stage, code):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, journal)
        try:
            result = await instance.run(f"await device.act('a', failure={failure!r})", [])
            assert result.returncode != 0
            assert journal.snapshot()[0]["journal_stage"] == stage
            assert journal.snapshot()[0]["error"]["code"] == code
            assert len(calls(tmp_path)) == 1
            replay = await instance.run("await device.act('a')", [])
            assert replay.session_reset and len(calls(tmp_path)) == 1
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


def test_raised_transition_sends_negative_ack_and_stops_worker(tmp_path):
    async def scenario():
        class BrokenJournal:
            def transition(self, event, context):
                raise RuntimeError("不应公开的私有诊断")
        instance = executor(tmp_path, BrokenJournal())
        try:
            result = await instance.run("await device.act('a')", [])
            assert result.session_reset and result.returncode != 0
            assert calls(tmp_path) == []
            assert "JOURNAL_UNAVAILABLE" in result.stderr and "私有诊断" not in result.stderr
        finally:
            await instance.close()
    asyncio.run(scenario())


def test_concurrent_approval_overlay_and_journal_acks_are_serialized(tmp_path):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        seen = []
        async def approval(event):
            seen.append("approval")
            assert calls(tmp_path) == [] or journal.record_sequence > 1
            return True
        async def overlay(event):
            if event.get("phase") == "begin":
                seen.append("overlay")
                # 当前 begin 发生于对应 dispatch durable 之前。
                assert any(item["journal_stage"] == "intent_recorded" for item in journal.snapshot())
        instance = executor(tmp_path, journal, approval_handler=approval, overlay_handler=overlay)
        code = (
            "import types\n"
            "device._backend = types.SimpleNamespace(_observations={'o': types.SimpleNamespace(_data={"
            "'target': {'image_token': 'i'}, 'geometry': {'x':0,'y':0,'width':100,'height':100},"
            "'width':100,'height':100})})\n"
            "await asyncio.gather(device.act('a', delivery='foreground', image_token='i', x=1, y=1),"
            "device.act('b', delivery='foreground', image_token='i', x=2, y=2))"
        )
        try:
            result = await instance.run(code, [])
            assert result.returncode == 0, result.stderr
            assert seen.count("approval") == seen.count("overlay") == 2
            assert len(calls(tmp_path)) == len(journal.snapshot()) == 2
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


def test_factory_gate_failure_and_no_automatic_permission_retry(tmp_path):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, journal)
        try:
            result = await instance.run("await device.act('a')\n"
                                        "await device.act('b', failure='PERMISSION_DENIED')", [])
            assert result.returncode != 0
            assert len(calls(tmp_path)) == 2
            assert journal.snapshot()[-1]["journal_stage"] == "outcome_unknown"
            journal._failed = True
            result = await instance.run("await factories.create(mu, 'factory')", [])
            assert result.session_reset and result.returncode != 0
            assert len(calls(tmp_path)) == 2
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


def test_sdk_status_unavailable_reports_unknown_not_fake_queue(tmp_path):
    async def scenario():
        published = []
        journal = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, journal, action_event_handler=published.append)
        try:
            result = await instance.run(
                "async def unavailable(action_id): raise RuntimeError('status unavailable')\n"
                "mu.action_status = unavailable\nawait device.act('a', delay=0.1)", [])
            assert result.returncode == 0
            progress = [event for event in published if event.get("type") == "action_progress"]
            assert [event["stage"] for event in progress] == ["unknown"]
            assert progress[0]["execution"] == progress[0]["effect"] == "unknown"
            assert journal.snapshot()[0]["receipt"] is None
            assert journal.snapshot()[0]["raw_result"]["effect"] == "applied"
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["approval", "overlay"])
def test_cancelled_existing_ack_reader_fences_journal_before_next_emit(monkeypatch, phase):
    import code_runtime as rt
    import overlay_guard
    import types
    entered, release = threading.Event(), threading.Event()
    seen = []
    monkeypatch.setattr(rt, "_JOURNAL_BROKEN", False)
    monkeypatch.setattr(rt, "_HOST_ACK_LOCK", asyncio.Lock())
    monkeypatch.setattr(rt, "_CONTROL_SOCKET", object())
    monkeypatch.setattr(rt, "_CONFIG", {"journal_actions": True, "gui_approval": True, "overlay_guard": True})
    monkeypatch.setattr(rt, "_emit", seen.append)
    monkeypatch.setattr(rt, "_check_active_request", lambda: None)
    monkeypatch.setattr(rt, "_install_journal_direct_calls", lambda session: None)
    monkeypatch.setattr(overlay_guard, "impact", lambda *args: {"kind": "unknown_pointer"})
    def recv(kind, key, request_id, timeout):
        if kind == "action_journal_ack":
            return {"allowed": True}
        entered.set()
        release.wait(2)
        return None
    monkeypatch.setattr(rt, "_recv_host_ack", recv)
    class Session:
        id = "sdk"
        async def _request(self, *args):
            pytest.fail("被取消的 guard 不得到达 native")
    async def approved(*args, **kwargs): return True
    async def scenario():
        try:
            if phase == "approval":
                task = asyncio.create_task(rt._request_gui_approval("app", "window"))
            else:
                monkeypatch.setattr(rt, "_request_gui_approval", approved)
                session = Session()
                rt._instrument_sdk_session(session)
                device = types.SimpleNamespace(platform="macos", _app_id="app", _window_id="window")
                task = asyncio.create_task(session._request(device, {
                    "op": "action", "action_id": "a", "action": "click", "delivery": "foreground",
                }))
            for _ in range(100):
                if entered.is_set(): break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            count = len(seen)
            with pytest.raises(rt._JournalGateError):
                await rt._journal_transition({"action_id": "b"}, "intent_recorded")
            assert len(seen) == count
        finally:
            release.set()
    asyncio.run(scenario())


def test_worker_handshake_mismatch_has_bounded_read_and_poison(monkeypatch):
    import code_runtime as rt
    seen = []
    monkeypatch.setattr(rt, "_JOURNAL_BROKEN", False)
    monkeypatch.setattr(rt, "_CONTROL_SOCKET", object())
    monkeypatch.setattr(rt, "_emit", seen.append)
    def wrong_ack(kind, key, handshake_id, timeout):
        assert kind == "action_journal_ack" and timeout == 15
        # _recv_host_ack 本身对 fence 不符返回 None；这里模拟该真实结果。
        return None
    monkeypatch.setattr(rt, "_recv_host_ack", wrong_ack)
    async def scenario():
        with pytest.raises(rt._JournalGateError, match="JOURNAL_UNAVAILABLE"):
            await rt._journal_transition({"action_id": "a"}, "intent_recorded")
        with pytest.raises(rt._JournalGateError):
            await rt._journal_transition({"action_id": "b"}, "dispatching")
        assert len(seen) == 1 and seen[0]["handshake_id"]
    asyncio.run(scenario())


def test_approval_precedes_dispatch_durable_gate_without_gui(tmp_path):
    async def scenario():
        seen = []
        journal = ActionJournal(tmp_path / "j", "host")
        async def approval(event):
            seen.append(event)
            assert stages(journal) == ["intent_recorded"]
            return False
        instance = executor(tmp_path, journal, approval_handler=approval)
        try:
            result = await instance.run("await device.act('a', delivery='foreground')", [])
            assert result.returncode == 1 and len(seen) == 1
            assert calls(tmp_path) == []
            assert journal.recover()[0]["recovery_state"] == "rejected"
            assert journal.snapshot()[0]["error"]["code"] == "PERMISSION_DENIED"
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


def receipts(journal):
    return {item["action_id"]: item for item in journal.snapshot() if item["journal_stage"] != "intent_recorded"
            and item["journal_stage"] != "dispatching"}


def test_real_sdk_files_shell_processes_journal_wire_command_id(tmp_path):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, journal, source=BRIDGE_WORKER)
        try:
            result = await instance.run(
                "await files.write('/a', b'hi', action_id='w1')\n"
                "await files.mkdir('/d')\n"
                "await files.move('/a', '/b', action_id='mv')\n"
                "await files.remove('/b', recursive=True, action_id='rm')\n"
                "await shell.run('echo hi', action_id='sh')\n"
                "proc = await processes.run(['/bin/echo', 'x'])\n"
                "print('PROC=' + proc.command_id)\n"
                "await files.read('/b')\nawait files.stat('/b')\nawait files.list('/')", [])
            assert result.returncode == 0, result.stderr
            native = calls(tmp_path)
            assert [(m["op"], m.get("operation")) for m in native] == [
                ("file", "write"), ("file", "mkdir"), ("file", "move"), ("file", "remove"),
                ("shell", None), ("shell", None), ("file", "read"), ("file", "stat"), ("file", "list"),
            ]
            # read/stat/list 原样派发但不进 journal，也不被补 ID。
            assert all("command_id" not in m and "action_id" not in m for m in native[6:])
            assert "cHJpdmF0ZQ==" not in journal.path.read_text()
            records = journal.snapshot()
            assert stages(journal) == ["intent_recorded", "dispatching", "receipt_received"] * 6
            done = receipts(journal)
            ids = [m["command_id"] for m in native[:6]]
            assert list(done) == ids and len(set(ids)) == 6
            assert ids[0] == "w1" and ids[2:5] == ["mv", "rm", "sh"]
            assert [done[i]["operation"] for i in ids] == [
                "file.write", "file.mkdir", "file.move", "file.remove", "shell", "shell"]
            for action_id in ids:
                record = done[action_id]
                # SDK 原字段 command_id 不被改名；journal 键与 native status 键同值。
                assert record["request"]["command_id"] == action_id and "action_id" not in record["request"]
                assert record["receipt"]["action_id"] == action_id
                assert record["receipt"]["execution"] == "completed"
                # bridge 未报告 effect：保留 native 的 unknown，不伪造 applied。
                assert record["receipt"]["effect"] == "unknown"
                assert record["cell_request_id"] != action_id
            write = done["w1"]["request"]
            assert (write["op"], write["operation"], write["path"], write["data"], write["overwrite"]) == (
                "file", "write", "/a", "aGk=", False)
            assert done["rm"]["request"]["recursive"] is True
            # 无 ID mkdir：worker 用 SDK uid() 生成 command_id，adapter 收到同一值。
            assert re.fullmatch(r"[0-9a-f]{32}", ids[1])
            # processes.run 的 command_id 由 native prepare_shell 生成，不再另补。
            assert f"PROC={ids[5]}" in result.stdout and re.fullmatch(r"\d+-\d+", ids[5])
            assert native[5]["argv"] == ["/bin/echo", "x"] and done[ids[5]]["request"]["argv"] == ["/bin/echo", "x"]
            assert len({record["cell_request_id"] for record in records}) == 1
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("fail_at", ["intent_recorded", "dispatching"])
@pytest.mark.parametrize("code", [
    "await files.write('/a', b'x', action_id='w')",
    "await files.mkdir('/d')",
    "await files.remove('/d')",
    "await shell.run('echo hi', action_id='s')",
    "await processes.run(['/bin/echo', 'x'])",
])
def test_real_sdk_files_shell_negative_ack_never_dispatches(tmp_path, monkeypatch, fail_at, code):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        original = journal.transition
        def transition(event, context):
            if event["journal_stage"] == fail_at:
                return {"allowed": False, "error": {"code": "JOURNAL_UNAVAILABLE"}}
            return original(event, context)
        monkeypatch.setattr(journal, "transition", transition)
        instance = executor(tmp_path, journal, source=BRIDGE_WORKER)
        try:
            result = await instance.run(code, [])
            assert result.returncode != 0 and result.session_reset
            assert "JOURNAL_UNAVAILABLE" in result.stderr
            assert calls(tmp_path) == []
            assert stages(journal) == ([] if fail_at == "intent_recorded" else ["intent_recorded"])
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


def test_real_sdk_file_rejected_unknown_and_replay_lifecycle(tmp_path):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, journal, source=BRIDGE_WORKER)
        try:
            rejected = await instance.run("await files.mkdir('/exists', action_id='ex')", [])
            assert rejected.returncode != 0 and "ALREADY_EXISTS" in rejected.stderr
            record = receipts(journal)["ex"]
            assert record["journal_stage"] == "rejected" and record["error"]["code"] == "ALREADY_EXISTS"
            assert (record["receipt"]["execution"], record["receipt"]["effect"]) == ("rejected", "not_applied")
            replay = await instance.run("await files.mkdir('/exists', action_id='ex')", [])
            assert replay.session_reset and "ACTION_REPLAY_BLOCKED" in replay.stderr
            assert len(calls(tmp_path)) == 1 and journal.record_sequence == 3
            unknown = await instance.run("await files.write('/unknown', b'x', action_id='u')", [])
            assert unknown.returncode != 0 and len(calls(tmp_path)) == 2
            record = receipts(journal)["u"]
            assert record["journal_stage"] == "outcome_unknown"
            assert record["error"]["code"] == "ACTION_OUTCOME_UNKNOWN"
            assert record["receipt"]["execution"] == "unknown"
            assert {item["action_id"]: item["recovery_state"] for item in journal.recover()} == {
                "ex": "rejected", "u": "outcome_unknown"}
            # native 未知结果后 fence 端点；SDK 同步 check 先于 session._request 拒绝，既不派发也不写 journal。
            replay = await instance.run("await files.write('/unknown', b'x', action_id='u')", [])
            assert replay.returncode != 0 and "DEVICE_OFFLINE" in replay.stderr
            assert len(calls(tmp_path)) == 2 and journal.record_sequence == 6
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


def test_real_sdk_same_command_id_memory_replay_and_payload_conflict(tmp_path):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, journal, source=BRIDGE_WORKER)
        try:
            code = "await files.write('/a', b'x', action_id='same')"
            assert (await instance.run(code, [])).returncode == 0
            again = await instance.run(code, [])
            assert again.returncode == 0, again.stderr
            # 同 ID 同请求：journal memory_replay，SDK 内去重，adapter 只收到一次。
            assert len(calls(tmp_path)) == 1
            assert stages(journal) == ["intent_recorded", "dispatching", "receipt_received"]
            conflict = await instance.run("await files.write('/a', b'changed', action_id='same')", [])
            assert conflict.session_reset and "ACTION_REPLAY_BLOCKED" in conflict.stderr
            assert len(calls(tmp_path)) == 1 and journal.record_sequence == 3
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


def test_real_sdk_inflight_file_interrupt_recovers_unknown(tmp_path):
    async def scenario():
        journal = ActionJournal(tmp_path / "j", "host")
        instance = executor(tmp_path, journal, source=BRIDGE_WORKER)
        try:
            run = asyncio.create_task(instance.run("await files.write('/hang', b'x', action_id='h')", []))
            for _ in range(500):
                if calls(tmp_path): break
                await asyncio.sleep(0.01)
            assert calls(tmp_path)[0]["command_id"] == "h"
            instance.interrupt_active()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(run, 2)
            assert stages(journal) == ["intent_recorded", "dispatching"]
            assert journal.recover()[0]["recovery_state"] == "outcome_unknown"
        finally:
            await instance.close()
            journal.close()
    asyncio.run(scenario())


# 真实 SDK wire 形状（取自 bridge adapter 实收请求）；保证 native 缺失时的回退判定与 native 一致。
WIRE_SHAPES = [
    ({"op": "file", "operation": "write", "path": "/a", "data": "aGk=", "overwrite": False,
      "timeout_ms": 5000, "command_id": "w1"}, True),
    ({"op": "file", "operation": "mkdir", "path": "/d", "parents": False, "timeout_ms": 5000}, True),
    ({"op": "file", "operation": "move", "path": "/a", "destination": "/b", "overwrite": False,
      "timeout_ms": 5000}, True),
    ({"op": "file", "operation": "remove", "path": "/b", "recursive": False, "timeout_ms": 5000}, True),
    ({"op": "file", "operation": "read", "path": "/a", "max_bytes": 1048576, "timeout_ms": 5000}, False),
    ({"op": "file", "operation": "stat", "path": "/a", "timeout_ms": 5000}, False),
    ({"op": "file", "operation": "list", "path": "/", "timeout_ms": 5000}, False),
    ({"op": "shell", "command_id": "48376-6", "provider_id": "local", "argv": ["/bin/echo", "x"],
      "max_output_bytes": 1048576, "timeout_ms": 30000}, True),
    ({"op": "action", "action": "click", "action_id": "a", "timeout_ms": 3000}, True),
    ({"op": "clipboard", "operation": "read", "timeout_ms": 5000}, False),
    ({"op": "observe", "tree": True, "screenshot": False, "mode": "latest", "timeout_ms": 1500}, False),
    ({"op": "browser", "operation": "list_tabs"}, False),
    ({"op": "browser", "operation": "navigate", "url": "about:blank"}, True),
]


@pytest.mark.parametrize("wire,mutation", WIRE_SHAPES)
def test_mutation_classifier_matches_native_on_real_sdk_shapes(wire, mutation):
    import code_runtime as rt
    from mai_ui._native import is_mutation
    assert is_mutation(json.dumps(wire)) is mutation
    assert rt._protocol_mutation(wire) is mutation
    assert rt._sdk_mutation(wire) is mutation


def test_journal_wire_keeps_existing_ids_and_fills_sdk_field(monkeypatch):
    import code_runtime as rt
    monkeypatch.setattr(rt, "_CONFIG", {"journal_actions": True})
    write, mkdir, read, shell, action = (WIRE_SHAPES[i][0] for i in (0, 1, 4, 7, 8))
    assert rt._journal_wire(write) is write and rt._journal_wire(shell) is shell
    assert rt._journal_wire(read) is None
    filled = rt._journal_wire(mkdir)
    assert "action_id" not in filled and re.fullmatch(r"[0-9a-f]{32}", filled["command_id"])
    assert "command_id" not in mkdir
    bare_action = {k: v for k, v in action.items() if k != "action_id"}
    assert re.fullmatch(r"[0-9a-f]{32}", rt._journal_wire(bare_action)["action_id"])
    both = {**write, "action_id": "a"}
    assert rt._journal_call_id(both) == ("action_id", "a")
    event = rt._journal_event(types.SimpleNamespace(id="sdk"), None, write)
    assert event["action_id"] == "w1" and event["operation"] == "file.write"
    assert event["request"]["command_id"] == "w1" and "action_id" not in event["request"]
    monkeypatch.setattr(rt, "_CONFIG", {"journal_actions": False})
    assert rt._journal_wire(mkdir) is None and not rt._journal_enabled(write)
