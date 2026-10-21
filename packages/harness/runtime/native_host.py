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

from event_protocol import EventClock, EventProjection, terminal_state, terminal_status, validate_event
from memory_runtime import MemoryService, error_text

PROTOCOL = 1


class NativeFrontend:
    """原生 UI 只消费语义事件，不解析控制台文本。"""
    interactive = True

    def __init__(self, control):
        self.control = control
        self._delta = None
        self._delta_timer = None
        self._preview_chars = 0
        self.event_projection = EventProjection()
        control.frontend = self

    def flush(self):
        if self._delta_timer is not None:
            self._delta_timer.cancel()
            self._delta_timer = None
        if self._delta is not None:
            kind, fields = self._delta
            self._delta = None
            self.control.emit(kind, **fields)

    def bind_abort(self, callback):
        self.control.interrupt_callback = callback

    def handle_sigint(self):
        if self.control.interrupt_callback:
            self.control.interrupt_callback(False)

    def emit(self, event):
        c = self.control
        if event.payload.get("schema_version") is not None:
            if not self.event_projection.consume(event.to_wire()):
                return
            self.flush()
            fields = event.to_wire()
            if c.current_command_id is not None:
                fields["command_id"] = c.current_command_id
                fields["purpose"] = "compaction"
                fields["manual"] = True
            if event.kind == "phase":
                c.active_turn = fields.get("phase") not in {"waiting", "complete"}
                fields["display_step"] = (event.step or 0) + 1
            elif event.kind == "aborted":
                c.active_turn = False; c.waiting_for_follow_up = True; c.suspended = True
                c.cancel_waits()
            elif event.kind == "finished":
                state = terminal_state(terminal_status(fields))
                fields["state"] = "completed" if state == "completed" else "stopped" if state == "aborted" else "failed"
                c.session_terminal_sent = True
            elif event.kind == "session":
                fields.update(model=event.payload.get("model", ""), protocol=c.config.get("llm_protocol", "openai"),
                              reasoning_effort=c.config.get("reasoning_effort", "low"),
                              foreground_allowed=c.foreground_allowed, ask_user_enabled=c.ask_user_enabled)
            c.emit(event.kind, **fields)
            if event.kind == "aborted":
                c.queue_snapshot()
            return
        if event.kind not in {"reasoning_delta", "code_delta", "intent_delta"}:
            self.flush()
        if event.kind == "phase":
            phase = event.payload.get("phase") or event.text
            c.active_turn = phase not in {"waiting", "complete"}
            c.emit("progress", phase=phase, step=(event.step or 0) + 1,
                   manual=event.payload.get("manual", False), command_id=c.current_command_id,
                   purpose="compaction" if phase == "compaction" else "action")
        elif event.kind == "aborted":
            c.active_turn = False
            c.waiting_for_follow_up = True
            c.suspended = True
            c.cancel_waits()
            c.emit("interrupted", text=event.text, worker_reset=getattr(c, "worker_reset", False))
            c.queue_snapshot()
        elif event.kind in {"reasoning_delta", "reasoning_end", "intent_delta", "intent_end", "code_start", "code_delta", "code_end", "code_reset", "model_status", "control_result"}:
            fields = dict(event.payload)
            fields.pop("type", None)
            fields.update(text=event.text, step=event.step)
            if c.current_command_id is not None:
                fields["command_id"] = c.current_command_id
                fields["purpose"] = "compaction"
            if event.kind == "control_result":
                fields["command_id"] = c.current_command_id
            if event.kind == "model_status":
                c.attempt_id = fields.get("attempt_id")
            elif event.kind in {"code_reset", "code_start", "code_delta", "code_end", "reasoning_delta", "reasoning_end", "intent_delta", "intent_end"}:
                fields.setdefault("attempt_id", getattr(c, "attempt_id", None))
            if event.kind == "code_reset":
                self._preview_chars = 0
            if event.kind in {"reasoning_delta", "code_delta", "intent_delta"}:
                room = max(0, 131072 - self._preview_chars)
                fields["text"] = fields["text"][:room]
                self._preview_chars += len(fields["text"])
                if not fields["text"]:
                    if event.kind == "intent_delta" and fields.get("start"):
                        self.flush()
                        c.emit(event.kind, **fields)
                    return
                if self._delta is not None and (self._delta[0] != event.kind or any(self._delta[1].get(key) != fields.get(key) for key in ("block_index", "attempt_id", "turn_index"))):
                    self.flush()
                if self._delta is None:
                    self._delta = (event.kind, fields)
                else:
                    self._delta[1]["text"] += fields["text"]
                if self._delta_timer is None:
                    try:
                        self._delta_timer = asyncio.get_running_loop().call_later(0.03, self.flush)
                    except RuntimeError:
                        self.flush()
            else:
                c.emit(event.kind, **fields)
        elif event.kind == "foreground_consent":
            c.emit("policy", foreground_allowed=c.foreground_allowed, ask_user_enabled=c.ask_user_enabled)
        elif event.kind == "session":
            c.emit("session_info", session_id=event.payload.get("session_id", ""),
                   model=event.payload.get("model", ""), protocol=c.config.get("llm_protocol", "openai"),
                   reasoning_effort=c.config.get("reasoning_effort", "low"),
                   foreground_allowed=c.foreground_allowed, ask_user_enabled=c.ask_user_enabled)

    async def read(self, prompt):
        return await self.control.follow_up()

    async def choose_approval(self, prompt):
        return await self.control.choose_approval(prompt)

    def close(self):
        self.flush()

