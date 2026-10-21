"""真实伪终端中的 TUI 端到端：真实键盘字节 → prompt_toolkit RichTerminalFrontend → 宿主循环。

两层覆盖：
1. 纯 UI 输入：子进程只运行真实 RichTerminalFrontend（start/read/emit/stop），由测试经 PTY 键入；
2. 宿主链路：子进程运行真实 ``mac_agent.main()``（``--ui tui`` → run_interactive_agent → run_agent），
   CodeExecutor/code_runtime worker 为生产代码，worker 内 SDK 为 test_journal_agent_integration 的 fake。

替换边界（与 test_journal_agent_integration.Harness 相同层级）：
- 模型：plan_code/build_model_client 为离线 fake，输入记在子进程内存并镜像到 tmp 标记文件；
- 桌面：CuaDesktop/CuaRpcServer 为空 fake，不触碰 TCC/CUA/Browser/driver；
- 数据：HOME、GENERALIST_DATA_DIR、runs、memory、config 全部在 tmp；不读取 API key 或本地配置。
这不是桌面验证；终端是真实 PTY，模型与设备是 fake。
观察 tap 只包装 RichTerminalFrontend 实例的 _append/_consume/read/stop 与 renderer.render 写同步标记，不改变行为。
"""
from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from test_journal_agent_integration import _worker_source

pytestmark = pytest.mark.skipif(os.name != "posix", reason="需要 POSIX pty/termios")

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
# 每个终端进程从启动到退出的总预算；所有等待都是有界轮询，不做固定 sleep。
PROCESS_TIMEOUT = 20.0
_ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78]")
ENTER, CTRL_C, CTRL_D = b"\r", b"\x03", b"\x04"


def _child_env(tmp_path: Path) -> dict[str, str]:
    """去掉凭据/端点类变量，并把用户目录全部指向 tmp。"""
    dropped = {"MAC_AGENT_CONFIG", "CUA_DRIVER_SOCKET", "MAC_AGENT_HUB_EVENTS", "LLM_BASE_URL", "MODEL_NAME"}
    env = {key: value for key, value in os.environ.items()
           if key not in dropped and not key.endswith("API_KEY") and "PROXY" not in key.upper()}
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir(exist_ok=True)
    data.mkdir(exist_ok=True)
    paths = [str(Path(entry).absolute()) for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry]
    env.update(TERM="xterm-256color", HOME=str(home), GENERALIST_DATA_DIR=str(data),
               PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=os.pathsep.join([str(RUNTIME_ROOT), *paths]))
    return env


