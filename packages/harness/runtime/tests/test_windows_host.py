"""Real Windows process tests; no UI, credentials, SDK driver, or model calls."""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import code_runtime
from code_executor import CodeExecutor
from host_platform import IS_WINDOWS


def _executor(tmp_path: Path, *, timeout=10, cleanup="") -> CodeExecutor:
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).parents[1])!r})\n"
        "import code_runtime as rt\n"
        "rt._bootstrap = lambda loop: {}\n" + cleanup +
        "raise SystemExit(rt.main(sys.argv))\n", encoding="utf-8",
    )
    return CodeExecutor(
        runtime_path=worker, work_dir=tmp_path, rpc_socket=tmp_path / "unused.sock",
        timeout=timeout, rpc_timeout=3, coordinate_range=1000,
        pyautogui_pause=0, enable_bash=False,
    )


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows socket sharing and job objects")
def test_windows_worker_preserves_state_and_gracefully_cleans_up(tmp_path):
    marker = tmp_path / "closed"
    cleanup = (
        "async def cleanup(*, close_runtime):\n"
        f"    open({str(marker)!r}, 'w').write('closed')\n"
        "rt._close_sdk_session = cleanup\n"
    )

    async def scenario():
        executor = _executor(tmp_path, cleanup=cleanup)
        try:
            first = await executor.run("value = 41\nprint('ready')", [])
            assert first.returncode == 0, first
            pid = executor._process.pid
            assert first.returncode == 0 and first.stdout.strip() == "ready"
            second = await executor.run("print(value + 1)", [])
            assert second.stdout.strip() == "42"
            assert executor._process.pid == pid
        finally:
            await executor.close()
        assert marker.read_text() == "closed"

    asyncio.run(scenario())


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows process-tree ownership")
@pytest.mark.parametrize("finish", ["interrupt", "timeout", "worker_exit"])
def test_windows_worker_reaps_descendants_before_replacement(tmp_path, finish):
    marker = tmp_path / "child.pid"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)

    async def scenario():
        executor = _executor(tmp_path, timeout=1 if finish == "timeout" else 20)
        code = (
            "import subprocess, sys, time, os\n"
            "from pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"Path({str(marker)!r}).write_text(str(child.pid))\n"
            "time.sleep(0.5)\n" + ("os._exit(7)" if finish == "worker_exit" else "time.sleep(60)")
        )
        running = asyncio.create_task(executor.run(code, []))
        handle = None
        try:
            for _ in range(200):
                if marker.exists():
                    break
                await asyncio.sleep(0.02)
            assert marker.exists(), "worker did not reach controlled child launch"
            handle = kernel.OpenProcess(0x100000, False, int(marker.read_text()))
            assert handle, ctypes.WinError(ctypes.get_last_error())
            if finish == "interrupt":
                executor.interrupt_active()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(running, 5)
            else:
                result = await asyncio.wait_for(running, 5)
                assert result.returncode != 0
                assert result.timed_out is (finish == "timeout")
            assert kernel.WaitForSingleObject(handle, 3000) == 0, "owned descendant leaked"
            followup = await executor.run("print('new session')", [])
            if followup.control["stop_reason"] == "session_reset":
                assert followup.session_reset and followup.stdout == ""
                followup = await executor.run("print('new session')", [])
            assert followup.stdout.strip() == "new session", followup
        finally:
            await executor.close()
            if handle:
                kernel.CloseHandle(handle)

    asyncio.run(scenario())


@pytest.mark.skipif(not IS_WINDOWS, reason="native Windows PowerShell")
def test_powershell_unicode_exit_code_permission_and_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(code_runtime, "_CONFIG", {"work_dir": str(tmp_path), "enable_bash": True})
    monkeypatch.setattr(code_runtime, "_record_tool", lambda *a, **k: None)
    result = code_runtime.powershell("Write-Output 'Grüße 世界'")
    assert result.ok and result.stdout.strip() == "Grüße 世界"
    assert code_runtime.powershell("exit 9").returncode == 9
    result = code_runtime.powershell("Start-Sleep -Seconds 60", timeout=0.3)
    assert result.timed_out and result.returncode == 124
    assert not list(tmp_path.glob("bash_*"))
    code_runtime._CONFIG["enable_bash"] = False
    with pytest.raises(PermissionError):
        code_runtime.powershell("Write-Output 'must not execute'")
    with pytest.raises(NotImplementedError, match="powershell"):
        code_runtime.bash("echo invalid-host")


