from __future__ import annotations

import asyncio
from pathlib import Path
import types

import pytest

from code_executor import CodeExecutor
import code_runtime
from skill_context import SkillContext
from skill_registry import SkillRegistry


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    monkeypatch.setattr(code_runtime, "_CONFIG", {})
    monkeypatch.setattr(code_runtime, "_MAI_SESSION", None)
    monkeypatch.setattr(code_runtime, "_SDK_KEEPALIVE", None)
    monkeypatch.setattr(code_runtime, "_SDK_REVIVE_PENDING", False)
    monkeypatch.setattr(code_runtime, "_EXEC_NAMESPACE", None)
    monkeypatch.delattr(code_runtime._REQUEST_CONTEXT, "ask_user_enabled", raising=False)


@pytest.fixture
def registry(tmp_path):
    for name in ("example", "other"):
        folder = tmp_path / "bundle" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}\n---\n\n{name} body.\n", encoding="utf-8",
        )
        (folder / "reference.md").write_text(f"{name} reference", encoding="utf-8")
    result = SkillRegistry(tmp_path / "bundle", tmp_path / "personal")
    result.discover()
    return result


def offline_executor(tmp_path, registry, **options):
    # 使用真实 bootstrap/控制通道，仅替换 SDK 的打开、重置和关闭，不启动桌面或浏览器。
    worker = tmp_path / "session_controls_worker.py"
    worker.write_text(
        "import sys, types\n"
        f"sys.path.insert(0, {str(Path(code_runtime.__file__).parent)!r})\n"
        "import code_runtime as rt\n"
        "class Session:\n"
        "    def begin_turn(self): pass\n"
        "    def end_turn(self): pass\n"
        "    def reset_output_context(self): pass\n"
        "async def opened():\n"
        "    return object(), object(), Session(), types.SimpleNamespace(ui=object())\n"
        "async def reset():\n"
        "    return rt._MAI_SESSION, types.SimpleNamespace(ui=object())\n"
        "async def closed(**kwargs): pass\n"
        "rt._open_sdk_session = opened\n"
        "rt._reset_sdk_session = reset\n"
        "rt._close_sdk_session = closed\n"
        "rt._start_keepalive = lambda loop: None\n"
        "raise SystemExit(rt.main(sys.argv))\n", encoding="utf-8",
    )
    return CodeExecutor(
        runtime_path=worker, work_dir=tmp_path / "work", rpc_socket=tmp_path / "unused.sock",
        timeout=5, rpc_timeout=1, coordinate_range=1000, pyautogui_pause=0,
        enable_bash=False, skill_roots=(registry.bundled_root, registry.personal_root), **options,
    )


def skill_events(result):
    return [event for event in result.control["events"] if event["type"] == "skill_loaded"]


@pytest.fixture
def host_skill_commit(tmp_path, registry):
    """按主循环绑定方式提交：source 校验 + activate(expected_digest) + 同步 executor 选择。"""
    context = SkillContext(registry)
    calls = []
    host = types.SimpleNamespace(context=context, calls=calls, executor=None)

    def handler(event):
        calls.append(dict(event))
        # activate 会直接写入选择，故 source 须在其前校验，否则拒绝后 host 上下文已被污染。
        current = context.records()
        source = current[0]["source"] if current else registry.load(event["name"]).info.source
        if source != event["source"]:
            raise ValueError("source root mismatch")
        context.activate(event["name"], expected_digest=event["digest"])
        record = context.records()[0]
        host.executor.set_skill_selection(context.selected_name, record["digest"], context.revision)

    def make(**options):
        host.executor = offline_executor(tmp_path, registry, skill_handler=options.pop("skill_handler", handler), **options)
        return host.executor

    host.make = make
    return host


async def wait_until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_executor_serializes_host_controls_and_dynamic_ask_user_flag(tmp_path, registry):
    executor = offline_executor(tmp_path, registry)
    config = executor._config()
    assert config["ask_user_enabled"] is True
    assert (config["selected_skill"], config["skill_digest"], config["skill_revision"]) == (None, None, 0)
    document = registry.load("example")
    executor.set_skill_selection("example", document.info.digest, 7)
    executor.ask_user_enabled = False
    configured = executor._config()
    assert configured["ask_user_enabled"] is False
    assert (configured["selected_skill"], configured["skill_digest"], configured["skill_revision"]) == (
        "example", document.info.digest, 7,
    )
    configured["selected_skill"] = "other"
    assert executor._config()["selected_skill"] == "example"
    executor.set_skill_selection(None, None, 8)
    assert executor._config()["selected_skill"] is None