class PtyRun:
    """在 pty.openpty 上运行真实程序；以子进程写出的标记做有界同步。"""

    def __init__(self, tmp_path: Path, source: str, config: dict, *, size=(40, 140)) -> None:
        self.tmp_path = tmp_path
        self.marks_path = tmp_path / "marks.jsonl"
        script, config_path = tmp_path / "child.py", tmp_path / "child_config.json"
        script.write_text(source, encoding="utf-8")
        config_path.write_text(json.dumps({**config, "marks": str(self.marks_path),
                                           "runtime": str(RUNTIME_ROOT)}), encoding="utf-8")
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *size, 0, 0))
        self.process = subprocess.Popen(
            [sys.executable, str(script), str(config_path)], stdin=slave, stdout=slave, stderr=slave,
            env=_child_env(tmp_path), cwd=tmp_path, start_new_session=True,
        )
        os.close(slave)
        self.output = bytearray()
        self.cursor = 0
        self.deadline = time.monotonic() + PROCESS_TIMEOUT

    def _drain(self, timeout: float) -> None:
        ready, _, _ = select.select([self.master], [], [], timeout)
        if ready:
            try:
                self.output.extend(os.read(self.master, 65536))
            except OSError:
                pass

    def marks(self) -> list[dict]:
        if not self.marks_path.exists():
            return []
        text = self.marks_path.read_text(encoding="utf-8")
        lines = text.split("\n")
        # 最后一行可能尚未写完；只解析以换行结尾的完整记录。
        return [json.loads(line) for line in lines[:-1] if line]

    def screen_text(self) -> str:
        return _ANSI.sub(b"", bytes(self.output)).decode("utf-8", errors="replace")

    def fail(self, message: str):
        self.kill()
        tail = "\n".join(json.dumps(m, ensure_ascii=False)[:300] for m in self.marks()[-40:])
        raise AssertionError(f"{message}\n--- marks tail ---\n{tail}\n--- pty tail ---\n{self.screen_text()[-3000:]}")

    def wait(self, description: str, *predicates) -> list[dict]:
        """等待每个谓词都在游标之后各匹配一条标记；返回匹配项并把游标推进到最后一项之后。"""
        while True:
            marks = self.marks()
            found = []
            for predicate in predicates:
                index = next((i for i in range(self.cursor, len(marks)) if predicate(marks[i])), None)
                if index is None:
                    break
                found.append(index)
            if len(found) == len(predicates):
                self.cursor = max(found) + 1
                return [marks[i] for i in found]
            if self.process.poll() is not None:
                self.fail(f"等待 {description} 时终端进程已退出 code={self.process.returncode}")
            if time.monotonic() > self.deadline:
                self.fail(f"等待 {description} 超过 {PROCESS_TIMEOUT}s")
            self._drain(0.02)

    def wait_text(self, description: str, *needles: str) -> list[dict]:
        return self.wait(description, *(
            (lambda m, n=needle: m["m"] == "append" and n in m["text"]) for needle in needles))

    def type(self, text: str) -> None:
        """模拟人工输入：逐字节写入 PTY 主端，再单独按 Enter。"""
        for byte in text.encode("utf-8"):
            os.write(self.master, bytes([byte]))
        os.write(self.master, ENTER)

    def press(self, key: bytes) -> None:
        os.write(self.master, key)

    def finish(self) -> int:
        while self.process.poll() is None:
            if time.monotonic() > self.deadline:
                self.fail(f"终端进程未在 {PROCESS_TIMEOUT}s 内退出")
            self._drain(0.05)
        # 读尽退出前的剩余输出。
        for _ in range(20):
            ready, _, _ = select.select([self.master], [], [], 0.02)
            if not ready:
                break
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            self.output.extend(data)
        return self.process.returncode

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()

    def close(self) -> None:
        self.kill()
        (self.tmp_path / "pty_output.raw").write_bytes(bytes(self.output))
        (self.tmp_path / "pty_screen.txt").write_text(self.screen_text(), encoding="utf-8")
        evidence = os.environ.get("TUI_PTY_EVIDENCE_DIR")
        if evidence:
            target = Path(evidence) / self.tmp_path.name
            target.mkdir(parents=True, exist_ok=True)
            for name in ("marks.jsonl", "pty_output.raw", "pty_screen.txt"):
                if (self.tmp_path / name).exists():
                    shutil.copy2(self.tmp_path / name, target / name)
        os.close(self.master)


_MARK_PRELUDE = r'''
import asyncio, json, os, sys, types
from pathlib import Path
CFG = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sys.path.insert(0, CFG["runtime"])
_MARKS = open(CFG["marks"], "a", encoding="utf-8", buffering=1)

def mark(_m, /, **fields):
    _MARKS.write(json.dumps({"m": _m, **fields}, ensure_ascii=False, default=repr) + "\n")

def tap_frontend(ui):
    """只观察：原方法照常执行，之后写标记。"""
    append, consume, read, stop = ui._append, ui._consume, ui.read, ui.stop
    renderer = ui._application.renderer
    render = renderer.render
    last_rows = []

    def tapped_render(*args, **kwargs):
        # 保存最近一次真实渲染的屏幕单元；应用退出时 renderer 会清空 _last_screen。
        render(*args, **kwargs)
        screen = renderer._last_screen
        if screen is not None:
            size = ui._application.output.get_size()
            last_rows[:] = ["".join(screen.data_buffer[y][x].char for x in range(size.columns)).rstrip()
                            for y in range(size.rows)]

    def tapped_append(text):
        append(text)
        mark("append", text=text)

    def tapped_consume(event):
        consume(event)
        mark("event", kind=event.kind, step=event.step, phase=event.payload.get("phase"),
             stage=event.payload.get("stage"), action_id=event.payload.get("action_id"),
             active=ui.turn_active)

    async def tapped_read(prompt):
        mark("read", kind=prompt.kind)
        value = await read(prompt)
        mark("read_result", kind=prompt.kind,
             value=value if value is None or isinstance(value, str) else repr(value))
        return value

    async def tapped_stop():
        await stop()
        mark("screen", rows=list(last_rows))

    renderer.render = tapped_render
    ui._append, ui._consume, ui.read, ui.stop = tapped_append, tapped_consume, tapped_read, tapped_stop
    return ui
'''