def test_windows_bootstrap_preserves_sdk_session_authority(monkeypatch):
    session = SimpleNamespace(
        __aenter__=AsyncMock(), close=AsyncMock(), register=AsyncMock(),
    )
    device = SimpleNamespace(apps=SimpleNamespace(list=AsyncMock()))
    session.register.return_value = device
    runtime = SimpleNamespace(
        __aenter__=AsyncMock(), close=AsyncMock(),
        add_windows=AsyncMock(return_value="resource"), session=lambda **k: session,
    )
    monkeypatch.setitem(sys.modules, "mai_ui", SimpleNamespace(Runtime=lambda: runtime))
    monkeypatch.setattr(code_runtime, "IS_WINDOWS", True)
    monkeypatch.setattr(code_runtime, "_instrument_sdk_session", lambda _session: None)
    monkeypatch.setattr(code_runtime, "_create_web_resource", AsyncMock(return_value=None))
    monkeypatch.setenv("GENERALIST_WINDOWS_DRIVER", "C:/fixture/cua-driver.exe")
    monkeypatch.delenv("GENERALIST_ANDROID_SERIAL", raising=False)
    result = asyncio.run(code_runtime._open_sdk_session())
    runtime.add_windows.assert_awaited_once_with(
        name="local-windows", driver_path="C:/fixture/cua-driver.exe",
    )
    session.register.assert_awaited_once_with("resource", name="windows")
    assert result == (runtime, "resource", session, device)
    assert session.auto_reconnect and session.coordinate_space == "normalized_1000"


@pytest.mark.parametrize("background,profile,expected", [
    (True, "isolated", True), (False, "isolated", False),
    (True, "attach", False), (True, "borrow", False),
])
def test_windows_browser_launch_preserves_requested_visibility(monkeypatch, background, profile, expected):
    monkeypatch.setattr(code_runtime, "IS_WINDOWS", True)
    monkeypatch.setenv("MAC_AGENT_BACKGROUND", "1" if background else "0")
    monkeypatch.setenv("MAC_AGENT_WEB_PROFILE", profile)
    runtime = SimpleNamespace(add_browser_factory=AsyncMock())
    asyncio.run(code_runtime._add_web_factory(runtime))
    kwargs = runtime.add_browser_factory.await_args.kwargs
    assert kwargs["headless"] is expected
    assert not kwargs.get("background_launch", False)
    if profile == "isolated":
        assert "channel" not in kwargs
    else:
        assert kwargs["channel"] == "chrome"


def test_windows_prompt_addendum_keeps_sdk_authority(monkeypatch):
    import mac_agent
    monkeypatch.setattr(mac_agent, "IS_WINDOWS", True)
    context = mac_agent.ContextManager(
        task="test", coordinate_range=1000, img_keep_n=5, img_interval=10,
        history_turns=100, micro_compaction=False, micro_trigger_steps=60,
        micro_keep_turns=25, micro_interval=35, todo_diff_snapshot=True,
        max_image_dimension=1920, jpeg_quality=85,
    )
    assert mac_agent.WINDOWS_PLATFORM_NOTE in context.system_prompt
    assert "powershell(cmd" in context.system_prompt
    assert "never silently substitute foreground input" in context.system_prompt