@pytest.mark.parametrize("name,digest,revision", [
    ("", None, 1), (None, "digest", 1), ("example", "", 1),
    ("example", None, -1), ("example", None, True),
])
def test_invalid_executor_selection_is_not_saved(tmp_path, registry, name, digest, revision):
    executor = offline_executor(tmp_path, registry)
    with pytest.raises(ValueError):
        executor.set_skill_selection(name, digest, revision)
    assert executor._config()["selected_skill"] is None
    assert executor._config()["skill_revision"] == 0


def test_bootstrap_configures_selection_without_loading_body(monkeypatch, registry):
    device = types.SimpleNamespace(ui=object())
    async def opened():
        return object(), object(), object(), device
    monkeypatch.setattr(code_runtime, "_open_sdk_session", opened)
    monkeypatch.setattr(code_runtime, "_start_keepalive", lambda loop: None)
    monkeypatch.setattr(code_runtime, "_SDK_RUNTIME", None)
    monkeypatch.setattr(code_runtime, "_SDK_RESOURCE", None)
    monkeypatch.setattr(code_runtime, "_SDK_DEVICE", None)
    events = []
    monkeypatch.setattr(code_runtime, "_emit", events.append)
    digest = registry.load("example").info.digest
    code_runtime._CONFIG = {
        "skill_roots": [str(registry.bundled_root), str(registry.personal_root)],
        "selected_skill": "example", "skill_digest": digest, "skill_revision": 3,
    }
    loop = asyncio.new_event_loop()
    try:
        namespace = code_runtime._bootstrap(loop)
        runtime = namespace["skills"]
        assert runtime.selected_name == "example" and runtime.revision == 3
        assert events == []
        with pytest.raises(ValueError, match="单槽"):
            runtime.load("other")
        runtime.load("example")
        assert events[0]["digest"] == digest and events[0]["revision"] == 3
    finally:
        loop.close()


def test_worker_syncs_replace_clear_and_task_reset_without_losing_pin(tmp_path, registry):
    async def scenario():
        executor = offline_executor(tmp_path, registry)
        digest = registry.load("example").info.digest
        executor.set_skill_selection("example", digest, 1)
        try:
            first = await executor.run("saved_skills = skills\nsaved = skills.load('example')\nprint(saved.body)", [])
            assert first.returncode == 0, first.stderr
            assert len(skill_events(first)) == 1
            process = executor._process
            executor.start_skill_task()
            await executor.reset_task()
            executor.set_skill_selection("example", digest, 1)
            same = await executor.run("assert skills is saved_skills\nassert skills.load('example') is saved", [])
            assert same.returncode == 0, same.stderr
            assert skill_events(same) == [] and executor._process is process
            executor.set_skill_selection("other", registry.load("other").info.digest, 2)
            executor.start_skill_task()
            await executor.reset_task()
            replaced = await executor.run("assert skills.selected_name == 'other'\nprint(skills.load('other').body)", [])
            assert replaced.returncode == 0, replaced.stderr
            assert skill_events(replaced)[0]["name"] == "other"
            denied = await executor.run("skills.load('example')", [])
            assert denied.returncode == 1 and "单槽" in denied.stderr
            assert skill_events(denied) == []
            executor.set_skill_selection(None, None, 3)
            cleared = await executor.run("assert skills.selected_name is None\nprint(skills.load('example').body)", [])
            assert cleared.returncode == 0, cleared.stderr
            assert skill_events(cleared)[0]["name"] == "example"
            still = await executor.run("assert skills.selected_name == 'example'\nassert skills.load('example') is not saved", [])
            assert still.returncode == 0, still.stderr
            assert skill_events(still) == []
        finally:
            await executor.close()
    asyncio.run(scenario())