UI_CHILD = _MARK_PRELUDE + r'''
from agent_events import AgentEvent, AgentPrompt
from memory_runtime import MemoryService
from terminal_tui import RichTerminalFrontend

async def until(predicate, label):
    for _ in range(1500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("child wait timeout: " + label)

async def main():
    ui = tap_frontend(RichTerminalFrontend())
    ui.memory_service = MemoryService(Path(CFG["memory"]))
    aborts = []
    def on_abort(force):
        aborts.append(force)
        mark("abort", force=force)
    ui.bind_abort(on_abort)
    ui.start()
    results = {}
    results["first"] = await ui.read(AgentPrompt(kind="task"))
    ui.emit(AgentEvent("phase", step=0, payload={"phase": "planning"}))
    await until(lambda: ui.turn_active, "turn active")
    mark("stage", name="turn_active")
    await until(lambda: aborts, "ctrl-c abort")
    await until(lambda: ui._application.is_running, "app still running")
    mark("stage", name="after_abort", running=ui._application.is_running, queued=list(ui.queued_messages()))
    ui.emit(AgentEvent("aborted", "stopped"))
    results["correction"] = await ui.read(AgentPrompt(kind="task"))
    ui.emit(AgentEvent("phase", step=1, payload={"phase": "planning"}))
    ui.emit(AgentEvent("answer", "CORRECTION-DONE", step=1))
    await until(lambda: not ui.turn_active, "turn finished")
    results["queue_send"] = await ui.read(AgentPrompt(kind="task"))
    results["fifo_second"] = await ui.read(AgentPrompt(kind="task"))
    ui.emit(AgentEvent("phase", step=2, payload={"phase": "planning"}))
    await until(lambda: ui.turn_active, "second turn active")
    mark("stage", name="final_turn_active")
    await until(lambda: not ui._application.is_running, "ctrl-d exit")
    await ui.stop()
    mark("summary", results=results, aborts=aborts, queued=list(ui.queued_messages()))

asyncio.run(main())
'''


HOST_CHILD = _MARK_PRELUDE + r'''
import code_executor, llm_entry, mac_agent, terminal_tui
from model_stream import CodeAction

# 与 test_journal_agent_integration.Harness 相同：宿主不加载原生 SDK，worker 内为 fake SDK。
sys.modules["mai_ui"] = types.SimpleNamespace(Runtime=object)

class Desktop:
    generation = 0
    permission_status = {"accessibility": True, "screen_recording": True}
    def __init__(self, **_kwargs): pass
    async def start(self): pass
    async def close(self): pass
    async def wait_for_idle(self): pass
    def invalidate_references(self): self.generation += 1

class RpcServer:
    def __init__(self, _desktop, _socket): self.calls = []
    async def start(self): pass
    async def close(self): pass

class Client:
    on_event = on_attempt = on_parse_event = on_compaction_event = None
    token_estimator = context_limit = None
    async def __aenter__(self): return self
    async def __aexit__(self, *_args): pass
    async def complete(self, *_args, **_kwargs):
        raise AssertionError("本测试不触发压缩")

class WorkerExecutor(code_executor.CodeExecutor):
    """生产 CodeExecutor；仅把 runtime_path 指向 fake SDK worker，并记录 worker pid。"""
    def __init__(self, **kwargs):
        super().__init__(**{**kwargs, "runtime_path": Path(CFG["worker"])})
    async def _start_worker(self):
        await super()._start_worker()
        mark("worker", pid=self._process.pid)

MODEL_LOG = []
PLANS = list(CFG["plans"])

def user_texts(messages):
    texts = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        parts = [content] if isinstance(content, str) else content if isinstance(content, list) else []
        for part in parts:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    return texts

async def plan(_client, messages, **_kwargs):
    MODEL_LOG.append(messages)
    mark("model", n=len(MODEL_LOG), user=user_texts(messages))
    if not PLANS:
        raise AssertionError("fake model: no scripted plan left")
    item = PLANS.pop(0)
    action = CodeAction(intent=item["intent"], code=item["code"])
    return action, action.as_response(), "", {}

mac_agent.CuaDesktop = Desktop
mac_agent.CuaRpcServer = RpcServer
mac_agent.CodeExecutor = WorkerExecutor
mac_agent.plan_code = plan
llm_entry.build_model_client = lambda *_args, **_kwargs: Client()
llm_entry.load_llm_config = lambda: {}
_real_build = terminal_tui.build_rich_frontend
terminal_tui.build_rich_frontend = lambda: tap_frontend(_real_build())

sys.argv = ["mac_agent.py", *CFG["argv"]]
code = mac_agent.main()
mark("exit", code=code, model_calls=len(MODEL_LOG), plans_left=len(PLANS))
raise SystemExit(code)
'''


