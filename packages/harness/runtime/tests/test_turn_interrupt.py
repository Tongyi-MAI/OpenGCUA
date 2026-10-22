from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal

import pytest

import mac_agent
from code_executor import CodeExecutor


def test_with_abort_cancels_planning_and_returns_to_caller():
    async def scenario():
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def planning():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        turn = asyncio.create_task(mac_agent.with_abort(planning()))
        await entered.wait()
        mac_agent._abort_event().set()
        with pytest.raises(mac_agent.StepAborted):
            await asyncio.wait_for(turn, 1)
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_with_abort_forces_cancel_resistant_operation_without_waiting_forever():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        force_called = asyncio.Event()

        async def stubborn():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        async def force_stop():
            force_called.set()

        turn = asyncio.create_task(mac_agent.with_abort(stubborn(), force_stop=force_stop))
        await entered.wait()
        mac_agent._abort_event().set()
        try:
            with pytest.raises(mac_agent.StepAborted):
                await asyncio.wait_for(turn, 3)
            assert force_called.is_set()
        finally:
            release.set()
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_interrupt_active_worker_kills_its_process_group_before_followup(tmp_path):
    async def scenario():
        worker = tmp_path / "worker.py"
        worker.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(Path(__file__).parents[1])!r})\n"
            "import code_runtime as rt\n"
            "rt._bootstrap = lambda loop: {}\n"
            "raise SystemExit(rt.main(sys.argv))\n",
            encoding="utf-8",
        )
        executor = CodeExecutor(
            runtime_path=worker, work_dir=tmp_path, rpc_socket=tmp_path / "unused.sock",
            timeout=60, rpc_timeout=3, coordinate_range=1000,
            pyautogui_pause=0, enable_bash=False, ui_api="legacy-cua",
        )
        try:
            running = asyncio.create_task(executor.run("import time\ntime.sleep(60)", []))
            for _ in range(100):
                if executor._process is not None and executor._active_run is not None:
                    break
                await asyncio.sleep(0.01)
            assert executor._process is not None
            old_pid = executor._process.pid
            executor.interrupt_active()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(running, 3)
            with pytest.raises(ProcessLookupError):
                os.kill(old_pid, 0)
        finally:
            await executor.close()

    asyncio.run(scenario())


def test_closing_the_executor_lets_the_worker_run_its_shutdown_cleanup(tmp_path):
    """正常关闭不硬杀：worker 的收尾（关闭专用浏览器）必须有机会运行。"""
    marker = tmp_path / "cleaned"

    async def scenario():
        worker = tmp_path / "worker.py"
        worker.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(Path(__file__).parents[1])!r})\n"
            "import code_runtime as rt\n"
            "rt._bootstrap = lambda loop: {}\n"
            "async def cleanup(*, close_runtime):\n"
            "    import asyncio\n"
            "    await asyncio.sleep(0.1)\n"
            f"    open({str(marker)!r}, 'w').write(str(close_runtime))\n"
            "rt._close_sdk_session = cleanup\n"
            "raise SystemExit(rt.main(sys.argv))\n",
            encoding="utf-8",
        )
        executor = CodeExecutor(
            runtime_path=worker, work_dir=tmp_path, rpc_socket=tmp_path / "unused.sock",
            timeout=60, rpc_timeout=3, coordinate_range=1000,
            pyautogui_pause=0, enable_bash=False, ui_api="legacy-cua",
        )
        result = await executor.run("import sys\nprint(sys.dont_write_bytecode)", [])
        assert result.stdout.strip() == "True", "worker 不得向已签名 App 写入 pyc"
        pid = executor._process.pid
        await executor.close()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(scenario())
    assert marker.read_text() == "True"


def test_executor_close_has_a_deadline_when_shutdown_cleanup_hangs(tmp_path, monkeypatch):
    import code_executor
    monkeypatch.setattr(code_executor, "_GRACEFUL_STOP_SECONDS", 0.2)

    async def scenario():
        worker = tmp_path / "worker.py"
        worker.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(Path(__file__).parents[1])!r})\n"
            "import code_runtime as rt\n"
            "rt._bootstrap = lambda loop: {}\n"
            "async def cleanup(*, close_runtime):\n"
            "    import asyncio\n"
            "    await asyncio.sleep(60)\n"
            "rt._close_sdk_session = cleanup\n"
            "raise SystemExit(rt.main(sys.argv))\n",
            encoding="utf-8",
        )
        executor = CodeExecutor(
            runtime_path=worker, work_dir=tmp_path, rpc_socket=tmp_path / "unused.sock",
            timeout=60, rpc_timeout=3, coordinate_range=1000,
            pyautogui_pause=0, enable_bash=False, ui_api="legacy-cua",
        )
        await executor.run("x = 1", [])
        pid = executor._process.pid
        await asyncio.wait_for(executor.close(), 3)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(scenario())