def test_worker_model_first_selection_is_preserved_then_acknowledged_by_host(tmp_path, registry):
    async def scenario():
        executor = offline_executor(tmp_path, registry)
        context = SkillContext(registry)
        try:
            failed = await executor.run("skills.load('missing')", [])
            assert failed.returncode == 1 and skill_events(failed) == []
            first = await executor.run("saved = skills.load('example')", [])
            assert first.returncode == 0, first.stderr
            event = skill_events(first)[0]
            assert event["selection_source"] == "model"
            same = await executor.run("assert skills.load('example') is saved", [])
            assert same.returncode == 0 and skill_events(same) == []
            context.activate(event["name"], expected_digest=event["digest"])
            executor.set_skill_selection(context.selected_name, context.records()[0]["digest"], context.revision)
            acknowledged = await executor.run("assert skills.load('example') is saved", [])
            assert acknowledged.returncode == 0 and skill_events(acknowledged) == []
        finally:
            await executor.close()
    asyncio.run(scenario())


def test_skill_handler_commits_first_load_before_done_and_cancel_keeps_pin(registry, host_skill_commit):
    async def scenario():
        executor = host_skill_commit.make()
        digest = registry.load("example").info.digest
        try:
            cell = asyncio.create_task(executor.run("skills.load('example')\ntime.sleep(30)", []))
            await wait_until(lambda: host_skill_commit.calls)
            # cell 仍在执行时 host 已提交，不依赖 run 结果。
            assert not cell.done()
            assert host_skill_commit.calls[0]["selection_source"] == "model"
            assert host_skill_commit.calls[0]["revision"] == 0
            assert host_skill_commit.context.selected_name == "example"
            committed = executor._config()
            assert (committed["selected_skill"], committed["skill_digest"], committed["skill_revision"]) == (
                "example", digest, 1,
            )
            executor.interrupt_active()
            with pytest.raises(asyncio.CancelledError):
                await cell
            assert executor._config()["selected_skill"] == "example"
            assert executor._config()["skill_revision"] == 1
            assert host_skill_commit.context.selected_name == "example"
            assert host_skill_commit.context.revision == 1
            idle = await executor.run("print('not run')", [])
            assert idle.session_reset and idle.control["stop_reason"] == "session_reset"
            # 新 worker 从 host 配置恢复 pin，而非重新空槽。
            denied = await executor.run("assert skills.selected_name == 'example'\nskills.load('other')", [])
            assert denied.returncode == 1 and "单槽" in denied.stderr and not denied.session_reset
            assert skill_events(denied) == [] and len(host_skill_commit.calls) == 1
            loaded = await executor.run("print(skills.load('example').body)", [])
            assert loaded.returncode == 0, loaded.stderr
            assert "example body." in loaded.stdout
            # _apply_event 仍保留已提交事件，兼容最终轨迹；重复提交不递增 revision。
            assert [event["selection_source"] for event in skill_events(loaded)] == ["user"]
            assert len(host_skill_commit.calls) == 2 and host_skill_commit.calls[1]["revision"] == 1
            assert host_skill_commit.context.revision == 1 and executor._config()["skill_revision"] == 1
        finally:
            await executor.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("forged,expected", [
    ({"digest": "forged"}, "ValueError"),
    ({"source": "elsewhere"}, "source root mismatch"),
])
def test_skill_handler_rejection_stops_cell_without_injecting_event(registry, host_skill_commit, forged, expected):
    async def scenario():
        executor = host_skill_commit.make()
        info = registry.load("example").info
        event = {"type": "skill_loaded", "name": "example", "source": info.source, "digest": info.digest,
                 "selection_source": "model", "revision": 0, **forged}
        try:
            rejected = await asyncio.wait_for(executor.run(f"""import code_runtime as rt
rt._emit({event!r})
print('must not run')
time.sleep(30)
""", []), 3)
            assert rejected.returncode == 1 and not rejected.timed_out and rejected.session_reset
            assert rejected.control["stop_reason"] == "skill_commit_failed"
            assert "SKILL_COMMIT_FAILED" in rejected.feedback() and expected in rejected.stderr
            assert "must not run" not in rejected.stdout
            assert skill_events(rejected) == []
            failure = [e for e in rejected.control["events"] if e["type"] == "skill_commit_failed"]
            assert failure[0]["error_code"] == "SKILL_COMMIT_FAILED" and failure[0]["name"] == "example"
            assert len(host_skill_commit.calls) == 1
            assert host_skill_commit.context.selected_name is None
            assert executor._config()["selected_skill"] is None and executor._config()["skill_revision"] == 0
            usable = await executor.run("assert skills.selected_name is None\nprint(skills.load('other').body)", [])
            assert usable.returncode == 0, usable.stderr
            assert host_skill_commit.context.selected_name == "other"
        finally:
            await executor.close()
    asyncio.run(scenario())