# These settings belong to the native host's process/task lifecycle, not a file.
# memory_dir 同样由宿主数据根决定，App 配置不能把长期记忆改写到别处。
_HOST_ARGUMENTS = frozenset({"config", "task", "resume", "ui", "ui_api", "driver_mode",
                            "driver_socket", "session_id", "runs_dir", "multi_task", "countdown",
                            "memory_dir"})
_MEMORY_MUTATIONS = {"memory_approve": "approve", "memory_reject": "reject",
                     "memory_forget": "forget", "memory_update": "update"}


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
    if args.demo_mode and "background_mode" not in config:
        args.background_mode = False
    args.llm_base_url = validate_endpoint(str(config.get("llm_base_url") or ""))
    args.model_name = str(config.get("model_name") or "").strip()
    if not args.model_name:
        raise ValueError("请填写模型名称。")
    if args.background_mode and args.demo_mode:
        raise ValueError("后台模式与演示模式不能同时开启。")
    args.multi_task = True
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
    def __init__(self, task_id, send, sanitize=str):
        self.task_id, self.send = task_id, send
        self.sanitize = sanitize
        self.resume_event = asyncio.Event()
        self.resume_event.set()
        self.question_id = None
        self.answer = None
        self.run_dir = None
        self.paused = False
        self.messages = []
        self.message_states = {}
        self.message_orders = {}
        self.message_event = asyncio.Event()
        self.waiting_for_follow_up = False
        self.ended = False
        self.turn_index = 0
        self.overlay_waits = {}
        self.sequence = 0
        self.event_clock = EventClock(task_id)
        self.event_context = {}
        self.policy_revision = 0
        self.active_turn = True
        self.session_terminal_sent = False
        self.suspended = False
        self.commands = []
        self.config = {}
        self.foreground_allowed = False
        self.ask_user_enabled = True
        self.approval_wait = None
        self.approval_id = None
        self.interrupt_callback = None
        self.policy_callback = None
        self.skill_callback = None
        self.compaction_callback = None
        self.current_command_id = None
        self.authorization_epoch = 0
        self.memory_service = None
        self._queue_order = 0

    def next_order(self):
        self._queue_order += 1
        return self._queue_order

    def queue_snapshot(self):
        items = [{"id": m["id"], "text": m["text"], "kind": "message"} for m in self.messages]
        items += [{"id": c.get("command_id", ""), "text": c["command"], "kind": "control"} for c in self.commands]
        orders = dict(self.message_orders)
        orders.update({c.get("command_id", ""): c.get("order", 0) for c in self.commands})
        items.sort(key=lambda item: orders.get(item["id"], 0))
        self.emit("queue_state", items=items, suspended=self.suspended)

    def cancel_waits(self):
        if self.answer is not None and not self.answer.done():
            self.answer.cancel()
        if self.approval_wait is not None and not self.approval_wait.done():
            self.approval_wait.set_result("cancel")
        self.resume_event.set()

    async def choose_approval(self, prompt):
        if self.foreground_allowed:
            return "allow"
        epoch = self.authorization_epoch
        self.approval_id = prompt.request_id
        self.approval_wait = asyncio.get_running_loop().create_future()
        choice = "cancel"
        self.emit("approval", approval_id=self.approval_id, app_id=prompt.app_id,
                  window_id=prompt.window_id,
                  text=prompt.text.replace("本次任务", "本 session").replace("直到任务结束", "直到结束或切换 session"), scope="session")
        try:
            choice = await self.approval_wait
            if epoch != self.authorization_epoch:
                choice = "cancel"
            if choice == "allow":
                self.foreground_allowed = True
                self.policy_revision += 1
                if self.policy_callback:
                    self.policy_callback()
                self.emit("policy", foreground_allowed=True, ask_user_enabled=self.ask_user_enabled)
            return choice
        finally:
            self.emit("approval_closed", approval_id=self.approval_id, choice=choice)
            self.approval_wait = None
            self.approval_id = None

    def bind_runtime(self, *, policy, skill, compaction):
        self.policy_callback, self.skill_callback, self.compaction_callback = policy, skill, compaction
        policy()

    async def process_controls(self):
        while self.commands and not self.suspended:
            if self.messages and self.message_orders.get(self.messages[0]["id"], 0) < self.commands[0].get("order", 0):
                break
            command = self.commands.pop(0)
            self.current_command_id = command.get("command_id")
            try:
                if command["command"] == "compaction":
                    if self.compaction_callback is None:
                        raise ValueError("没有可压缩的历史")
                    self.emit("control_result", command_id=self.current_command_id, status="started", text="正在压缩上下文")
                    await self.compaction_callback()
                elif command["command"] == "skill_select":
                    if self.skill_callback is None:
                        raise ValueError("Skill 尚未准备就绪")
                    self.skill_callback(command.get("name"))
                    self.emit("control_result", command_id=self.current_command_id, status="completed", text="Skill 选择已生效")
            except Exception as exc:
                self.emit("control_result", command_id=self.current_command_id, status="failed", text=self.sanitize(exc))
            finally:
                self.current_command_id = None
                self.queue_snapshot()

    async def overlay(self, event):
        fields = {k: event[k] for k in ("phase", "guard_id", "points") if k in event}
        if "kind" in event: fields["impact"] = event["kind"]
        if fields.get("phase") != "begin":
            self.emit("overlay", **fields)
            return
        guard_id = fields["guard_id"]
        future = asyncio.get_running_loop().create_future()
        self.overlay_waits[guard_id] = future
        try:
            self.emit("overlay", **fields)
            await asyncio.wait_for(future, 5)
        finally:
            self.overlay_waits.pop(guard_id, None)

    def acknowledge_overlay(self, guard_id):
        future = self.overlay_waits.get(guard_id)
        if future is not None and not future.done():
            future.set_result(True)

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
        item = {"id": message_id, "text": text.strip()}
        if self.suspended and not self.active_turn:
            item["correction"] = True
            position = next((i for i, m in enumerate(self.messages) if not m.get("correction")), len(self.messages))
            self.messages.insert(position, item)
        else:
            self.messages.append(item)
        # 顺序独立于展示字段，保证普通消息与控制命令共享边界 FIFO。
        self.message_orders[message_id] = self.next_order()
        self.message_states[message_id] = "queued"
        self.emit("message", message_id=message_id, state="queued")
        self.message_event.set()
        self.queue_snapshot()

    def complete_turn(self, status, answer):
        # A pause requested during the final cell belongs to the completed turn.
        self.resume_event.set()
        self.waiting_for_follow_up = True
        self.active_turn = False
        self.emit("turn_finished", turn_index=self.turn_index,
                  state="completed" if status == "success" else "failed",
                  text=self.sanitize(answer), path=str(self.run_dir or ""))

    def begin_turn(self, text):
        self.waiting_for_follow_up = False
        self.active_turn = True
        self.turn_index += 1
        self.emit("turn_started", turn_index=self.turn_index, text=text)

    async def follow_up(self):
        # Never read the host's NDJSON command pipe through the terminal frontend.
        while True:
            await self.boundary()
            if self.ended:
                return None
            if self.commands and not self.suspended:
                await self.process_controls()
            if self.messages and (not self.suspended or self.messages[0].get("correction")):
                message = self.messages.pop(0)
                self.message_states[message["id"]] = "consumed"
                self.emit("message", message_id=message["id"], state="consumed")
                self.queue_snapshot()
                return message["text"]
            self.message_event.clear()
            await self.message_event.wait()

    def end(self):
        self.ended = True
        self.resume_event.set()
        self.message_event.set()

    def take_messages(self):
        messages, self.messages = self.messages, []
        for message in messages:
            self.message_states[message["id"]] = "applied"
            self.emit("message", message_id=message["id"], state="applied")
        return messages

    def emit(self, event_kind, **fields):
        kind = event_kind
        if kind == "trajectory":
            self.run_dir = Path(fields["path"])
        if fields.get("schema_version") is not None:
            wire = validate_event(dict(fields))
            self.sequence = max(self.sequence, wire["sequence"])
        else:
            context = {**self.event_context}
            session_id = fields.pop("session_id", None)
            if session_id:
                self.event_clock.session_id = session_id
            context.update(task_id=context.get("task_id", self.task_id),
                           turn_id=f"{self.event_clock.stream_id}/turn/{self.turn_index}")
            merged = {**context, **fields}
            merged.pop("policy_revision", None)
            merged.pop("origin", None)
            wire = self.event_clock.make(kind, origin="native_host", policy_revision=self.policy_revision,
                                         **merged)
            self.sequence = wire["sequence"]
        if kind in {"turn_started", "turn_finished", "finished", "aborted", "interrupted"}:
            frontend = getattr(self, "frontend", None)
            if frontend is not None:
                frontend.event_projection.consume(wire)
        alias = {"phase": "progress", "aborted": "interrupted", "session": "session_info"}.get(kind, kind)
        self.send({**wire, "event": alias, "native_task_id": self.task_id,
                   "turn_index": wire.get("turn_index", self.turn_index)})

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
        if not self.ask_user_enabled:
            raise ValueError("ASK_USER_DISABLED：用户已关闭业务提问，请使用已知信息或说明阻塞")
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
        self.emit("reply", question_id=question_id, text=text.strip())


