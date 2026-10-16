"""OpenGCUA's private NDJSON pipe. No listener, web server, or shell launcher.

One process owns one task at a time. Commands and events are fenced by task_id;
question replies also carry question_id. stdin EOF cancels and drains the worker.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import uuid
from urllib.parse import urlsplit

PROTOCOL = 1

# These settings belong to the native host's process/task lifecycle, not a file.
_HOST_ARGUMENTS = frozenset({"config", "task", "resume", "ui", "ui_api", "driver_mode",
                            "driver_socket", "session_id", "runs_dir", "multi_task", "countdown"})


def task_arguments(config: dict, runs_dir: Path):
    """Use the harness parser's vocabulary, preserving native task ownership."""
    import mac_agent
    config = dict(config)
    for old, current in (("endpoint", "llm_base_url"), ("model", "model_name"),
                         ("allow_empty_key", "allow_empty_api_key")):
        if old in config and current not in config:
            config[current] = config[old]
    parser = mac_agent.build_parser()
    args = parser.parse_args([])
    args.startup_observation = "none"
    args.max_steps = 100
    args.api_retries = 1
    args.api_timeout = 180
    for action in parser._actions:
        name = action.dest
        if name in _HOST_ARGUMENTS or name not in config or config[name] is None:
            continue
        value = config[name]
        expected = action.type or type(getattr(args, name, None))
        if expected is bool and type(value) is not bool:
            raise ValueError(f"配置项 {name} 必须为布尔值。")
        if expected is int and type(value) is not int:
            raise ValueError(f"配置项 {name} 必须为整数。")
        if expected is float and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError(f"配置项 {name} 必须为有限数值。")
        if expected is str and not isinstance(value, str):
            raise ValueError(f"配置项 {name} 必须为字符串。")
        if action.choices is not None and value not in action.choices:
            raise ValueError(f"配置项 {name} 的选项无效。")
        setattr(args, name, value)
    args.llm_base_url = validate_endpoint(str(config.get("llm_base_url") or ""))
    args.model_name = str(config.get("model_name") or "").strip()
    if not args.model_name:
        raise ValueError("请填写模型名称。")
    if args.background_mode and args.demo_mode:
        raise ValueError("后台模式与演示模式不能同时开启。")
    args.multi_task = False
    args.countdown = 0
    args.ui = "plain"
    args.runs_dir = runs_dir
    return args


def validate_endpoint(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query
            or parsed.fragment or (parsed.scheme != "https" and not (local and parsed.scheme == "http"))):
        raise ValueError("模型地址必须使用 HTTPS；仅本机地址允许 HTTP。")
    return value


def require_mac_permissions(report: dict) -> None:
    permissions = report.get("permissions") or {}
    if (permissions.get("accessibility") is not True
            or permissions.get("screen_recording") is not True):
        raise RuntimeError("OpenGCUA 尚未获得辅助功能和屏幕录制权限。请在应用设置中完成授权；尚未发送模型请求。")