def offline_host(event):
    raise RuntimeError("host offline")


@pytest.mark.parametrize("handler,expected", [
    (offline_host, "RuntimeError: host offline"),
    (lambda event: asyncio.sleep(0), "必须是同步函数"),
])
def test_skill_handler_error_is_reported_for_real_load(host_skill_commit, handler, expected):
    async def scenario():
        executor = host_skill_commit.make(skill_handler=handler)
        try:
            failed = await asyncio.wait_for(executor.run("skills.load('example')\nprint('must not run')\ntime.sleep(30)", []), 3)
            assert failed.returncode == 1 and failed.session_reset and not failed.timed_out
            assert failed.control["stop_reason"] == "skill_commit_failed"
            assert expected in failed.stderr and "must not run" not in failed.stdout
            assert skill_events(failed) == [] and executor._config()["selected_skill"] is None
        finally:
            await executor.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("forged", [
    {"digest": ""}, {"name": None}, {"source": 3}, {"revision": 5}, {"revision": True}, {"revision": None},
])
def test_invalid_skill_event_never_reaches_handler(registry, host_skill_commit, forged):
    async def scenario():
        executor = host_skill_commit.make()
        info = registry.load("example").info
        event = {"type": "skill_loaded", "name": "example", "source": info.source, "digest": info.digest,
                 "selection_source": "model", "revision": 0, **forged}
        try:
            rejected = await asyncio.wait_for(executor.run(f"import code_runtime as rt\nrt._emit({event!r})\ntime.sleep(30)", []), 3)
            assert rejected.returncode == 1 and rejected.session_reset
            assert rejected.control["stop_reason"] == "skill_commit_failed"
            assert "SKILL_COMMIT_FAILED" in rejected.stderr
            assert host_skill_commit.calls == [] and skill_events(rejected) == []
            assert host_skill_commit.context.selected_name is None
        finally:
            await executor.close()
    asyncio.run(scenario())


def test_worker_digest_failure_emits_nothing_and_user_can_choose_another(tmp_path, registry):
    async def scenario():
        executor = offline_executor(tmp_path, registry)
        executor.set_skill_selection("example", "wrong", 1)
        try:
            failed = await executor.run("skills.load('example')", [])
            assert failed.returncode == 1 and "changed" in failed.stderr
            assert skill_events(failed) == [] and not failed.session_reset
            executor.set_skill_selection("other", None, 2)
            valid = await executor.run("skills.load('other')", [])
            assert valid.returncode == 0, valid.stderr
            assert skill_events(valid)[0]["name"] == "other"
        finally:
            await executor.close()
    asyncio.run(scenario())


def test_model_variables_do_not_change_host_selection_or_ask_user_config(tmp_path, registry):
    async def scenario():
        executor = offline_executor(tmp_path, registry, ask_user_enabled=False)
        digest = registry.load("example").info.digest
        executor.set_skill_selection("example", digest, 1)
        try:
            first = await executor.run("""import code_runtime as rt
saved = skills.load('example')
skills = None
selected_skill = 'other'
skill_digest = 'wrong'
skill_revision = 99
ask_user_enabled = True
rt._CONFIG['ask_user_enabled'] = True
ask_user('must be refused')
""", [])
            assert first.returncode == 1 and "ASK_USER_DISABLED" in first.stderr
            assert first.control["ask_user"] is None
            assert executor._config()["selected_skill"] == "example"
            assert executor.ask_user_enabled is False
            next_cell = await executor.run("assert skills.load('example') is saved\nask_user('still refused')", [])
            assert next_cell.returncode == 1 and "ASK_USER_DISABLED" in next_cell.stderr
            assert skill_events(next_cell) == [] and next_cell.control["ask_user"] is None
        finally:
            await executor.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("question", ["Which file?", ""])
