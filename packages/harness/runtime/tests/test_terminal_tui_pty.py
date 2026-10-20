"""A real pseudo-terminal smoke test for persistent input during streaming."""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time


def run_in_pty(source: str, keystrokes: list[tuple[float, bytes]]) -> tuple[int, str]:
    master, slave = pty.openpty()
    env = dict(os.environ, TERM="xterm-256color")
    process = subprocess.Popen(
        [sys.executable, "-c", source], stdin=slave, stdout=slave, stderr=slave,
        env=env, start_new_session=True,
    )
    os.close(slave)
    output = bytearray()
    try:
        for delay, keys in keystrokes:
            time.sleep(delay)
            os.write(master, keys)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process.poll() is None:
            ready, _, _ = select.select([master], [], [], .1)
            if ready:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
        process.wait(timeout=3)
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