class TaskControl:
    def __init__(self, task_id, send):
        self.task_id, self.send = task_id, send
        self.resume_event = asyncio.Event()
        self.resume_event.set()
        self.question_id = None
        self.answer = None
        self.run_dir = None
        self.paused = False
        self.messages = []
        self.message_states = {}

    def message(self, message_id, text):
        if not isinstance(message_id, str) or not message_id or len(message_id) > 100:
            raise ValueError("消息标识无效。")
        if message_id in self.message_states:
            self.emit("message", message_id=message_id, state=self.message_states[message_id])
            return
        if self.answer is not None:
            raise ValueError("任务正在提问，请使用当前问题的回复入口。")
        if not text.strip() or len(text) > 8000 or len(self.messages) >= 20:
            raise ValueError("消息为空、过长，或待处理消息过多。")
        self.messages.append({"id": message_id, "text": text.strip()})
        self.message_states[message_id] = "queued"
        self.emit("message", message_id=message_id, state="queued")

    def take_messages(self):
        messages, self.messages = self.messages, []
        for message in messages:
            self.message_states[message["id"]] = "applied"
            self.emit("message", message_id=message["id"], state="applied")
        return messages

    def emit(self, kind, **fields):
        if kind == "trajectory":
            self.run_dir = Path(fields["path"])
        self.send({"event": kind, "task_id": self.task_id, **fields})

    def pause(self):
        self.resume_event.clear()
        self.emit("state", state="pausing")

    def resume(self):
        self.resume_event.set()
        # A question cannot be dismissed by Resume.
        self.emit("state", state="waiting" if self.answer is not None else "running")

    async def boundary(self):
        if not self.resume_event.is_set():
            self.paused = True
            self.emit("state", state="paused")
            try:
                await self.resume_event.wait()
            finally:
                self.paused = False
            self.emit("state", state="running")

    async def ask(self, question):
        self.question_id = uuid.uuid4().hex
        self.answer = asyncio.get_running_loop().create_future()
        self.emit("question", question_id=self.question_id, text=question)
        try:
            response = await self.answer
        finally:
            self.answer = None
            self.question_id = None
        self.emit("state", state="running")
        await self.boundary()
        return response

    def reply(self, question_id, text):
        if question_id != self.question_id or self.answer is None or self.answer.done():
            raise ValueError("这个问题已结束，请查看当前任务。")
        if not text.strip():
            raise ValueError("请填写回复。")
        self.answer.set_result(text.strip())
        self.emit("reply", question_id=question_id)