class NativeHost:
    def __init__(self, send, runs_dir: Path, runner=None):
        self.send, self.runs_dir, self.runner = send, runs_dir, runner
        self.task = None
        self.control = None
        self.key = ""
        self.driver = None
        self.socket_dir = None
        self.command_receipts = {}
        # 与 runs 同在宿主数据根下；懒初始化，列表不创建数据库，也不需要 driver。
        self.memory_service = MemoryService(Path(runs_dir).parent / "memory")

    def memory_target(self, command):
        """带 task_id 的记忆命令必须指向当前 live 会话；不带则走连接级。"""
        if command.get("task_id") is None:
            return None
        if not self.control or not self.task or self.task.done() or command["task_id"] != self.control.task_id:
            raise ValueError("会话标识已过期")
        return self.control

    def memory_emit(self, control, kind, fields):
        # live 走 TaskControl 的 v1 信封；连接级是不带 schema_version 的 legacy 帧，task 为 null。
        if control is not None:
            control.emit(kind, **fields)
        else:
            self.send({"event": kind, "kind": kind, "task_id": None, **fields})

    def memory_mutation(self, command):
        operation = _MEMORY_MUTATIONS[command["command"]]
        memory_id, revision = command.get("memory_id"), command.get("revision")
        if not isinstance(command.get("command_id"), str) or not command["command_id"]:
            raise ValueError("记忆命令缺少 command_id")
        if not isinstance(memory_id, str) or not memory_id or len(memory_id) > 64:
            raise ValueError("memory_id 无效")
        if type(revision) is not int or revision < 1:
            raise ValueError("revision 必须是正整数")
        fields = {}
        if operation == "update":
            fields = {key: command[key] for key in ("title", "content", "kind", "app") if key in command}
            if not fields or any(not isinstance(fields[key], str) for key in ("title", "content", "kind") if key in fields) \
                    or ("app" in fields and fields["app"] is not None and not isinstance(fields["app"], str)):
                raise ValueError("memory_update 字段无效")
        return operation, memory_id, revision, fields

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

    def public_text(self, value):
        message = str(value)
        if self.key:
            message = message.replace(self.key, "[REDACTED]")
        return message

    def public_error(self, exc):
        return self.public_text(exc)[:2000]

    async def preflight(self):
        app = os.environ.get("GENERALIST_DRIVER_APP")
        if not app or self.runner is not None:
            return
        from mai_ui import _native
        report = await asyncio.wait_for(_native.macos_admin(
            "doctor", False, None, app, os.environ["GENERALIST_DRIVER_SOCKET"]
        ), 30)
        require_mac_permissions(json.loads(report))

    def resume_path(self, value):
        if not isinstance(value, str) or not value:
            raise ValueError("历史对话的运行路径无效。")
        path = Path(value).resolve()
        root = self.runs_dir.resolve()
        if path == root or not path.is_relative_to(root):
            raise ValueError("只能恢复本应用保存的历史对话。")
        try:
            trajectory = (path / "traj.json").resolve()
            if not trajectory.is_relative_to(root):
                raise ValueError("历史对话的轨迹路径超出记录目录。")
            data = json.loads(trajectory.read_text())
            import mac_agent
            mac_agent.validate_trajectory_ui_api(data, "mai-ui")
            tasks = data.get("tasks")
            if not isinstance(tasks, list) or not tasks or any(not isinstance(task, dict) for task in tasks):
                raise ValueError("历史对话缺少有效的任务记录。")
        except (OSError, ValueError, RuntimeError) as exc:
            raise ValueError(f"无法恢复历史对话：{exc}") from exc
        return path

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
            if command.get("resume_path") is not None:
                args.resume = self.resume_path(command["resume_path"])
            args.task = str(command.get("text") or "").strip()
            if not args.task or len(args.task) > 32000:
                raise ValueError("任务不能为空，且不能超过 32,000 字符。")
            args.api_key = self.key or None
            args.allow_empty_api_key = not self.key
            await self.start_driver()
            await self.preflight()
            control.emit("state", state="running")
            if self.runner is not None:
                await self.runner(args, host_control=control)
            else:
                await mac_agent.run_agent(args, host_control=control, frontend=NativeFrontend(control))
            # The trajectory is the authoritative terminal result, not process exit 0.
            data = json.loads((control.run_dir / "traj.json").read_text()) if control.run_dir else {}
            status = data.get("status", "error")
            if not control.session_terminal_sent:
                control.emit("finished", state="completed" if status == "success" else "failed",
                         text=self.public_text(data.get("final_answer") or data.get("answer")
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
        command_id = command.get("command_id")
        deferred = None
        if command_id in self.command_receipts:
            previous = self.command_receipts[command_id]
            if previous != command:
                raise ValueError("相同 command_id 不能用于不同命令")
            self.send({"event": "command_ack", "command_id": command_id,
                       "task_id": command.get("task_id"), "command": kind})
            return
        if kind == "overlay_ack":
            if self.control and command.get("task_id") == self.control.task_id:
                self.control.acknowledge_overlay(command.get("guard_id"))
            return
        if kind == "start":
            # Validate before closing an idle live conversation or modifying files.
            if command.get("resume_path") is not None:
                self.resume_path(command["resume_path"])
            if self.task is not None and not self.task.done():
                if not self.control.waiting_for_follow_up:
                    raise ValueError("已有任务正在运行。")
                self.control.end()
                await self.task
            task_id = str(command.get("task_id") or "")
            if not task_id or len(task_id) > 100:
                raise ValueError("任务标识无效。")
            self.control = TaskControl(task_id, self.send, self.public_text)
            self.control.memory_service = self.memory_service
            self.control.config = dict(command.get("config") or {})
            if type(self.control.config.get("ask_user_enabled", True)) is not bool:
                raise ValueError("ask_user_enabled 必须为布尔值")
            self.control.ask_user_enabled = self.control.config.get("ask_user_enabled", True)
            self.task = asyncio.create_task(self.run(command, self.control))
            control = self.control
            # Cancellation may precede the coroutine's first instruction.
            self.task.add_done_callback(lambda task: control.emit(
                "finished", state="stopped", text="任务已停止。", path=""
            ) if task.cancelled() else None)
        elif kind in {"skills_list", "skills_reload"}:
            from skill_registry import SkillRegistry, default_skill_roots
            roots = default_skill_roots(__file__)
            extra = (self.control.config.get("skills_dirs") or ()) if self.control else ()
            registry = SkillRegistry(*roots, extra)
            infos = registry.discover()
            event = {"event": "skills_catalog", "skills": [{"name": i.name, "description": i.description, "source": i.source} for i in infos],
                     "selected_name": self.control.config.get("selected_skill") if self.control else None}
            if self.control and command.get("task_id") == self.control.task_id:
                self.control.emit("skills_catalog", skills=event["skills"], selected_name=event["selected_name"])
            else:
                self.send(event)
        elif kind == "memory_list":
            # 宿主存储控制：不排队、不进模型、不需要前台权限。
            live = self.memory_target(command)
            offset = command.get("offset", 0)
            if not isinstance(command_id, str) or not command_id or type(offset) is not int or not 0 <= offset <= 384:
                raise ValueError("记忆目录命令的 command_id 或 offset 无效")
            fields = await asyncio.to_thread(self.memory_service.catalog_fields,
                                             source_command_id=command_id, offset=offset)
            deferred = lambda: self.memory_emit(live, "memory_catalog", fields)
        elif kind in _MEMORY_MUTATIONS:
            operation, memory_id, revision, fields = self.memory_mutation(command)
            target = self.memory_target(command)
            result, catalog = await asyncio.to_thread(self.memory_service.mutate, operation, memory_id, revision, fields)
            catalog["source_command_id"] = command_id
            reply = {"command_id": command_id, "status": "completed" if result["ok"] else "failed",
                     "memory_id": memory_id, "revision": result["revision"],
                     "generation": result["generation"], "error_code": result["error_code"],
                     "text": "记忆已更新" if result["ok"] else error_text(result["error_code"])}
            # 受理回执先于结果；control_result 与目录快照来自同一事务的 generation。
            deferred = lambda: (self.memory_emit(target, "control_result", reply),
                                self.memory_emit(target, "memory_catalog", catalog))
        elif kind in {"interrupt", "compaction_cancel", "set_policy", "approval_reply", "queue_list", "queue_resume", "queue_clear", "compaction", "skill_select"}:
            if not self.control or not self.task or self.task.done() or command.get("task_id") != self.control.task_id:
                raise ValueError("会话标识已过期")
            c = self.control
            if kind in {"interrupt", "compaction_cancel"}:
                if kind == "compaction_cancel" and c.current_command_id is None:
                    pending = [item for item in c.commands if item["command"] == "compaction"]
                    if not pending:
                        raise ValueError("当前没有待执行的压缩")
                    c.commands = [item for item in c.commands if item["command"] != "compaction"]
                    for item in pending:
                        c.emit("control_result", command_id=item.get("command_id"), status="cancelled", text="待执行压缩已取消")
                else:
                    if c.interrupt_callback is None:
                        raise ValueError("当前没有可中止的操作")
                    if kind == "interrupt":
                        if not c.active_turn and c.current_command_id is None:
                            raise ValueError("当前没有活动轮次")
                        c.suspended = True
                    c.interrupt_callback(command.get("force") is True)
            elif kind == "set_policy":
                fields = ("foreground_allowed", "ask_user_enabled")
                if any(field in command and type(command[field]) is not bool for field in fields):
                    raise ValueError("策略必须是布尔值")
                for field in fields:
                    if field in command:
                        setattr(c, field, command[field])
                if command.get("foreground_allowed") is False:
                    c.authorization_epoch += 1
                c.policy_revision += 1
                if not c.ask_user_enabled and c.answer is not None and not c.answer.done():
                    if c.interrupt_callback:
                        c.interrupt_callback(False)
                if "foreground_allowed" in command and c.approval_wait is not None and not c.approval_wait.done():
                    c.approval_wait.set_result("allow" if command["foreground_allowed"] else "cancel")
                if c.policy_callback:
                    c.policy_callback()
                c.emit("policy", foreground_allowed=c.foreground_allowed, ask_user_enabled=c.ask_user_enabled)
            elif kind == "approval_reply":
                if c.approval_id != command.get("approval_id") or c.approval_wait is None or c.approval_wait.done():
                    raise ValueError("授权卡已过期")
                choice = command.get("choice")
                if choice not in {"allow", "reject", "cancel"}:
                    raise ValueError("授权选项无效")
                c.approval_wait.set_result(choice)
            elif kind == "queue_clear":
                for pending in c.commands:
                    c.emit("control_result", command_id=pending.get("command_id"), status="cancelled", text="待执行控制已清空")
                for m in c.messages:
                    c.message_states[m["id"]] = "cancelled"
                    c.emit("message", message_id=m["id"], state="cancelled")
                c.messages.clear()
                c.commands.clear()
            elif kind == "queue_resume":
                c.suspended = False
                c.message_event.set()
            elif kind in {"compaction", "skill_select"}:
                if len(c.commands) >= 20:
                    raise ValueError("控制队列已满")
                if kind == "skill_select":
                    from skill_registry import SkillRegistry, default_skill_roots
                    name = command.get("name")
                    if name is not None:
                        registry = SkillRegistry(*default_skill_roots(__file__), c.config.get("skills_dirs") or ())
                        registry.discover()
                        registry.load(name)
                    c.emit("skill_state", name=name, pending=c.active_turn)
                c.commands.append({**command, "order": c.next_order()})
                c.message_event.set()
            c.queue_snapshot()
            if kind not in {"compaction", "skill_select", "interrupt", "approval_reply"}:
                c.emit("control_result", command_id=command.get("command_id"), status="completed", text="控制已应用")
        elif kind in {"pause", "resume", "reply", "stop", "message", "end"}:
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
            elif kind == "end":
                if not self.control.waiting_for_follow_up:
                    raise ValueError("请先停止正在执行的任务。")
                self.control.end()
                await self.task
            elif kind == "stop":
                self.control.emit("state", state="stopping")
                self.task.cancel()
        else:
            raise ValueError("不支持的命令。")
        if isinstance(command_id, str):
            if len(self.command_receipts) >= 256:
                self.command_receipts.pop(next(iter(self.command_receipts)))
            self.command_receipts[command_id] = dict(command)
            self.send({"event": "command_ack", "command_id": command["command_id"],
                       "task_id": command.get("task_id"), "command": kind})
        if deferred is not None:
            deferred()

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
