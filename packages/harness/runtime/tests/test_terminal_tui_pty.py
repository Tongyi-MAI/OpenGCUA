"""A real pseudo-terminal smoke test for persistent input during streaming."""

from __future__ import annotations

import os
import pty
import select
import fcntl
import signal
import struct
import subprocess
import sys
import time
import termios
import pytest


def run_in_pty(source: str, keystrokes: list[tuple[float, bytes]], *,
               terminal_size: tuple[int, int] | None = None,
               resize_after_first: tuple[int, int] | None = None) -> tuple[int, str]:
    master, slave = pty.openpty()
    if terminal_size is not None:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *terminal_size, 0, 0))
    env = dict(os.environ, TERM="xterm-256color")
    process = subprocess.Popen(
        [sys.executable, "-c", source], stdin=slave, stdout=slave, stderr=slave,
        env=env, start_new_session=True,
    )
    os.close(slave)
    output = bytearray()
    try:
        def drain_for(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and process.poll() is None:
                ready, _, _ = select.select([master], [], [], max(0, min(.05, deadline - time.monotonic())))
                if ready:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        break

        for index, (delay, keys) in enumerate(keystrokes):
            drain_for(delay)
            os.write(master, keys)
            if index == 0 and resize_after_first is not None:
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", *resize_after_first, 0, 0))
                process.send_signal(signal.SIGWINCH)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process.poll() is None:
            ready, _, _ = select.select([master], [], [], .1)
            if ready:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(output.decode("utf-8", errors="replace")) from exc
        return process.returncode, output.decode("utf-8", errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def test_persistent_input_survives_streamed_output_in_pty():
    source = '''import asyncio
from agent_events import AgentEvent, AgentPrompt
from terminal_tui import RichTerminalFrontend
async def main():
    ui = RichTerminalFrontend()
    ui.start()
    pending = asyncio.create_task(ui.read(AgentPrompt(kind="task")))
    await asyncio.sleep(.3)
    ui.emit(AgentEvent("intent_delta", "INTENT_FIRST\\nINTENT_SECOND"))
    ui.emit(AgentEvent("intent_end"))
    await asyncio.sleep(.3)
    print("RESULT=" + repr(await pending), flush=True)
    await ui.stop()
asyncio.run(main())
'''
    code, decoded = run_in_pty(source, [(.25, b"draft"), (.7, b"-survives\r")])
    assert code == 0, decoded
    assert "RESULT='draft-survives'" in decoded
    assert "INTENT_FIRST" in decoded and "INTENT_SECOND" in decoded


@pytest.mark.parametrize("size", [(24, 80), (8, 30)])
def test_visual_shell_keeps_brand_and_composer_visible_at_terminal_sizes(size):
    source = '''import asyncio
from terminal_tui import RichTerminalFrontend
async def main():
    ui = RichTerminalFrontend()
    ui.start()
    await asyncio.sleep(.5)
    await ui.stop()
    print("EXITED", flush=True)
asyncio.run(main())
'''
    code, output = run_in_pty(source, [], terminal_size=size)
    assert code == 0, output
    assert "OpenGCUA" in output
    assert "MESSAGE" in output
    assert "EXITED" in output


def test_short_conversation_has_no_scrollbar_arrows():
    source = '''import asyncio
from terminal_tui import RichTerminalFrontend
async def main():
    ui = RichTerminalFrontend()
    ui.start()
    await asyncio.sleep(.3)
    screen = ui._application.renderer._last_screen
    arrows = [screen.data_buffer[y][79].char for y in range(1, 18)
              if screen.data_buffer[y][79].char in "^v"]
    await ui.stop()
    print("ARROWS=" + repr(arrows), flush=True)
asyncio.run(main())
'''
    code, output = run_in_pty(source, [], terminal_size=(24, 80))
    assert code == 0, output
    assert "ARROWS=[]" in output


def test_slash_menu_tab_completes_status_without_sending_it_to_agent():
    source = '''import asyncio
from agent_events import AgentPrompt
from terminal_tui import RichTerminalFrontend
async def main():
    ui = RichTerminalFrontend()
    ui.start()
    pending = asyncio.create_task(ui.read(AgentPrompt(kind="task")))
    await asyncio.sleep(1.0)
    assert not pending.done(), "slash command was sent as an agent task"
    ui._input.text = "real task"
    ui._submit_enter()
    result = await pending
    print("STATUS_SHOWN=" + repr("Session:" in ui.transcript), flush=True)
    await ui.stop()
    print("TASK=" + repr(result), flush=True)
asyncio.run(main())
'''
    code, output = run_in_pty(source, [(.3, b"/"), (.3, b"sta\t\r")])
    assert code == 0, output
    assert "查看当前会话状态" in output
    assert "STATUS_SHOWN=True" in output
    assert "TASK='real task'" in output


def test_slash_menu_arrows_choose_new_and_wait_for_initial_message():
    source = '''import asyncio
from agent_events import AgentPrompt
from terminal_tui import RichTerminalFrontend
async def main():
    ui = RichTerminalFrontend()
    ui.start()
    result = await ui.read(AgentPrompt(kind="task"))
    await ui.stop()
    print("TASK=" + repr(result), flush=True)
asyncio.run(main())
'''
    code, output = run_in_pty(
        source, [(.3, b"/"), (.3, b"\x1b[B\x1b[B\r"), (.3, b"find a file\r")],
    )
    assert code == 0, output
    assert "TASK='find a file'" in output


def test_exit_during_active_turn_stops_future_chat_read_in_pty():
    source = '''import asyncio
from agent_events import AgentEvent, AgentPrompt
from terminal_tui import RichTerminalFrontend
async def main():
    ui = RichTerminalFrontend()
    ui.start()
    aborts = []
    ui.bind_abort(aborts.append)
    ui.emit(AgentEvent("phase", step=1, payload={"phase":"execution"}))
    await asyncio.sleep(.7)
    message = await ui.read(AgentPrompt(kind="task"))
    await ui.stop()
    print("EXIT=" + repr(message) + " ABORTS=" + repr(aborts), flush=True)
asyncio.run(main())
'''
    code, output = run_in_pty(source, [(.3, b"/exit\r")])
    assert code == 0, output
    assert "EXIT=None ABORTS=[True]" in output


def test_slash_argument_tab_completes_local_toggle_in_pty():
    source = '''import asyncio
from agent_events import AgentPrompt
from terminal_tui import RichTerminalFrontend
async def main():
    ui = RichTerminalFrontend()
    ui.start()
    pending = asyncio.create_task(ui.read(AgentPrompt(kind="task")))
    await asyncio.sleep(.9)
    assert not pending.done(), "toggle was sent to the agent"
    assert "code: off for future events" in ui.transcript
    ui._input.text = "next task"
    ui._submit_enter()
    result = await pending
    await ui.stop()
    print("TASK=" + repr(result), flush=True)
asyncio.run(main())
'''
    code, output = run_in_pty(source, [(.3, b"/code of\t\r")])
    assert code == 0, output
    assert "TASK='next task'" in output


def test_small_terminal_resize_preserves_input():
    source = '''import asyncio
from agent_events import AgentEvent, AgentPrompt
from terminal_tui import RichTerminalFrontend
async def main():
    ui = RichTerminalFrontend()
    ui.start()
    pending = asyncio.create_task(ui.read(AgentPrompt(kind="task")))
    await asyncio.sleep(.35)
    ui.emit(AgentEvent("intent_delta", "a long message that wraps in a small terminal"))
    result = await pending
    await ui.stop()
    print("RESIZE_RESULT=" + repr(result), flush=True)
asyncio.run(main())
'''
    code, output = run_in_pty(
        source, [(.3, b"before"), (.4, b"-after\r")],
        terminal_size=(8, 30), resize_after_first=(16, 60),
    )
    assert code == 0, output
    assert "RESIZE_RESULT='before-after'" in output


def test_choice_keys_in_pty_require_explicit_allow_selection():
    source = '''import asyncio
from agent_events import ForegroundApprovalPrompt
from terminal_tui import RichTerminalFrontend
async def main():
    ui = RichTerminalFrontend()
    ui.start()
    prompt = ForegroundApprovalPrompt("r1", "Foreground control until task end", 0, "app:a", "window:7")
    answer = await ui.choose_approval(prompt)
    await ui.stop()
    print("CHOICE=" + answer, flush=True)
asyncio.run(main())
'''
    code, output = run_in_pty(source, [(.3, b"\r")])
    assert code == 0, output
    assert "CHOICE=reject" in output
    code, output = run_in_pty(source, [(.3, b"\x1b[C\r")])
    assert code == 0, output
    assert "CHOICE=allow" in output


def test_running_state_advances_on_screen_without_new_agent_events():
    source = '''import asyncio
from agent_events import AgentEvent
from terminal_tui import RichTerminalFrontend
async def main():
    ui = RichTerminalFrontend()
    ui.start()
    ui.emit(AgentEvent("phase", step=4, payload={"phase":"planning"}))
    ui.emit(AgentEvent("model_status", "正在等待 API 响应", step=4, payload={"stage":"waiting"}))
    await asyncio.sleep(1.4)
    screen = ui._application.renderer._last_screen
    rows = ["".join(screen.data_buffer[y][x].char for x in range(80)).strip()
            for y in range(24)]
    await ui.stop()
    print("SCREEN=" + repr(rows), flush=True)
asyncio.run(main())
'''
    code, output = run_in_pty(source, [], terminal_size=(24, 80))
    assert code == 0, output
    assert "RUNNING" in output
    assert "00:01" in output
    assert "waiting for API" in output


def test_permission_choices_stay_in_composer_on_small_terminal():
    source = '''import asyncio
from agent_events import ForegroundApprovalPrompt
from terminal_tui import RichTerminalFrontend
async def main():
    ui = RichTerminalFrontend()
    ui.start()
    pending = asyncio.create_task(ui.choose_approval(ForegroundApprovalPrompt(
        "r1", "Agent wants foreground control", 0, "app:a", "window:7")))
    await asyncio.sleep(.4)
    screen = ui._application.renderer._last_screen
    rows = ["".join(screen.data_buffer[y][x].char for x in range(30)).strip()
            for y in range(8)]
    print("SCREEN=" + repr(rows), flush=True)
    ui._submit_enter()
    print("CHOICE=" + await pending, flush=True)
    await ui.stop()
asyncio.run(main())
'''
    code, output = run_in_pty(source, [], terminal_size=(8, 30))
    assert code == 0, output
    screen = output.split("SCREEN=", 1)[1]
    assert "前台键鼠" in screen
    assert "拒绝" in screen and "允许" in screen
    assert "CHOICE=reject" in output


def test_ctrl_c_twice_interrupts_turn_and_accepts_same_task_correction_in_pty():
    source = '''import asyncio
from agent_events import AgentEvent, AgentPrompt
from terminal_tui import RichTerminalFrontend
async def main():
    ui = RichTerminalFrontend()
    ui.start()
    calls = []
    ui.bind_abort(calls.append)
    ui.emit(AgentEvent("phase", step=1, payload={"phase":"execution"}))
    await asyncio.sleep(.7)
    ui.emit(AgentEvent("aborted", "stopped"))
    correction = await ui.read(AgentPrompt(kind="task"))
    await ui.stop()
    print("ABORTS=" + repr(calls) + " CORRECTION=" + repr(correction), flush=True)
asyncio.run(main())
'''
    code, output = run_in_pty(source, [(.3, b"\x03"), (.1, b"\x03"), (1.0, b"corrected\r")])
    assert code == 0, output
    assert "ABORTS=[False, True] CORRECTION='corrected'" in output


@pytest.mark.parametrize("mode,expected", [
    ("planning", "ABORTS=[False]"),
    ("question", "ANSWER=None ABORTS=[False]"),
    ("approval", "ANSWER='cancel' ABORTS=[False]"),
])
def test_ctrl_c_cancels_each_waiting_phase_in_pty(mode, expected):
    source = f'''import asyncio
from agent_events import AgentEvent, AgentPrompt, ForegroundApprovalPrompt
from terminal_tui import RichTerminalFrontend
async def main():
    ui = RichTerminalFrontend()
    ui.start()
    calls = []
    ui.bind_abort(calls.append)
    ui.emit(AgentEvent("phase", step=1, payload={{"phase":"planning"}}))
    if {mode!r} == "planning":
        await asyncio.sleep(.5)
        print("ABORTS=" + repr(calls), flush=True)
    elif {mode!r} == "question":
        answer = await ui.read(AgentPrompt(kind="question", text="What next?"))
        print("ANSWER=" + repr(answer) + " ABORTS=" + repr(calls), flush=True)
    else:
        prompt = ForegroundApprovalPrompt("r1", "Take foreground control?", 1, "app:a", "window:7")
        answer = await ui.choose_approval(prompt)
        print("ANSWER=" + repr(answer) + " ABORTS=" + repr(calls), flush=True)
    await ui.stop()
asyncio.run(main())
'''
    code, output = run_in_pty(source, [(.3, b"\x03")])
    assert code == 0, output
    assert expected in output