class NativeHost:
    def __init__(self, send, runs_dir: Path, runner=None):
        self.send, self.runs_dir, self.runner = send, runs_dir, runner
        self.task = None
        self.control = None
        self.key = ""
        self.driver = None
        self.socket_dir = None

    async def start_driver(self):
        app = os.environ.get("GENERALIST_DRIVER_APP")
        if not app or self.runner is not None:
            return
        if self.driver is not None and self.driver.returncode is None:
            if Path(os.environ.get("GENERALIST_DRIVER_SOCKET", "")).is_socket():
                return
            await self.stop_driver()
        if self.socket_dir is not None:
            self.socket_dir.cleanup()
        self.socket_dir = tempfile.TemporaryDirectory(prefix="generalist-")
        socket = str(Path(self.socket_dir.name) / "driver.sock")
        os.environ["GENERALIST_DRIVER_SOCKET"] = socket
        self.driver = await asyncio.create_subprocess_exec(
            str(Path(app) / "Contents/MacOS/cua-driver"), "serve", "--embedded",
            "--socket", socket, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        for _ in range(100):
            if Path(socket).is_socket():
                return
            if self.driver.returncode is not None:
                raise RuntimeError("Mac 控制组件启动失败，请检查系统权限后重试。")
            await asyncio.sleep(0.1)
        await self.stop_driver()
        raise RuntimeError("Mac 控制组件未就绪，请检查系统权限后重试。")

    def public_error(self, exc):
        message = str(exc)
        if self.key:
            message = message.replace(self.key, "[REDACTED]")
        return message[:2000]

    async def preflight(self):
        app = os.environ.get("GENERALIST_DRIVER_APP")
        if not app or self.runner is not None:
            return
        from mai_ui import _native
        report = await asyncio.wait_for(_native.macos_admin(
            "doctor", False, None, app, os.environ["GENERALIST_DRIVER_SOCKET"]
        ), 30)
        require_mac_permissions(json.loads(report))

    async def run(self, command, control):
        try:
            import mac_agent
            config = command.get("config") or {}
            if not isinstance(config, dict):
                raise ValueError("任务配置必须是 JSON 对象。")
            if config.get("api_key") is not None and not isinstance(config["api_key"], str):
                raise ValueError("API Key 必须为字符串。")
            self.key = str(config.get("api_key") or "").strip()
            args = task_arguments(config, self.runs_dir)
            if not self.key and not args.allow_empty_api_key:
                raise ValueError("请先在设置中保存此服务的 API Key。")
            args.task = str(command.get("text") or "").strip()
            if not args.task or len(args.task) > 32000:
                raise ValueError("任务不能为空，且不能超过 32,000 字符。")
            args.api_key = self.key or None
            args.allow_empty_api_key = not self.key
            await self.start_driver()
            await self.preflight()
            control.emit("state", state="running")
            await (self.runner or mac_agent.run_agent)(args, host_control=control)
            # The trajectory is the authoritative terminal result, not process exit 0.
            data = json.loads((control.run_dir / "traj.json").read_text()) if control.run_dir else {}
            status = data.get("status", "error")
            control.emit("finished", state="completed" if status == "success" else "failed",
                         text=self.public_error(data.get("final_answer") or data.get("answer")
                                                or (data.get("tasks") or [{}])[-1].get("answer")
                                                or f"任务结束：{status}"),
                         path=str(control.run_dir or ""))
        except asyncio.CancelledError:
            control.emit("finished", state="stopped", text="任务已停止。已发生的操作不会撤销。",
                         path=str(control.run_dir or ""))
        except (Exception, SystemExit) as exc:
            control.emit("finished", state="failed", text=self.public_error(exc),
                         path=str(control.run_dir or ""))
        finally:
            self.key = ""

    async def command(self, command):
        kind = command.get("command")
        if kind == "start":
            if self.task is not None and not self.task.done():
                raise ValueError("已有任务正在运行。")
            task_id = str(command.get("task_id") or "")
            if not task_id or len(task_id) > 100:
                raise ValueError("任务标识无效。")
            self.control = TaskControl(task_id, self.send)
            self.task = asyncio.create_task(self.run(command, self.control))
            control = self.control
            # Cancellation may precede the coroutine's first instruction.
            self.task.add_done_callback(lambda task: control.emit(
                "finished", state="stopped", text="任务已停止。", path=""
            ) if task.cancelled() else None)
        elif kind in {"pause", "resume", "reply", "stop", "message"}:
            if (not self.control or command.get("task_id") != self.control.task_id
                    or not self.task or self.task.done()):
                raise ValueError("任务已结束或任务标识已过期。")
            if kind == "pause":
                if self.control.answer is not None:
                    raise ValueError("任务正在等待你的回复。")
                self.control.pause()
            elif kind == "resume":
                self.control.resume()
            elif kind == "reply":
                self.control.reply(command.get("question_id"), str(command.get("text") or ""))
            elif kind == "message":
                self.control.message(command.get("message_id"), str(command.get("text") or ""))
            elif kind == "stop":
                self.control.emit("state", state="stopping")
                self.task.cancel()
        else:
            raise ValueError("不支持的命令。")
        if isinstance(command.get("command_id"), str):
            self.send({"event": "command_ack", "command_id": command["command_id"],
                       "task_id": command.get("task_id"), "command": kind})

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        await self.stop_driver()
        if self.socket_dir is not None:
            self.socket_dir.cleanup()

    async def stop_driver(self):
        if self.driver is not None and self.driver.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.driver.terminate()
            try:
                await asyncio.wait_for(self.driver.wait(), 3)
            except asyncio.TimeoutError:
                self.driver.kill()
                await self.driver.wait()


async def serve():
    os.umask(0o077)
    protocol_output = sys.stdout
    # Agent console output must never corrupt the event stream or echo a key.
    # Detailed execution evidence is already stored in the private run directory.
    quiet = open(os.devnull, "w")
    sys.stdout = sys.stderr = quiet
    root = Path(os.environ.get("GENERALIST_DATA_DIR") or
                Path.home() / "Library/Application Support/Generalist")
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True, mode=0o700)

    def send(event):
        try:
            protocol_output.write(json.dumps(event, ensure_ascii=False) + "\n")
            protocol_output.flush()
        except BrokenPipeError:
            pass

    host = NativeHost(send, runs)
    reader = asyncio.StreamReader(limit=262144)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    send({"event": "ready", "protocol": PROTOCOL})
    try:
        while line := await reader.readline():
            command = {}
            try:
                command = json.loads(line)
                if not isinstance(command, dict):
                    raise ValueError("命令格式无效。")
                if command.get("command") == "shutdown":
                    break
                await host.command(command)
            except (ValueError, TypeError) as exc:
                send({"event": "error", "text": host.public_error(exc),
                      **({k: command[k] for k in ("task_id", "message_id", "command_id") if k in command}
                         if isinstance(command, dict) else {})})
    finally:
        await host.close()
        transport.close()


def main():
    # A script shipped next to the agent works with Python's isolated (-I) mode.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    asyncio.run(serve())


if __name__ == "__main__":
    main()
