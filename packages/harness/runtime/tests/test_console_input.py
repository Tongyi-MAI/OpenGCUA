"""Console input regressions with no model requests or desktop interaction."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys

import mac_agent
import prompt_toolkit
import pytest
from agent_events import AgentPrompt, PlainFrontend
from model_stream import AgentError
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_memory_agent import install_fakes


def test_noninteractive_retry_does_not_read_input():
    async def unavailable(_prompt):
        raise NotImplementedError("Windows Proactor stdin reader")

    output = io.StringIO()
    frontend = PlainFrontend(unavailable, interactive=False, stdout=output)
    assert asyncio.run(frontend.read(AgentPrompt(kind="retry", text="Retry?"))) is None
    assert output.getvalue() == ""


def test_windows_console_input_reads_retry_without_console_descriptors(monkeypatch):
    monkeypatch.setattr(mac_agent, "IS_WINDOWS", True)
    monkeypatch.setattr(mac_agent, "console_is_interactive", lambda _stream: True)
    with create_pipe_input() as pipe:
        session = prompt_toolkit.PromptSession(input=pipe, output=DummyOutput())
        monkeypatch.setattr(prompt_toolkit, "PromptSession", lambda: session)
        pipe.send_text("r\n")
        assert asyncio.run(mac_agent.console_input("Retry? ")) == "r"


def test_cancelled_windows_console_input_leaves_no_blocked_reader(monkeypatch):
    monkeypatch.setattr(mac_agent, "IS_WINDOWS", True)
    monkeypatch.setattr(mac_agent, "console_is_interactive", lambda _stream: True)

    def blocking_input(*_args):
        pytest.fail("interactive Windows input must not use a blocking input thread")

    monkeypatch.setattr("builtins.input", blocking_input)
    with create_pipe_input() as pipe:
        session = prompt_toolkit.PromptSession(input=pipe, output=DummyOutput())
        monkeypatch.setattr(prompt_toolkit, "PromptSession", lambda: session)

        async def scenario():
            reading = asyncio.create_task(mac_agent.console_input("Retry? "))
            for _ in range(100):
                if session.app.is_running:
                    break
                await asyncio.sleep(0.01)
            assert session.app.is_running
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(reading, 1)
            assert not session.app.is_running

        # Runner shutdown would also hang if cancellation leaked an input thread.
        asyncio.run(scenario())


def run_model_failure(tmp_path, monkeypatch, *, frontend=None):
    calls = []
    executors = install_fakes(monkeypatch, [])

    async def fail(_client, _messages, **_kwargs):
        calls.append(1)
        raise AgentError("insufficient_quota: test account has no available quota")

    monkeypatch.setattr(mac_agent, "plan_code", fail)
    args = mac_agent.build_parser().parse_args(
        [
            "Read a sample table",
            "--allow-empty-api-key",
            "--countdown",
            "0",
            "--startup-observation",
            "none",
            "--disable-multi-task",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--memory-dir",
            str(tmp_path / "memory"),
        ]
    )
    assert asyncio.run(mac_agent.run_agent(args, frontend=frontend)) == 1
    assert calls == [1]
    assert all(executor.calls == 0 for executor in executors)
    trajectory = json.loads(next((tmp_path / "runs").glob("*/traj.json")).read_text("utf-8"))
    assert trajectory["status"] == "error"
    assert trajectory["answer"] == (
        "AgentError: insufficient_quota: test account has no available quota"
    )
    assert trajectory["finished_at"]
    assert len(trajectory["steps"]) == 1
    assert "NotImplementedError" not in json.dumps(trajectory)


@pytest.mark.skipif(not mac_agent.IS_WINDOWS, reason="Windows NUL console detection")
def test_windows_devnull_model_error_preserves_trajectory_without_retry(
    tmp_path, monkeypatch, capsys
):
    async def unsupported(_prompt):
        pytest.fail("NUL must not trigger interactive retry input")

    monkeypatch.setattr(mac_agent, "console_input", unsupported)
    with open(os.devnull, encoding="utf-8") as stream:
        monkeypatch.setattr(sys, "stdin", stream)
        # Windows CRT considers NUL a character device despite having no console.
        assert stream.isatty()
        assert not mac_agent.console_is_interactive(stream)
        run_model_failure(tmp_path, monkeypatch)
    output = capsys.readouterr()
    assert "insufficient_quota" in output.err
    assert "[r]etry" not in output.out
    assert "Trajectory saved to:" in output.out


def test_unavailable_retry_input_preserves_original_model_error(tmp_path, monkeypatch):
    calls = []

    async def unsupported(_prompt):
        calls.append(1)
        raise NotImplementedError("Windows Proactor stdin reader")

    frontend = PlainFrontend(
        unsupported, interactive=True, stdout=io.StringIO(), stderr=io.StringIO()
    )
    run_model_failure(tmp_path, monkeypatch, frontend=frontend)
    assert calls == [1]