def _text(needle: str):
    return lambda m: m["m"] == "append" and needle in m["text"]


def _task_read(m: dict) -> bool:
    return m["m"] == "read" and m["kind"] == "task"


def _appended(marks: list[dict]) -> str:
    return "".join(m["text"] for m in marks if m["m"] == "append")


def test_real_keyboard_ui_routes_local_commands_queue_interrupt_and_exit(tmp_path):
    memory = tmp_path / "memory"
    run = PtyRun(tmp_path, UI_CHILD, {"memory": str(memory)})
    try:
        run.wait("首个 task read", lambda m: m["m"] == "read" and m["kind"] == "task")
        # 空闲且有待处理 read 时，本地命令只在 TUI 执行，不完成 read。
        run.type("/status")
        run.wait_text("/status", "Session: (not started)", "Context: (no planning request yet)")
        run.type("/help")
        run.wait_text("/help", "Ctrl-C 中止当前轮 · Ctrl-D 退出")
        run.type("/memory list")
        run.wait_text("/memory list", "长期记忆（", str(memory))
        run.type("hello ui")
        run.wait("首条消息", lambda m: m["m"] == "read_result" and m["value"] == "hello ui")
        run.wait("活动轮", lambda m: m["m"] == "stage" and m["name"] == "turn_active")
        run.type("queued one")
        run.wait_text("排队 1", "You (queued): queued one")
        run.type("queued two")
        run.wait_text("排队 2", "You (queued): queued two")
        run.type("/queue")
        run.wait_text("/queue", "Queued messages:\n1. queued one\n2. queued two")
        run.press(CTRL_C)
        run.wait("Ctrl-C 只中止本轮", lambda m: m["m"] == "abort" and m["force"] is False,
                 lambda m: m["m"] == "stage" and m["name"] == "after_abort" and m["running"])
        run.wait("纠正 read", lambda m: m["m"] == "read" and m["kind"] == "task")
        run.type("fix it")
        run.wait("纠正送达", lambda m: m["m"] == "read_result" and m["value"] == "fix it")
        # 中止后队列暂停：正常完成纠正轮也不自动发送旧队列，需显式 /queue send。
        run.wait("暂停队列上的 read", lambda m: m["m"] == "read" and m["kind"] == "task")
        run.type("/queue")
        run.wait_text("队列仍保留", "Queued messages:\n1. queued one\n2. queued two")
        run.type("/queue send")
        run.wait("FIFO 第一条", lambda m: m["m"] == "read_result" and m["value"] == "queued one")
        run.wait("FIFO 第二条", lambda m: m["m"] == "read_result" and m["value"] == "queued two")
        run.wait("最后一轮活动", lambda m: m["m"] == "stage" and m["name"] == "final_turn_active")
        run.type("leftover")
        run.wait_text("排队 leftover", "You (queued): leftover")
        run.press(CTRL_D)
        run.wait_text("Ctrl-D 需确认", "仍有排队消息未发送；再按 Ctrl-D")
        run.press(CTRL_D)
        code = run.finish()
    finally:
        run.close()
    marks = run.marks()
    assert code == 0, run.screen_text()[-2000:]
    summary = next(m for m in marks if m["m"] == "summary")
    assert summary["results"] == {"first": "hello ui", "correction": "fix it",
                                  "queue_send": "queued one", "fifo_second": "queued two"}
    # 第一次 Ctrl-C 协作中止；Ctrl-D 确认退出时活动轮被强制中止。
    assert summary["aborts"] == [False, True]
    assert summary["queued"] == ["leftover"]
    reads = [m["value"] for m in marks if m["m"] == "read_result"]
    assert not any(isinstance(v, str) and v.startswith("/") for v in reads), reads
    screen = run.screen_text()
    assert "OpenGCUA" in screen and "MESSAGE" in screen