def test_windows_lifecycle_capture_is_not_an_sdk_observation(monkeypatch):
    import sdk_backend
    from PIL import Image, ImageGrab
    monkeypatch.setattr(sdk_backend, "IS_WINDOWS", True)
    grab = []
    def fake_grab(**kwargs):
        grab.append(kwargs)
        return Image.new("RGB", (50, 20))
    monkeypatch.setattr(ImageGrab, "grab", fake_grab)
    result = asyncio.run(sdk_backend.SdkDesktop(session_id="test").observe())
    assert grab == [{"all_screens": True}]
    assert result.width == 50 and result.height == 20
    assert result.structured == {"lifecycle_capture": True}
    assert result.target == {"kind": "desktop", "display": "all"}


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows private memory storage")
def test_windows_memory_uses_local_data_and_retains_approval_boundary(tmp_path, monkeypatch):
    from memory_runtime import resolve_memory_root
    from memory_store import MemoryStore
    from skill_registry import default_skill_roots
    monkeypatch.delenv("GENERALIST_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    root = resolve_memory_root()
    assert root == tmp_path / "local/Generalist/memory"
    assert default_skill_roots(__file__)[1] == tmp_path / "local/Generalist/skills"
    store = MemoryStore(root)
    proposal = store.propose("Writing preference", "Use concise paragraphs", "preference", None)
    assert proposal["status"] == "candidate"
    assert store.recall("Writing preference") == []
    approved = store.approve(proposal["memory_id"], proposal["revision"])
    assert approved["status"] == "approved"
    assert store.recall("Writing preference")
    monkeypatch.setenv("GENERALIST_DATA_DIR", str(tmp_path / "override"))
    assert resolve_memory_root() == tmp_path / "override/memory"


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows ACL failure")
def test_windows_memory_acl_failure_prevents_storage(tmp_path, monkeypatch):
    import memory_store
    from memory_runtime import MemoryService
    root = tmp_path / "memory"
    root.mkdir()
    def reject(*args, **kwargs):
        raise PermissionError("private diagnostic")
    monkeypatch.setattr(memory_store, "open_private_file", reject)
    service = MemoryService(root)
    result = service.propose({"title": "Writing", "content": "Concise paragraphs"})
    assert result["ok"] is False and result["error_code"] == "MEMORY_UNSAFE_PATH"
    assert list(root.iterdir()) == []
    assert "private diagnostic" not in str(result)


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows captured file descriptors")
def test_windows_output_capture_includes_native_children_and_bounds_floods(tmp_path):
    async def scenario():
        executor = _executor(tmp_path)
        try:
            result = await executor.run(
                "import os, sys, subprocess\n"
                "os.write(1, b'native-out\\n')\n"
                "os.write(2, b'native-err\\n')\n"
                "subprocess.run([sys.executable, '-c', \"print('child-out')\"], check=True)\n"
                "os.write(1, b'x' * 400000)\n", [],
            )
            assert result.returncode == 0, result
            assert "native-out" in result.stdout and "child-out" in result.stdout
            assert "native-err" in result.stderr
            assert len(result.stdout.encode("utf-8")) <= 256 * 1024 + 256
            followup = await executor.run("print('next-cell')", [])
            assert followup.stdout.strip() == "next-cell"
        finally:
            await executor.close()
    asyncio.run(scenario())


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows file identity and ACLs")
def test_windows_private_storage_refuses_hardlinks_before_writing(tmp_path):
    from host_platform import open_private_file
    target, linked = tmp_path / "target", tmp_path / "linked"
    target.write_text("keep", encoding="utf-8")
    os.link(target, linked)
    with pytest.raises(OSError, match="multiply linked"):
        open_private_file(linked)
    assert target.read_text(encoding="utf-8") == "keep"


def test_web_state_replacement_truncates_old_private_temporary_file(tmp_path, monkeypatch):
    state = tmp_path / "browser/state.json"
    state.parent.mkdir()
    temporary = state.with_name(state.name + ".tmp")
    temporary.write_text("stale" * 1000, encoding="utf-8")
    monkeypatch.setattr(code_runtime, "_WEB_STATE", state)
    code_runtime._write_web_state({"cookies": []})
    assert json.loads(state.read_text(encoding="utf-8")) == {"cookies": []}
    assert not temporary.exists()


def test_windows_default_config_requires_real_model_configuration(tmp_path, monkeypatch):
    import mac_agent
    runtime = tmp_path / "harness/runtime/mac_agent.py"
    configs = tmp_path / "harness/configs"
    configs.mkdir(parents=True)
    example = configs / "windows_example.json"
    example.write_text(json.dumps({"model_name": "YOUR_MODEL_NAME"}), encoding="utf-8")
    monkeypatch.setattr(mac_agent, "IS_WINDOWS", True)
    monkeypatch.setattr(mac_agent, "__file__", str(runtime))
    monkeypatch.delenv("MAC_AGENT_CONFIG", raising=False)
    monkeypatch.setattr(sys, "argv", ["mai-ui-windows-agent"])
    assert mac_agent._default_config_path(None) == str(example)
    with pytest.raises(SystemExit, match="Configure your model"):
        mac_agent.main()
    local = configs / "local.json"
    local.write_text("{}", encoding="utf-8")
    assert mac_agent._default_config_path(None) == str(local)


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows journal ACL")
def test_windows_journal_is_private_and_recovers_without_replay(tmp_path):
    from action_journal import ActionJournal
    from ctypes import wintypes

    path = tmp_path / "actions.jsonl"
    event = {"type": "action_journal", "journal_stage": "intent_recorded", "action_id": "a1", "sdk_session_id": "sdk",
             "operation": "action.click", "request": {"text": "private"}}
    journal = ActionJournal(path, "host")
    answer = journal.transition(event, {"session_id": "host"})
    assert answer["allowed"], answer
    journal.close()
    # Inspect the persisted ACL independently of the writer. st_mode cannot
    # represent Windows permissions, and PowerShell modules may be unavailable.
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    api.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    api.GetSecurityDescriptorControl.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD),
    )
    api.GetAce.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
    api.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    assert api.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None,
                                    ctypes.byref(dacl), None, ctypes.byref(descriptor)) == 0
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        assert api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
        assert control.value & 0x1000  # SE_DACL_PROTECTED: no inherited broad access
        assert ctypes.c_ushort.from_address(dacl.value + 4).value == 1  # ACL.AceCount
        ace = ctypes.c_void_p()
        assert api.GetAce(dacl, 0, ctypes.byref(ace))
        assert ctypes.c_ubyte.from_address(ace.value).value == 0  # ACCESS_ALLOWED_ACE
        assert api.EqualSid(owner, ace.value + 8)  # only the file owner
    finally:
        kernel.LocalFree(descriptor)
    reopened = ActionJournal(path, "host")
    try:
        assert not reopened.transition(event, {"session_id": "host"})["allowed"]
        assert reopened.recover()[0]["recovery_state"] == "not_dispatched"
    finally:
        reopened.close()