def test_disabled_ask_user_raises_explicit_error_without_emitting(monkeypatch, question):
    code_runtime._CONFIG = {"ask_user_enabled": False}
    events = []
    monkeypatch.setattr(code_runtime, "_emit", events.append)
    with pytest.raises(RuntimeError, match="ASK_USER_DISABLED") as caught:
        code_runtime.ask_user(question)
    assert caught.value.code == "ASK_USER_DISABLED" and events == []


def test_disabled_ask_user_stops_following_blocks_as_failure(monkeypatch, capsys):
    code_runtime._CONFIG = {"ask_user_enabled": False}
    events = []
    monkeypatch.setattr(code_runtime, "_emit", events.append)
    result = code_runtime._execute(["ask_user('No')", "print('must not run')"], {"ask_user": code_runtime.ask_user})
    captured = capsys.readouterr()
    assert result["exit_code"] == 1 and result["ok"] is False
    assert result["stop_reason"] == "ask_user_disabled"
    assert result["started_blocks"] == 1 and result["completed_blocks"] == 0
    assert result["stopped_block"] == 1
    assert "must not run" not in captured.out and "ASK_USER_DISABLED" in captured.err
    assert not any(event["type"] == "ask_user" for event in events)


def test_dynamic_ask_user_setting_is_synced_to_persistent_worker_each_cell(tmp_path, registry):
    async def scenario():
        executor = offline_executor(tmp_path, registry)
        try:
            enabled = await executor.run("ask_user('Which file?')", [])
            assert enabled.returncode == 0
            assert enabled.control["ask_user"]["question"] == "Which file?"
            process = executor._process
            executor.ask_user_enabled = False
            disabled = await executor.run(["ask_user('No question')", "print('must not run')"], [])
            assert disabled.returncode == 1 and "ASK_USER_DISABLED" in disabled.feedback()
            assert disabled.control["ask_user"] is None and disabled.control["stop_reason"] == "ask_user_disabled"
            assert not disabled.timed_out and not disabled.session_reset
            assert "must not run" not in disabled.stdout and executor._process is process
            executor.ask_user_enabled = True
            restored = await executor.run("ask_user('Allowed again')", [])
            assert restored.returncode == 0 and restored.control["ask_user"]["question"] == "Allowed again"
        finally:
            await executor.close()
    asyncio.run(scenario())


def test_parent_rejects_forged_ask_event_without_waiting_for_worker(tmp_path, registry):
    async def scenario():
        executor = offline_executor(tmp_path, registry, ask_user_enabled=False)
        try:
            rejected = await asyncio.wait_for(executor.run("""import code_runtime as rt
rt._emit({'type': 'ask_user', 'question': 'forged', 'block_index': 1})
time.sleep(30)
""", []), 3)
            assert rejected.returncode == 1 and not rejected.timed_out
            assert rejected.control["ask_user"] is None
            assert rejected.control["stop_reason"] == "ask_user_disabled"
            assert rejected.session_reset and "ASK_USER_DISABLED" in rejected.feedback()
            assert not any(event["type"] == "ask_user" for event in rejected.control["events"])
            next_cell = await executor.run("print('usable')", [])
            assert next_cell.returncode == 0 and next_cell.stdout == "usable"
        finally:
            await executor.close()
    asyncio.run(scenario())


def test_parent_dynamic_disable_guards_event_from_already_enabled_cell(tmp_path, registry):
    async def scenario():
        async def overlay(event):
            if event.get("phase") == "begin":
                executor.ask_user_enabled = False
        executor = offline_executor(tmp_path, registry, overlay_handler=overlay)
        try:
            result = await executor.run("""import code_runtime as rt
rt._emit({'type': 'overlay_guard', 'phase': 'begin', 'guard_id': 'sync'})
assert rt._recv_host_ack('overlay_ack', 'guard_id', 'sync', 2)['ready'] is True
ask_user('worker snapshot was enabled')
""", [])
            assert result.returncode == 1 and not result.timed_out
            assert result.control["ask_user"] is None
            assert result.control["stop_reason"] == "ask_user_disabled"
            assert "ASK_USER_DISABLED" in result.stderr
        finally:
            await executor.close()
    asyncio.run(scenario())