def _gated(gate: Path, then: str = "") -> str:
    # 模型代码在真实 worker 中有界轮询门控文件，使测试能在活动轮内键入；不是固定 sleep。
    return ("import asyncio, os\n"
            "for _ in range(1500):\n"
            f"    if os.path.exists({str(gate)!r}):\n"
            "        break\n"
            "    await asyncio.sleep(0.01)\n"
            "else:\n"
            "    raise RuntimeError('gate timeout')\n" + then)


RECEIPT_FACT = "调用已结束；技术回执不证明期望的界面变化或业务成功"
UNKNOWN_FACT = "派发结果未知，可能已有副作用"


def test_real_keyboard_drives_mac_agent_host_loop_with_fake_sdk_worker(tmp_path):
    gate_first, gate_never = tmp_path / "gate_first", tmp_path / "gate_never"
    worker = tmp_path / "fake_worker.py"
    worker.write_text(_worker_source(), encoding="utf-8")
    runs, memory, config = tmp_path / "runs", tmp_path / "memory", tmp_path / "empty_config.json"
    config.write_text("{}", encoding="utf-8")
    plans = [
        {"intent": "INTENT-ONE", "code": _gated(gate_first, "await device.act('act-ok', text='提交')")},
        {"intent": "INTENT-TWO", "code": "await device.act('act-fail', failure='TIMEOUT')"},
        {"intent": "INTENT-THREE", "code": "terminate(status='success', answer='DONE-FIRST')"},
        {"intent": "INTENT-LONG", "code": _gated(gate_never)},
        # Ctrl-C 取消活动 cell 时生产代码硬停 worker；纠正后首个 cell 不执行，只回报 session_reset。
        {"intent": "INTENT-FIX-UNRUN", "code": "terminate(status='success', answer='DONE-NEVER-REPLAYED')"},
        {"intent": "INTENT-FIX", "code": "terminate(status='success', answer='DONE-CORRECTION')"},
        {"intent": "INTENT-B", "code": "terminate(status='success', answer='DONE-B')"},
    ]
    argv = ["--ui", "tui", "--config", str(config), "--api-key", "offline-fake-key",
            "--model-name", "fake-offline-model", "--llm-base-url", "http://127.0.0.1:9/offline",
            "--countdown", "0", "--startup-observation", "none", "--disable-bash",
            "--runs-dir", str(runs), "--memory-dir", str(memory), "--code-timeout", "30"]
    run = PtyRun(tmp_path, HOST_CHILD, {"worker": str(worker), "plans": plans, "argv": argv})
    try:
        run.wait("会话首个 read", lambda m: m["m"] == "read" and m["kind"] == "task")
        run.type("FIRST submit form")
        run.wait("首轮模型调用并进入执行", lambda m: m["m"] == "model" and m["n"] == 1,
                 lambda m: m["m"] == "event" and m["kind"] == "phase" and m["phase"] == "execution")
        # 活动轮内：本地命令即时执行；普通消息进入 FIFO 队列。
        run.type("/status")
        run.wait_text("/status 含 context_status", "Phase: execution", "Context: ")
        run.type("/help")
        run.wait_text("/help", "Ctrl-C 中止当前轮 · Ctrl-D 退出")
        run.type("/memory list")
        run.wait_text("/memory list", "长期记忆（", str(memory))
        run.type("SECOND followup A")
        run.wait_text("排队 A", "You (queued): SECOND followup A")
        run.type("SECOND followup B")
        run.wait_text("排队 B", "You (queued): SECOND followup B")
        run.type("/queue")
        run.wait_text("/queue", "Queued messages:\n1. SECOND followup A\n2. SECOND followup B")
        gate_first.write_text("go", encoding="utf-8")
        run.wait("回执与未知事实到达 TUI",
                 lambda m: m["m"] == "event" and m["kind"] == "action_status"
                 and m["stage"] == "receipt_received" and m["action_id"] == "act-ok",
                 lambda m: m["m"] == "event" and m["kind"] == "action_status"
                 and m["stage"] == "outcome_unknown" and m["action_id"] == "act-fail")
        run.wait_text("首轮回答", "Agent: DONE-FIRST")
        # 队列 A 按 FIFO 作为下一任务进入模型，B 继续排队。
        run.wait("A 进入模型并执行", lambda m: m["m"] == "model" and m["n"] == 4,
                 lambda m: m["m"] == "event" and m["kind"] == "phase" and m["phase"] == "execution")
        run.press(CTRL_C)
        # 宿主可能先挂起下一次 read 再投递 aborted/answer 事件；各谓词独立匹配，不假定二者先后。
        run.wait("本轮中止并等待纠正", _text("正在中止当前轮"), _text("Turn aborted"), _task_read)
        run.type("THIRD correction")
        run.wait("纠正回答与下一 read", _text("Agent: DONE-CORRECTION"), _task_read)
        run.type("/queue")
        run.wait_text("中止后队列暂停保留 B", "Queued messages:\n1. SECOND followup B")
        run.type("/queue send")
        run.wait("B 回答与空闲 read", _text("Agent: DONE-B"), _task_read)
        run.press(CTRL_D)
        code = run.finish()
    finally:
        run.close()

    marks = run.marks()
    transcript = _appended(marks)
    assert code == 0, transcript[-3000:]
    exit_mark = next(m for m in marks if m["m"] == "exit")
    assert exit_mark == {"m": "exit", "code": 0, "model_calls": 7, "plans_left": 0}

    # 模型输入（子进程内存 log 的镜像）：只含普通消息，本地命令从未进入模型。
    calls = [m["user"] for m in marks if m["m"] == "model"]
    joined = ["\n".join(texts) for texts in calls]
    assert "FIRST submit form" in joined[0] and "SECOND" not in joined[0]
    assert RECEIPT_FACT in joined[1], joined[1][-2000:]
    assert UNKNOWN_FACT in joined[2], joined[2][-2000:]
    assert "SECOND followup A" in joined[3] and "SECOND followup B" not in joined[3]
    assert "THIRD correction" in joined[4] and "上一轮已被用户中止" in joined[4]
    # 中止硬停了 worker：纠正后的首个 cell 未执行，模型收到会话重置事实，terminate 不被采纳。
    assert "Python 会话已重置" in joined[5] and "本轮代码未执行" in joined[5], joined[5][-2000:]
    assert "SECOND followup B" in joined[6]
    for text in joined:
        for command in ("/status", "/help", "/queue", "/memory list"):
            assert command not in text, (command, text[-1500:])

    # TUI 显示：context_status、动作事实标签、回答与中止都来自宿主事件。
    statuses = [m for m in marks if m["m"] == "event" and m["kind"] == "context_status"]
    assert len(statuses) >= 6
    assert re.search(r"Context: [\d,]+ / 200,000 tokens", transcript)
    assert "已收到技术回执，业务结果未验证 · act-ok" in transcript
    assert "动作结果未知，请核实后继续 · act-fail" in transcript
    assert "You: FIRST submit form" in transcript and "You: THIRD correction" in transcript
    assert "DONE-NEVER-REPLAYED" not in transcript
    assert transcript.index("Agent: DONE-FIRST") < transcript.index("Turn aborted") \
        < transcript.index("Agent: DONE-CORRECTION") < transcript.index("Agent: DONE-B")
    screen = next(m for m in marks if m["m"] == "screen")["rows"]
    assert any("Agent: DONE-B" in row for row in screen), screen
    assert any("OpenGCUA" in row for row in screen)
    assert "OpenGCUA" in run.screen_text()

    # 轨迹与 journal 落在 tmp runs；动作仍未验证；一次中止被记录。
    run_dir = next(runs.glob("*/traj.json")).parent
    trajectory = json.loads((run_dir / "traj.json").read_text(encoding="utf-8"))
    assert [(t["task"], t["status"]) for t in trajectory["tasks"]] == [
        ("FIRST submit form", "success"), ("SECOND followup A", "success"), ("SECOND followup B", "success")]
    assert len(trajectory["aborts"]) == 1
    records = [json.loads(line) for line in (run_dir / "actions.jsonl").read_text().splitlines()]
    assert [(r["action_id"], r["journal_stage"]) for r in records][-1] == ("act-fail", "outcome_unknown")
    assert all(r["verification"] == "unverified" for r in records)

    # Ctrl-D 退出后 worker 进程组已回收。
    pids = [m["pid"] for m in marks if m["m"] == "worker"]
    # 首个 worker 在 Ctrl-C 时被硬停，重置后由新 worker 接续。
    assert len(set(pids)) == 2, pids
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    leftover = subprocess.run(["pgrep", "-f", str(worker)], capture_output=True, text=True)
    assert leftover.stdout.strip() == "", leftover.stdout
