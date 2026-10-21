"""Shared process/SSE helpers retained for demo_hub.

The migrated Agent uses mai-ui's exclusive local macOS endpoint lease, so this legacy
multi-session console must not start concurrent Agent subprocesses. ``demo_hub`` uses
exactly one Session and remains the supported UI entry point. CLI/schema parsing still
comes from ``mac_agent.build_parser()``.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from console_protocol import encode_console_message
from event_protocol import EventClock, TERMINAL_STATUSES, terminal_state, terminal_status, validate_event

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

# gui-only 基线输出里的噪声行：tool_call 围栏/裸调用/截屏与调用日志，不进聊天流。
_GUI_NOISE_RE = re.compile(
    r"^\s*<\/?tool_call>\s*$"
    r"|^\[Tool Call"
    r"|^\[Step \d+\] (Capturing|Calling)"
    r"|^\s*(pyautogui|computer)\."
)

BOOL_FLAGS = {
    "bash": "允许模型执行 bash（文件/数据处理）",
    "todo": "启用 todo 进展记录",
    "intent": "启用流式 intent 预览",
    "micro_compaction": "启用 micro compaction",
    "multi_task": "启用多任务（/new 切换）",
    "record_model_requests": "录制完整模型请求（含图片，占磁盘）",
    "background_mode": "后台工作模式（有界面 web 按需启动 + 优先后台操作）",
    "demo_mode": "演示/前台模式（可见 web + 前台真实键鼠优先，用于录制）",
}
NUM_FLAGS = {
    "llm_protocol": ("choice", "模型接口协议（openai / openai-thinking / anthropic）"),
    "reasoning_effort": ("choice", "推理投入档位"),
    "max_steps": ("int", "单会话步数上限（0=不限）"),
    "history_turns": ("int", "上下文保留轮数"),
    "img_keep_n": ("int", "历史截图保留张数"),
    "img_interval": ("int", "截图进上下文的步间隔"),
    "micro_trigger_steps": ("int", "micro compaction 触发步数"),
    "micro_keep_turns": ("int", "micro compaction 保留轮数"),
    "micro_interval": ("int", "micro compaction 间隔"),
    "compaction_trigger_tokens": ("int", "LLM compaction token 触发阈值"),
    "compaction_trigger_steps": ("int", "LLM compaction 步数触发阈值"),
    "compaction_max_tokens": ("int", "摘要最大 token"),
    "api_timeout": ("float", "单次模型请求墙钟上限(秒)"),
    "api_retries": ("int", "模型请求重试次数"),
    "parse_retries": ("int", "解析重试次数"),
    "stall_warn_at": ("int", "停滞警告阈值"),
    "stall_stop_at": ("int", "停滞停止阈值"),
}


_PAIRED_CACHE: dict[str, bool] | None = None


def _paired_flags() -> dict[str, bool]:
    """哪些 bool 开关在 CLI 上是 --enable-/--disable- 对偶；非对偶（纯 store_true）只能“真时传flag”。"""
    global _PAIRED_CACHE
    if _PAIRED_CACHE is None:
        import mac_agent

        parser = mac_agent.build_parser()
        _PAIRED_CACHE = {
            dest: any(f"--disable-{dest.replace('_', '-')}" in (a.option_strings or [])
                      for a in parser._actions)
            for dest in BOOL_FLAGS
        }
    return _PAIRED_CACHE


def build_schema() -> list[dict]:
    import mac_agent

    parser = mac_agent.build_parser()
    defaults = {a.dest: parser.get_default(a.dest) for a in parser._actions}
    paired = _paired_flags()
    schema: list[dict] = []
    for dest, help_text in BOOL_FLAGS.items():
        schema.append({"dest": dest, "kind": "bool", "paired": paired[dest],
                       "default": bool(defaults.get(dest)), "help": help_text})
    for dest, (kind, help_text) in NUM_FLAGS.items():
        item = {"dest": dest, "kind": kind, "default": defaults.get(dest), "help": help_text}
        if kind == "choice":
            action = next((a for a in parser._actions if a.dest == dest and a.choices), None)
            item["choices"] = list(action.choices) if action else ["low", "medium", "xhigh"]
        schema.append(item)
    return schema


EVENT_BACKLOG_LIMIT = 2000
EVENT_QUEUE_LIMIT = 256
SUMMARY_LIMIT = 64
TERMINAL_KINDS = frozenset({"finished", "turn_finished", "aborted", "interrupted", "turn_complete",
                            "task_finished", "gui_turn_complete"})
# 旧 finished 帧把 final_status 放在 text；只认状态词，绝不从回答正文推断成功。
RECOVERY_PRIORITY = {"outcome_unknown": 0, "receipt_unverified": 1, "not_dispatched": 2}
RECOVERY_FIELDS = ("action_id", "status", "operation", "resource_id", "execution", "effect", "verification")


def recovery_summary(event: dict) -> dict:
    """只读历史动作对账：结果未知优先保留，计数覆盖全部条目；不产生 verified、重试或授权。"""
    raw = event.get("actions")
    rows = [{key: value[:200] if isinstance(value, str) else value for key in RECOVERY_FIELDS
             if key in item and (isinstance(value := item[key], (str, int, float, bool)) or value is None)}
            for item in (raw if isinstance(raw, list) else []) if isinstance(item, dict)]
    statuses = [row.get("status") for row in rows]
    order = sorted(range(len(rows)), key=lambda index: RECOVERY_PRIORITY.get(statuses[index], 3))
    actions = [rows[index] for index in order[:SUMMARY_LIMIT]]
    return {**{key: event.get(key) for key in ("kind", "event_id", "sequence", "session_id", "stream_id",
                                               "origin", "timestamp")},
            "text": str(event.get("text") or "")[:4000], "actions": actions, "total": len(rows),
            "unknown": statuses.count("outcome_unknown"), "unverified": statuses.count("receipt_unverified"),
            "not_dispatched": statuses.count("not_dispatched"), "omitted": len(rows) - len(actions),
            "view_only": True, "authoritative": False}


def is_v1_envelope(event: dict) -> bool:
    """只校验共享 flat envelope，不限制扩展语义 kind/payload。"""
    try:
        validate_event(event)
    except ValueError:
        return False
    return True


def event_cursor(path: str, headers) -> int | None:
    """浏览器重连 header 优先于初次连接的 after 查询参数。"""
    query = parse_qs(urlsplit(path).query, keep_blank_values=True)
    raw = headers.get("Last-Event-ID")
    if raw is None:
        values = query.get("after")
        if values is None:
            return None
        if len(values) != 1:
            raise ValueError("after must be one non-negative delivery cursor")
        raw = values[0]
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,20}", raw):
        raise ValueError("cursor must be a non-negative delivery cursor")
    return int(raw)


def sse_frame(event: dict) -> bytes:
    # seq 是 Hub delivery cursor；绝不能使用源 envelope.sequence 作 SSE id。
    return f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


def serve_event_stream(handler, session) -> None:
    try:
        backlog, subscriber = session.subscribe_events(event_cursor(handler.path, handler.headers))
    except ValueError as exc:
        handler._json({"error": str(exc)}, 400)
        return
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        for event in backlog:
            handler.wfile.write(sse_frame(event))
        handler.wfile.flush()
        while True:
            try:
                event = subscriber.get(timeout=15)
            except queue.Empty:
                handler.wfile.write(b": keepalive\n\n")
            else:
                handler.wfile.write(sse_frame(event))
            handler.wfile.flush()
    except (BrokenPipeError, TimeoutError, OSError):
        pass
    finally:
        session.unsubscribe_events(subscriber)


class Session:
    def __init__(self, sid: str, name: str, config: dict, first_message: str, port_note: str = ""):
        self.sid = sid
        self.name = name
        self.config = config
        self.first_message = first_message
        self.created_at = time.time()
        self.run_dir: str | None = None
        self.anchor_title = f"MAI console {sid}"
        self.backlog: list[dict] = []
        self.lock = threading.Lock()
        self._event_ready = threading.Condition(self.lock)
        self._subscribers: set[queue.Queue[dict]] = set()
        self._seq = 0
        self._compat_clock = EventClock(sid)
        self.projection = self._empty_projection()
        self._lifecycle_lock = threading.RLock()
        self._generation = 0
        self.proc: subprocess.Popen | None = None
        self.fifo = self._new_fifo_path()
        self._fifo_fd: int | None = None
        self.status = "starting"

    def _new_fifo_path(self) -> Path:
        return Path(
            f"/tmp/mai_console_{self.sid}_{os.getpid()}_{uuid.uuid4().hex[:8]}.in"
        )

    def _close_transport_locked(self) -> None:
        if self._fifo_fd is not None:
            try:
                os.close(self._fifo_fd)
            except OSError:
                pass
            self._fifo_fd = None
        try:
            self.fifo.unlink(missing_ok=True)
        except OSError:
            pass

    def _stop_process_locked(self, *, timeout: float = 8.0) -> None:
        """Stop the current agent and wait for its cleanup before reuse.

        SIGINT lets both agents run their Python ``finally`` blocks.  The harder
        signals are bounded fallbacks for a wedged process; waiting here prevents a
        replacement process from sharing the old FIFO or native SDK lease.
        """
        proc = self.proc
        if proc is None or proc.poll() is not None:
            self._close_transport_locked()
            return
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=timeout)
        except (AttributeError, ProcessLookupError):
            pass
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except (AttributeError, ProcessLookupError):
                pass
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except (AttributeError, ProcessLookupError, subprocess.TimeoutExpired):
                    pass
        finally:
            self._close_transport_locked()

    def _empty_projection(self) -> dict:
        return {"session_id": None, "task_id": None, "turn_id": None, "attempt_id": None,
                "phase": None, "model_status": None, "last_intent": None, "question": None,
                "terminal": None, "actions": [], "completed_turns": [], "step": None,
                "recovery_report": None, "source_sequence": -1}

    def _compat_envelope_locked(self, event: dict) -> dict:
        kind = event.get("kind") or event.get("type") or event.get("event") or "stdout"
        fields = {key: value for key, value in event.items()
                  if key not in {"kind", "text", "step", "task_id", "turn_id", "origin", "policy_revision"}}
        wire = self._compat_clock.make(kind, text=str(event.get("text") or ""), step=event.get("step"),
                                       task_id=event.get("task_id"), turn_id=event.get("turn_id"),
                                       origin="hub_compat", policy_revision=0, **fields)
        wire["step_id"] = event.get("step_id")
        return wire

    @staticmethod
    def _summary(event: dict) -> dict:
        keys = ("type", "retrying", "session_id", "task_id", "turn_id", "attempt_id", "event_id", "sequence",
                "kind", "origin", "step", "step_id", "action_id", "resource_id", "observation_id",
                "phase", "status", "state", "action_status", "stage", "at_ms", "attempt", "max_attempts",
                "answer_displayed",
                "timeout_seconds", "policy_revision", "execution", "effect", "verification", "name", "source", "text", "intent", "question",
                "answer", "reason", "round_index", "block_index", "preview_type")
        # 这是有界展示摘要，不是原始 run 或业务结果证明。
        return {key: value[:4000] if isinstance(value, str) else value
                for key in keys if key in event
                and (isinstance(value := event[key], (str, int, float, bool)) or value is None)}

    @staticmethod
    def _scope(event: dict) -> tuple:
        return tuple(event.get(key) for key in ("session_id", "turn_id", "attempt_id"))

    def _project_locked(self, event: dict) -> None:
        kind = event["kind"]
        state = self.projection
        structured = event.get("origin") != "hub_compat"
        if kind == "reset":
            self.projection = self._empty_projection()
            return
        if kind == "recovery_report" and not structured:
            return  # 旧兼容帧不是对账来源
        if structured:
            scope = self._scope(event)
            current = self._scope(state)
            if scope[0] == current[0]:
                turn = scope[1] if scope[1] is not None else current[1]
                attempt = scope[2] if scope[2] is not None or turn != current[1] else current[2]
                scope = (scope[0], turn, attempt)
            if kind in {"stdout", "stream"}:
                return
            if any(self._scope(item)[:2] == scope[:2] and (
                    item.get("turn_closed") or self._scope(item) == scope)
                   for item in state["completed_turns"]):
                return
            if scope[0] == current[0] and event["sequence"] <= state["source_sequence"]:
                return
            if kind == "recovery_report" and scope[0] == current[0]:
                # 会话级只读帧：不移动 turn/attempt，不改 phase/terminal。
                state["source_sequence"] = event["sequence"]
                state["recovery_report"] = recovery_summary(event)
                return
            if state["session_id"] is not None and scope != current:
                if scope[0] != current[0]:
                    state["recovery_report"] = None  # 对账只属于本 session
                # 缺 attempt_id 的阶段/终态属于当前 turn，而非切回旧请求。
                same_turn = scope[:2] == current[:2]
                if same_turn and scope[2] is None:
                    scope = current
                else:
                    state["completed_turns"].append({**dict(zip(
                        ("session_id", "turn_id", "attempt_id"), current)), "turn_closed": not same_turn})
                    state["completed_turns"] = state["completed_turns"][-SUMMARY_LIMIT:]
                    if not same_turn:
                        for key in ("phase", "model_status", "last_intent", "question", "terminal"):
                            state[key] = None
                        state["actions"] = []
                    else:
                        state["model_status"] = None
            state.update(dict(zip(("session_id", "turn_id", "attempt_id"), scope)))
            state["task_id"] = event.get("task_id")
            state["source_sequence"] = event["sequence"]
            if kind == "recovery_report":
                state["recovery_report"] = recovery_summary(event)
                return
            if state["terminal"] and kind not in TERMINAL_KINDS | {"answer", "status", "state"}:
                return
        summary = self._summary(event)
        if event.get("step") is not None:
            state["step"] = event["step"]
        if kind in {"user", "gui_turn_start", "turn_started", "task_started"}:
            for key in ("terminal", "question", "model_status", "last_intent"):
                state[key] = None
            state["phase"] = "thinking"
            state["actions"] = []
        elif kind == "phase":
            state["phase"] = event.get("phase") or event["text"]
            if state["phase"] not in {"waiting_user", "question"}:
                state["question"] = None
        elif kind == "model_status":
            state["model_status"] = summary
        elif kind in {"intent", "intent_start", "intent_delta", "intent_end", "intent_final", "gui_intent", "gui_preview"}:
            previous = state["last_intent"] or {}
            same_block = all(previous.get(key) == event.get(key) for key in ("step", "block_index"))
            prefix = previous.get("text", "") if same_block else ""
            intent_kind = event.get("preview_type") if kind == "gui_preview" else kind
            if intent_kind == "preview_reset":
                state["last_intent"] = None
                return
            if intent_kind == "intent_delta":
                summary["text"] = (prefix + str(event.get("delta") or event["text"]))[-4000:]
            else:
                summary["text"] = str(event.get("intent") or event["text"] or prefix)[-4000:]
            state["last_intent"] = summary
        elif kind == "question":
            state["question"] = summary
            state["phase"] = "waiting_user"
        elif kind == "action_status":
            action_key = tuple(event.get(key) for key in ("action_id", "step_id", "step", "resource_id"))
            actions = state["actions"]
            actions[:] = [item for item in actions if tuple(item.get(key) for key in
                         ("action_id", "step_id", "step", "resource_id")) != action_key]
            actions.append(summary)
            state["actions"] = actions[-SUMMARY_LIMIT:]
            state["phase"] = event.get("stage") or event.get("status") or event.get("action_status") or event["text"]
        elif (kind == "answer" or kind in TERMINAL_KINDS
              or kind in {"status", "state"} and event.get("status") in TERMINAL_STATUSES):
            previous = state["terminal"] or {}
            if kind in {"status", "state"} and previous:
                return
            # answer+finished 同轮共用一个终态：首个回答保留，状态取最新显式值，缺失时沿用前一终态。
            status = terminal_status(event)
            if status is None:
                status = previous.get("terminal_status")
            answer = previous.get("answer") or (event.get("text") if kind == "answer" else event.get("answer"))
            if isinstance(answer, str) and answer:
                summary["answer"] = answer[:4000]
            summary.update(terminal_status=status, terminal_state=terminal_state(status))
            state["terminal"] = summary
            state["question"] = None
            state["model_status"] = None
            state["phase"] = summary["terminal_state"]

    def _snapshot_locked(self, reason: str, *, overflow: bool = False) -> dict:
        kind = "resync_snapshot" if overflow else "snapshot"
        return {**self._compat_envelope_locked({"type": kind}), "type": kind, "seq": self._seq,
                "cursor": self._seq, "state": copy.deepcopy(self.projection), "reason": reason,
                "view_only": True, "authoritative": False}

    def event_view(self) -> dict:
        with self._event_ready:
            return {"cursor": self._seq, "state": copy.deepcopy(self.projection),
                    "structured_semantics": getattr(self, "structured_semantics_seen", False),
                    "view_only": True, "authoritative": False}

    def push(self, event: dict) -> None:
        with self._event_ready:
            if "schema_version" in event:
                if not is_v1_envelope(event):
                    raise ValueError("invalid v1 event envelope")
                event = copy.deepcopy(event)
            else:
                event = self._compat_envelope_locked(copy.deepcopy(event))
            self._seq += 1
            event.update(type=event["kind"], seq=self._seq)
            self._project_locked(event)
            self.backlog.append(event)
            if len(self.backlog) > EVENT_BACKLOG_LIMIT:
                del self.backlog[:len(self.backlog) - EVENT_BACKLOG_LIMIT]
            # 注册、投影、捕获和入队都在同一锁中；慢客户端只替换成快照，不阻塞发布。
            for subscriber in self._subscribers:
                try:
                    subscriber.put_nowait(event)
                except queue.Full:
                    while True:
                        try:
                            subscriber.get_nowait()
                        except queue.Empty:
                            break
                    subscriber.put_nowait(self._snapshot_locked("slow_consumer", overflow=True))

    def subscribe_events(self, cursor: int | None = None) -> tuple[list[dict], queue.Queue[dict]]:
        """原 noarg 返回 backlog+私有队列；cursor 为 delivery cursor，非源 sequence。"""
        subscriber: queue.Queue[dict] = queue.Queue(maxsize=EVENT_QUEUE_LIMIT)
        with self._event_ready:
            if cursor is not None and (type(cursor) is not int or cursor < 0 or cursor > self._seq):
                raise ValueError("cursor must be between 0 and the current delivery watermark")
            oldest = self.backlog[0]["seq"] if self.backlog else self._seq + 1
            if cursor is not None and cursor < oldest - 1:
                backlog = [self._snapshot_locked("retention_expired")]
            else:
                backlog = [event for event in self.backlog if cursor is None or event["seq"] > cursor]
            self._subscribers.add(subscriber)
        return backlog, subscriber

    def unsubscribe_events(self, subscriber: queue.Queue[dict]) -> None:
        with self._event_ready:
            self._subscribers.discard(subscriber)

    def _state_event(self) -> dict:
        state = {"type": "state", "run_dir": self.run_dir, "steps": 0,
                 "status": self.status, "ask_user": None}
        if self.run_dir:
            traj = Path(self.run_dir) / "traj.json"
            try:
                data = json.loads(traj.read_text(encoding="utf-8"))
                state["steps"] = len(data.get("steps") or [])
                state["status"] = data.get("status") or state["status"]
            except Exception:
                pass
            log = Path(self.run_dir) / "agent.log"
            try:
                tail = log.read_text(encoding="utf-8", errors="replace")[-4000:]
                for line in reversed(tail.splitlines()):
                    if "ask_user question=" in line:
                        raw_q = line.split("ask_user question=", 1)[1].strip()
                        try:  # 日志是 %r 转义形式，还原成真换行再展示
                            import ast as _ast
                            raw_q = str(_ast.literal_eval(raw_q))
                        except Exception:  # noqa: BLE001 - 非 repr 形式原样使用
                            raw_q = raw_q.strip().strip(chr(39) + chr(34))
                        state["ask_user"] = raw_q[:4000]
                        break
            except Exception:
                pass
        return state

    def start(self) -> None:
        with self._lifecycle_lock:
            self._close_transport_locked()
            self.fifo = self._new_fifo_path()
            os.mkfifo(self.fifo)
            # O_RDWR 持有写端（不阻塞），保证 fifo 常驻；再开读端给子进程作 stdin。
            self._fifo_fd = os.open(self.fifo, os.O_RDWR)
            read_fd = os.open(self.fifo, os.O_RDONLY)
            if not self.config.get("background_mode", True):
                self._create_space_anchor()
            cmd = [sys.executable, str(REPO / "mac_agent.py"), "--startup-observation", "none",
                   self.first_message]
            cmd += self._config_args()
            env = {**os.environ, "MAI_FOREGROUND_REQUIRES_VISIBLE": "1",
                   "MAI_CONSOLE_SESSION": self.sid}
            try:
                proc = subprocess.Popen(
                    cmd, stdin=read_fd, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env, cwd=str(REPO), text=True, bufsize=1)
            except BaseException:
                self._close_transport_locked()
                self.status = "exited"
                raise
            finally:
                os.close(read_fd)
            self.proc = proc
            self._generation += 1
            generation = self._generation
            self.status = "running"
        threading.Thread(
            target=self._read_stdout, args=(proc, generation), daemon=True).start()
        threading.Thread(
            target=self._poll_state, args=(proc, generation), daemon=True).start()

    def _config_args(self) -> list[str]:
        args: list[str] = []
        paired = _paired_flags()
        for dest, value in self.config.items():
            if dest in BOOL_FLAGS:
                flag = dest.replace("_", "-")
                if value:
                    args += [f"--enable-{flag}" if paired[dest] else f"--{flag}"]
                elif paired[dest]:
                    args += [f"--disable-{flag}"]
                # 非对偶开关取消勾选＝不传 flag（store_true 默认即 False）
            elif value is not None:
                args += [f"--{dest.replace('_', '-')}", str(value)]
        return args

    def _create_space_anchor(self) -> None:
        script = f'''
        tell application "Google Chrome"
            set w to make new window
            set URL of active tab of w to "about:blank"
            set title of w to "{self.anchor_title}"
        end tell
        tell application "System Events"
            tell process "Google Chrome"
                try
                    set value of attribute "AXFullScreen" of (first window whose name is "{self.anchor_title}") to true
                end try
            end tell
        end tell
        '''
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=20)
            self.push({"type": "space", "anchor": self.anchor_title,
                       "note": "已尝试为该会话创建全屏锚定窗（独立 Space）；foreground 操作仅在该 Space 可见时放行"})
        except Exception as exc:  # 锚定失败不阻塞会话
            self.push({"type": "space", "anchor": None, "note": f"Space 锚定失败（降级为逻辑隔离）：{exc}"})

    def _read_stdout(
        self, proc: subprocess.Popen | None = None, generation: int | None = None,
    ) -> None:
        proc = proc or self.proc
        with self._lifecycle_lock:
            if self.proc is not proc or (
                generation is not None and generation != self._generation
            ):
                return
        assert proc and proc.stdout
        fd = proc.stdout.fileno()
        buf = b""
        last_stream = 0.0
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            with self._lifecycle_lock:
                if self.proc is not proc or (
                    generation is not None and generation != self._generation
                ):
                    return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._push_line(line.decode("utf-8", "replace"))
            # 流式预览：未换行的在途内容（reasoning/intent/code delta）也限频推给前端，
            # 否则行迭代器要等换行才吐，录制/界面在长生成期间看起来像冻住。
            now = time.time()
            if buf and now - last_stream > 0.15:
                last_stream = now
                self.push({"type": "stream", "text": buf.decode("utf-8", "replace")})
        with self._lifecycle_lock:
            current = self.proc is proc and (
                generation is None or generation == self._generation
            )
            if not current:
                return
        if buf:
            self._push_line(buf.decode("utf-8", "replace"))
        self._gui_flush()
        self._warn_flush()
        with self._lifecycle_lock:
            current = self.proc is proc and (
                generation is None or generation == self._generation
            )
            if not current:
                return
            if self.status not in {"stopping", "stopped"}:
                self.status = "exited"
            terminal_status = self.status
            self._close_transport_locked()
        self.push(self._state_event())
        self.push({"type": "status", "status": terminal_status})

    def _warn_flush(self) -> None:
        buf = getattr(self, "_warn_buf", None)
        if buf:
            self.push({"type": "warn", "text": "\n".join(buf[:8])})
        self._warn_buf = None

    def _gui_flush(self) -> None:
        buf = getattr(self, "_gui_buf", None)
        if buf and buf[1]:
            self.push({"type": buf[0], "text": "\n".join(buf[1])})
        self._gui_buf = None

    def _push_line(self, text: str) -> None:
        text = text.rstrip("\n")
        if not text:
            return
        if text.startswith("Trajectory: "):
            self.run_dir = text[len("Trajectory: "):].strip()
        if getattr(self, "gui_coalesce", False):
            # gui-only 基线的输出按块合并：[Think]+正文→一条 reasoning，
            # [Action]+坐标/结果→一条 intent；噪声行（tool_call 围栏/裸调用/截屏日志）丢弃。
            if text == "[Think]":
                self._gui_flush()
                self._gui_buf = ["reasoning", []]
                return
            m_step = re.match(r"^\[Step\s+(\d+)\]", text)
            if m_step:
                self.push({"type": "gui_step", "step": int(m_step.group(1))})
            if text.startswith("[Turn Start] "):
                self.push({"type": "gui_turn_start", "round_index": int(text[len("[Turn Start] "):])})
                return
            if text.startswith("[Reasoning Delta] "):
                raw = text[len("[Reasoning Delta] "):]
                try:
                    delta = json.loads(raw)
                except json.JSONDecodeError:
                    delta = raw
                self.push({"type": "gui_reasoning_delta", "delta": str(delta)})
                return
            if text.startswith("[GUI Preview] "):
                try:
                    preview = json.loads(text[len("[GUI Preview] "):])
                except json.JSONDecodeError:
                    return
                if isinstance(preview, dict) and preview.get("type") in {
                    "intent_start", "intent_delta", "intent_end", "intent_final", "preview_reset"
                } and isinstance(preview.get("step"), int):
                    self.push({**preview, "type": "gui_preview", "preview_type": preview["type"]})
                return
            if text.startswith("[Intent] "):
                self._gui_flush()
                self.push({"type": "gui_intent", "text": text[len("[Intent] "):]})
                return
            if text.startswith("[Action"):
                self._gui_flush()
                self._gui_buf = ["intent", [text]]
                return
            if text.startswith("Answer:"):
                self._gui_flush()
                self.push({"type": "answer", "text": text[len("Answer:"):].strip()})
                return
            if text.startswith("[Turn Complete] "):
                self._gui_flush()
                self.push({"type": "gui_turn_complete", "status": text[len("[Turn Complete] "):].strip()})
                return
            if text.startswith(("Finished:", "Trajectory saved to:", "Last model input saved to:",
                                "Starting in", "Safety: press")):
                # 结束元信息/倒计时/安全提示不进聊天流；Finished 作为块边界触发 flush。
                if text.startswith("Finished:"):
                    self._gui_flush()
                return
            if _GUI_NOISE_RE.match(text):
                if text.startswith("[Tool Call"):
                    self._gui_flush()
                return
            buf = getattr(self, "_gui_buf", None)
            if buf is not None:
                buf[1].append(text)
                return
        if ("Error" in text or "Traceback" in text or text.startswith("  File ")) and \
                not text.startswith("Trajectory:"):
            # 错误/traceback 行折叠成单条 warn 事件，保证失败在界面上可见。
            buf = getattr(self, "_warn_buf", None)
            if buf is None:
                buf = self._warn_buf = []
            if not text.startswith(" ") or "File" in text or text.strip().startswith(("Error", "CodeParseError", "MuError")):
                buf.append(text.strip()[:200])
            return
        self._warn_flush()
        kind = "stdout"
        if "reasoning: " in text[:40] or text.startswith("[Think]"):
            kind = "reasoning"
        elif "（预览）: " in text or text.startswith("[Action"):
            kind = "intent"
        elif "该次预览已撤销" in text or text.startswith("[System] "):
            kind = "warn"
        if kind == "warn":
            self.push({"type": "warn", "text": text})
            return
        self.push({"type": kind, "text": text})

    def _poll_state(
        self, proc: subprocess.Popen | None = None, generation: int | None = None,
    ) -> None:
        proc = proc or self.proc
        while True:
            time.sleep(2)
            with self._lifecycle_lock:
                if (
                    self.status != "running"
                    or self.proc is not proc
                    or (generation is not None and generation != self._generation)
                ):
                    return
            self.push(self._state_event())

    def send(self, text: str) -> None:
        with self._lifecycle_lock:
            if (
                self.proc is None or self.proc.poll() is not None
                or self._fifo_fd is None
            ):
                raise RuntimeError("agent process is not accepting messages")
            frame = memoryview(encode_console_message(text))
            while frame:
                written = os.write(self._fifo_fd, frame)
                if written <= 0:
                    raise BrokenPipeError("console message transport accepted no bytes")
                frame = frame[written:]
        self.push({"type": "user", "text": text})

    def stop(self) -> None:
        with self._lifecycle_lock:
            self.status = "stopping"
            self._stop_process_locked()
            self.status = "stopped"
        subprocess.run(["osascript", "-e",
                        f'tell application "Google Chrome" to close (every window whose title is "{self.anchor_title}")'],
                       capture_output=True, timeout=15)
        self.push({"type": "status", "status": "stopped"})


class Manager:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.schema = build_schema()

    def create(self, name: str, config: dict, first_message: str) -> Session:
        sid = uuid.uuid4().hex[:8]
        session = Session(sid, name or f"session-{sid}", config, first_message)
        self.sessions[sid] = session
        threading.Thread(target=session.start, daemon=True).start()
        return session


MANAGER = Manager()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静默访问日志
        pass

    def _json(self, payload, code=200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        route = urlsplit(self.path).path
        if route == "/":
            body = UI_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/schema":
            self._json(MANAGER.schema)
        elif self.path == "/api/sessions":
            self._json([{"sid": s.sid, "name": s.name, "status": s.status,
                         "run_dir": s.run_dir, "anchor": s.anchor_title,
                         "config": s.config, "created_at": s.created_at}
                        for s in MANAGER.sessions.values()])
        elif route.endswith("/stream"):
            sid = route.split("/")[-2]
            session = MANAGER.sessions.get(sid)
            if not session:
                self._json({"error": "no session"}, 404)
                return
            serve_event_stream(self, session)
        elif self.path.endswith("/history"):
            sid = self.path.split("/")[-2]
            session = MANAGER.sessions.get(sid)
            payload = {"run_dir": session.run_dir if session else None, "steps": []}
            if session and session.run_dir:
                traj = Path(session.run_dir) / "traj.json"
                try:
                    data = json.loads(traj.read_text(encoding="utf-8"))
                    payload["steps"] = [
                        {"step": s.get("step"), "intent": s.get("intent"),
                         "plan": round((s.get("metrics") or {}).get("planning_time_seconds") or 0),
                         "exec": round((s.get("metrics") or {}).get("action_execution_time_seconds") or 0)}
                        for s in data.get("steps") or []]
                    payload["status"] = data.get("status")
                except Exception:
                    pass
            self._json(payload)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path == "/api/sessions":
            body = self._body()
            session = MANAGER.create(body.get("name") or "", body.get("config") or {},
                                     body.get("first_message") or "你好")
            self._json({"sid": session.sid, "name": session.name})
        elif self.path.endswith("/message"):
            sid = self.path.split("/")[-2]
            session = MANAGER.sessions.get(sid)
            if not session:
                self._json({"error": "no session"}, 404)
                return
            session.send(self._body().get("text") or "")
            self._json({"ok": True})
        elif self.path.endswith("/stop"):
            sid = self.path.split("/")[-2]
            session = MANAGER.sessions.get(sid)
            if not session:
                self._json({"error": "no session"}, 404)
                return
            session.stop()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


UI_HTML = r"""
<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mac_agent console</title>
<style>
:root {
  color-scheme: dark;
  --bg: #0d1117; --panel: #151b23; --panel2: #1a212b; --panel3: #202936;
  --line: #262d38; --line2: #303947;
  --text: #e6edf3; --muted: #98a3b0; --faint: #6b7684;
  --acc: #7c9cff; --acc2: #3fd68f; --warn: #e8b34b; --danger: #ff7b82;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --r: 12px;
  --shadow: 0 1px 0 rgba(255,255,255,.03) inset, 0 8px 24px rgba(0,0,0,.35);
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.55 -apple-system, "PingFang SC", "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased; }
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: #2c3542; border-radius: 8px; border: 2px solid var(--bg); }
::-webkit-scrollbar-thumb:hover { background: #3a4553; }
::-webkit-scrollbar-track { background: transparent; }
:focus-visible { outline: 2px solid var(--acc); outline-offset: 2px; border-radius: 6px; }

#topbar { position: sticky; top: 0; z-index: 9; display: flex; align-items: center; gap: 12px;
  padding: 12px 20px; background: rgba(13,17,23,.86); backdrop-filter: blur(12px); }
#topbar::after { content: ""; position: absolute; left: 0; right: 0; bottom: -1px; height: 1px;
  background: linear-gradient(90deg, transparent, #7c9cff55 30%, #3fd68f55 70%, transparent); }
#topbar .logo { width: 22px; height: 22px; border-radius: 7px; flex: none;
  background: linear-gradient(135deg, var(--acc), var(--acc2));
  box-shadow: 0 0 0 1px #ffffff14, 0 4px 14px #7c9cff40; }
#topbar h1 { font-size: 14.5px; font-weight: 650; margin: 0; letter-spacing: .2px; }
#topbar .sub { color: var(--faint); font-size: 12px; margin-top: 1px; }
#topbar .spacer { flex: 1; }
.chip { font: 500 11px/1 var(--mono); color: var(--muted); background: var(--panel2);
  border: 1px solid var(--line); border-radius: 999px; padding: 5px 10px; }

#app { display: grid; grid-template-columns: 312px minmax(0,1fr) 312px;
  height: calc(100vh - 47px); }
aside, section { overflow-y: auto; padding: 16px 14px; background: var(--panel); }
aside { border-right: 1px solid var(--line); }
section { border-left: 1px solid var(--line); }
main { display: flex; flex-direction: column; min-width: 0; }

h2 { display: flex; align-items: center; gap: 8px; font-size: 11px; font-weight: 650;
  letter-spacing: 1.4px; text-transform: uppercase; color: var(--faint); margin: 20px 4px 10px; }
h2:first-of-type { margin-top: 6px; }
h2 .n { font-family: var(--mono); color: var(--faint); background: var(--panel2);
  border: 1px solid var(--line); border-radius: 6px; padding: 1px 6px; font-size: 10px; }

button { font: inherit; border: 1px solid var(--line2); border-radius: 9px; padding: 8px 14px;
  cursor: pointer; color: var(--text); background: linear-gradient(180deg, var(--panel3), var(--panel2));
  box-shadow: 0 1px 0 #ffffff08 inset; transition: border-color .15s, filter .15s, transform .05s; }
button:hover { border-color: #46536a; }
button:active { transform: translateY(1px); }
button.primary { border: 0; color: #06130c; font-weight: 700;
  background: linear-gradient(135deg, #8fb0ff, #3fd68f);
  box-shadow: 0 2px 16px #3fd68f33, 0 1px 0 #ffffff55 inset; }
button.primary:hover { filter: brightness(1.07); }
button.danger { color: var(--danger); border-color: #5c3134; background: linear-gradient(180deg, #2a1a1d, #221518); }
button.danger:hover { border-color: #7a4045; }
button:disabled { opacity: .38; cursor: not-allowed; transform: none; }

.session { position: relative; padding: 10px 12px 10px 14px; border-radius: var(--r); margin-bottom: 8px;
  cursor: pointer; background: var(--panel2); border: 1px solid var(--line); transition: .15s; }
.session::before { content: ""; position: absolute; left: 0; top: 10px; bottom: 10px; width: 3px;
  border-radius: 3px; background: var(--faint); opacity: .5; }
.session.running::before { background: var(--acc2); opacity: 1; box-shadow: 0 0 10px #3fd68f88; }
.session.stopping::before { background: var(--warn); opacity: 1; }
.session:hover { border-color: var(--line2); background: var(--panel3); }
.session.active { border-color: #4d6390; background: #1b2432;
  box-shadow: 0 0 0 1px #7c9cff33, var(--shadow); }
.session .name { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 13.5px; }
.session .meta { margin-top: 3px; color: var(--faint); font: 400 11px/1.4 var(--mono); }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--faint); flex: none; }
.session.running .dot { background: var(--acc2); animation: pulse 1.5s infinite; }
@keyframes pulse { 50% { opacity: .3; } }

details.group { border: 1px solid var(--line); border-radius: var(--r); margin-bottom: 8px;
  background: var(--panel2); }
details.group > summary { cursor: pointer; list-style: none; padding: 9px 12px;
  display: flex; align-items: center; gap: 8px; font-size: 12.5px; font-weight: 600; color: var(--muted); }
details.group > summary::before { content: "▸"; color: var(--faint); transition: transform .15s; }
details.group[open] > summary::before { transform: rotate(90deg); }
details.group > summary:hover { color: var(--text); }
details.group .body { padding: 6px 12px 12px; border-top: 1px solid var(--line); }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 10px; }
.field { margin: 9px 0; }
.field .cap { display: block; font-size: 11.5px; color: var(--muted); margin-bottom: 5px; }
.field .cap b { color: var(--text); font: 600 11.5px var(--mono); }
input[type=text], input[type=number], select { width: 100%; font: 400 12.5px var(--mono);
  color: var(--text); background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
  padding: 7px 10px; outline: none; transition: border-color .15s, box-shadow .15s; }
input:focus, select:focus { border-color: var(--acc); box-shadow: 0 0 0 3px #7c9cff22; }
input[type=number] { appearance: textfield; -moz-appearance: textfield; }
input[type=number]::-webkit-inner-spin-button,
input[type=number]::-webkit-outer-spin-button { -webkit-appearance: none; margin: 0; }
.switch { display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 7px 0; font-size: 12.5px; color: var(--muted); }
.switch input { display: none; }
.switch .track { width: 34px; height: 20px; border-radius: 999px; background: #2b3542;
  position: relative; cursor: pointer; transition: background .18s; flex: none;
  box-shadow: 0 1px 3px #0006 inset; }
.switch .track::after { content: ""; position: absolute; top: 3px; left: 3px; width: 14px; height: 14px;
  border-radius: 50%; background: #93a0ae; transition: left .18s cubic-bezier(.34,1.56,.64,1), background .18s; }
.switch input:checked + .track { background: #1d4d38; }
.switch input:checked + .track::after { left: 17px; background: var(--acc2); }

#chathead { display: flex; align-items: center; gap: 12px; padding: 12px 20px;
  border-bottom: 1px solid var(--line); background: var(--panel); }
#chathead .phasedot { width: 9px; height: 9px; border-radius: 50%; background: var(--faint); }
#chathead .phasedot.running { background: var(--acc2); animation: pulse 1.5s infinite; }
#chathead .phasedot.exited { background: var(--faint); }
#chathead .t { font-weight: 650; font-size: 14px; }
#chathead .st { color: var(--faint); font: 400 11.5px var(--mono); }
#log { flex: 1; overflow-y: auto; padding: 18px 20px; display: flex; flex-direction: column; gap: 10px; }
.ev { border-radius: 10px; padding: 9px 13px; font-size: 13px; max-width: 880px;
  border: 1px solid var(--line); border-left: 3px solid var(--line2); background: var(--panel); }
.ev.stdout { font: 400 12px/1.6 var(--mono); color: #c6d0da; white-space: pre-wrap;
  word-break: break-word; border-left-color: #3a4553; }
.ev.intent { background: #14251d; border-color: #23463c; border-left-color: var(--acc2); color: #b9ebd0; }
.ev.warn { background: #26200f; border-color: #4d3d1e; border-left-color: var(--warn); color: #f0d9a6; }
.ev.user { align-self: flex-end; background: #16233b; border-color: #27406b;
  border-left: 1px solid #27406b; border-right: 3px solid var(--acc); color: #c4d8ff; }
.ev.space { background: var(--panel2); border-style: dashed; color: var(--muted); font-size: 12px; }
details.reasoning { max-width: 880px; border: 1px solid var(--line); border-radius: 10px;
  background: var(--panel); }
details.reasoning summary { cursor: pointer; list-style: none; padding: 8px 13px;
  color: var(--faint); font: 500 11.5px var(--mono); display: flex; gap: 8px; align-items: center; }
details.reasoning summary::before { content: "◦"; color: var(--acc); }
details.reasoning summary:hover { color: var(--muted); }
details.reasoning .body { padding: 0 14px 12px; color: var(--muted); font-size: 12.5px;
  white-space: pre-wrap; word-break: break-word; max-height: 320px; overflow-y: auto;
  mask-image: linear-gradient(#000 78%, transparent); }
#askbox { display: none; margin: 0 20px 12px; padding: 11px 15px; border-radius: 10px;
  background: linear-gradient(180deg, #2a2210, #241d0e); border: 1px solid #57431c;
  color: #f0d9a6; font-size: 13px; }
#chatbar { display: flex; gap: 10px; padding: 14px 20px; border-top: 1px solid var(--line);
  background: var(--panel); }
#msg { flex: 1; font: 400 13.5px inherit; font-family: inherit; color: var(--text);
  background: var(--bg); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px;
  outline: none; transition: border-color .15s, box-shadow .15s; }
#msg:focus { border-color: var(--acc); box-shadow: 0 0 0 3px #7c9cff22; }

.card { background: var(--panel2); border: 1px solid var(--line); border-radius: var(--r);
  padding: 10px 13px; margin-bottom: 9px; }
.card .row { display: flex; justify-content: space-between; gap: 10px; padding: 3px 0;
  font-size: 12.5px; align-items: baseline; }
.card .k { color: var(--faint); font-size: 11px; letter-spacing: .6px; text-transform: uppercase; }
.card .v { color: #c6d0da; font: 400 11.5px var(--mono); text-align: right; word-break: break-all; }
.steprow { display: flex; gap: 9px; align-items: baseline; padding: 6px 4px; font-size: 12.5px;
  border-radius: 7px; }
.steprow:hover { background: var(--panel2); }
.steprow .n { font: 600 10.5px var(--mono); color: var(--acc); flex: none; width: 40px; }
.steprow .i { color: #c6d0da; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.steprow .t { color: var(--faint); font: 400 10.5px var(--mono); flex: none; }
.empty { color: var(--faint); text-align: center; padding: 34px 0; font-size: 12.5px; }
.empty::before { content: "◇"; display: block; font-size: 20px; margin-bottom: 6px; opacity: .5; }
</style></head><body>
<div id="topbar">
  <span class="logo"></span>
  <div><h1>mac_agent console</h1><div class="sub">multi-session · space-isolated</div></div>
  <span class="spacer"></span>
  <span class="chip" id="srvchip">127.0.0.1</span>
</div>
<div id="app">
<aside>
  <button class="primary" id="new" style="width:100%">＋ 新建会话</button>
  <h2>会话 <span class="n" id="ncount">0</span></h2>
  <div id="sessions"><div class="empty">暂无会话</div></div>
  <h2>新会话配置</h2>
  <div class="field"><span class="cap">名称</span><input type="text" id="cfg-name" placeholder="my-session"></div>
  <div class="field"><span class="cap">首条消息</span><input type="text" id="cfg-first" placeholder="例：用 Chrome 打开知乎热榜…"></div>
  <div id="cfg-form"></div>
</aside>
<main>
  <div id="chathead"><span class="phasedot" id="phdot"></span>
    <span class="t" id="title">选择或创建会话</span>
    <span class="st" id="headst"></span></div>
  <div id="log"><div class="empty">会话输出将实时流式显示在这里</div></div>
  <div id="askbox"></div>
  <div id="chatbar"><input id="msg" placeholder="输入消息回车发送（ask_user 答复也在这里）">
    <button class="primary" id="send">发送</button><button class="danger" id="stop">停止</button></div>
</main>
<section>
  <h2>会话状态</h2>
  <div class="card">
    <div class="row"><span class="k">status</span><span class="v" id="st-status">—</span></div>
    <div class="row"><span class="k">steps</span><span class="v" id="st-steps">—</span></div>
    <div class="row"><span class="k">run dir</span><span class="v" id="st-run">—</span></div>
    <div class="row"><span class="k">space anchor</span><span class="v" id="st-anchor">—</span></div>
  </div>
  <h2>步骤时间线</h2>
  <div id="steps"><div class="empty">—</div></div>
</section>
</div>
<script>
let schema = [], current = null, es = null, lastSeq = -1, reasonEl = null;
const $ = id => document.getElementById(id);
const GROUPS = [
  ["模型与上下文", ["llm_protocol", "reasoning_effort", "history_turns", "compaction_trigger_tokens", "compaction_trigger_steps", "compaction_max_tokens"]],
  ["执行与工具", ["bash", "todo", "intent", "multi_task", "record_model_requests", "max_steps", "api_timeout", "api_retries", "parse_retries"]],
  ["观察与截图", ["img_keep_n", "img_interval", "micro_compaction", "micro_trigger_steps", "micro_keep_turns", "micro_interval"]],
  ["停滞保护", ["stall_warn_at", "stall_stop_at"]],
];
async function loadSchema() {
  schema = await (await fetch('/api/schema')).json();
  const by = Object.fromEntries(schema.map(f => [f.dest, f]));
  $('cfg-form').innerHTML = GROUPS.map(([name, dests]) => {
    const items = dests.filter(d => by[d]).map(d => {
      const f = by[d];
      if (f.kind === 'bool') return `<label class="switch"><span>${f.dest}</span>
        <span><input type="checkbox" data-dest="${f.dest}" ${f.default ? 'checked' : ''}>
        <span class="track" onclick="this.previousElementSibling.click()"></span></span></label>`;
      if (f.kind === 'choice') return `<div class="field"><span class="cap"><b>${f.dest}</b> · ${f.help}</span>
        <select data-dest="${f.dest}">${f.choices.map(c => `<option ${c === f.default ? 'selected' : ''}>${c}</option>`).join('')}</select></div>`;
      return `<div class="field"><span class="cap"><b>${f.dest}</b></span>
        <input type="number" step="any" data-dest="${f.dest}" value="${f.default}" title="${f.help}"></div>`;
    }).join('');
    return `<details class="group" ${name === '推理与上下文' ? 'open' : ''}>
      <summary>${name}</summary><div class="body grid2">${items}</div></details>`;
  }).join('');
}
function readConfig() {
  const cfg = {};
  document.querySelectorAll('#cfg-form [data-dest]').forEach(el => {
    const f = schema.find(s => s.dest === el.dataset.dest);
    if (f.kind === 'bool') cfg[el.dataset.dest] = el.checked;
    else if (f.kind === 'choice') cfg[el.dataset.dest] = el.value;
    else if (f.kind === 'int') cfg[el.dataset.dest] = parseInt(el.value, 10);
    else cfg[el.dataset.dest] = parseFloat(el.value);
  });
  return cfg;
}
async function refreshSessions() {
  const list = await (await fetch('/api/sessions')).json();
  $('ncount').textContent = list.length;
  $('sessions').innerHTML = list.length ? list.map(s =>
    `<div class="session ${s.status} ${current === s.sid ? 'active' : ''}" data-sid="${s.sid}">
       <div class="name"><span class="dot"></span>${s.name}</div>
       <div class="meta">${s.status}${s.run_dir ? ' · run✓' : ''}</div></div>`).join('')
    : '<div class="empty">暂无会话</div>';
  document.querySelectorAll('.session').forEach(el => el.onclick = () => select(el.dataset.sid));
}
function closeReason() { reasonEl = null; }
function pushEvent(ev) {
  ev = {...ev, type: ev.kind || ev.type};
  const log = $('log');
  if (log.querySelector('.empty')) log.innerHTML = '';
  if (ev.type === 'reasoning') {
    if (!reasonEl) {
      reasonEl = document.createElement('details');
      reasonEl.className = 'reasoning';
      reasonEl.innerHTML = '<summary>reasoning · 点击展开</summary><div class="body"></div>';
      log.appendChild(reasonEl);
    }
    reasonEl.querySelector('.body').textContent += ev.text.replace(/^\[Step \d+\] reasoning: /, '') + '\n';
    return;
  }
  closeReason();
  const div = document.createElement('div');
  div.className = 'ev ' + (ev.type === 'stdout' ? 'stdout' : ev.type);
  div.textContent = ev.text ?? JSON.stringify(ev);
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}
function select(sid) {
  current = sid; lastSeq = -1; closeReason();
  location.hash = 's=' + sid;
  if (es) es.close();
  $('log').innerHTML = '';
  es = new EventSource(`/api/sessions/${sid}/stream`);
  es.onmessage = e => {
    const raw = JSON.parse(e.data), ev = {...raw, type: raw.kind || raw.type};
    if (ev.seq !== undefined) { if (ev.seq <= lastSeq) return; lastSeq = ev.seq; }
    if (['snapshot', 'resync_snapshot'].includes(ev.type)) {
      const state = ev.state || {}; $('log').innerHTML = ''; closeReason();
      $('st-status').textContent = state.phase || '—';
      $('st-steps').textContent = state.step ?? '—';
      const question = state.question;
      $('askbox').style.display = question ? 'block' : 'none';
      $('askbox').textContent = question ? question.question || question.text || '' : '';
      const report = state.recovery_report;
      if (report) pushEvent({type:'recovery_report', text:`历史动作对账（只读）· 共 ${report.total} 项 · 结果未知 ${report.unknown} · 有回执未验证 ${report.unverified}`});
      if (state.last_intent) pushEvent({...state.last_intent, type:'intent', kind:'intent'});
      if (state.terminal) pushEvent(state.terminal);
      return;
    }
    if (ev.type === 'state') {
      $('st-status').textContent = ev.status ?? '—';
      $('st-steps').textContent = ev.steps ?? '—';
      $('st-run').textContent = ev.run_dir ? ev.run_dir.split('/').pop() : '—';
      $('headst').textContent = `${ev.status ?? ''} · steps ${ev.steps ?? 0}`;
      $('phdot').className = 'phasedot ' + (['exited', 'stopped'].includes(ev.status) ? 'exited' : 'running');
      $('askbox').style.display = ev.ask_user ? 'block' : 'none';
      $('askbox').textContent = ev.ask_user ? '⚠ Agent 询问：' + ev.ask_user : '';
      $('stop').disabled = ['exited', 'stopped'].includes(ev.status);
      refreshSessions();
      fetch(`/api/sessions/${sid}/history`).then(r => r.json()).then(h => {
        $('steps').innerHTML = (h.steps || []).length ? h.steps.map(s =>
          `<div class="steprow"><span class="n">step${s.step}</span>
           <span class="i">${s.intent ?? ''}</span><span class="t">${s.plan}s/${s.exec}s</span></div>`).join('')
          : '<div class="empty">—</div>';
      });
      return;
    }
    if (ev.type === 'status') {
      $('stop').disabled = ['exited', 'stopped'].includes(ev.status);
      $('st-status').textContent = ev.status;
    }
    if (ev.type === 'space') $('st-anchor').textContent = ev.anchor ?? '（降级：逻辑隔离）';
    pushEvent(ev);
  };
  refreshSessions();
}
$('new').onclick = async () => {
  const r = await fetch('/api/sessions', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: $('cfg-name').value, first_message: $('cfg-first').value || '你好', config: readConfig()})});
  const s = await r.json();
  $('cfg-name').value = ''; $('cfg-first').value = '';
  select(s.sid);
};
$('send').onclick = async () => {
  if (!current || !$('msg').value) return;
  await fetch(`/api/sessions/${current}/message`, {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: $('msg').value})});
  $('msg').value = '';
};
$('msg').onkeydown = e => { if (e.key === 'Enter') $('send').onclick(); };
$('stop').onclick = () => current && fetch(`/api/sessions/${current}/stop`, {method: 'POST'});
loadSchema(); refreshSessions(); setInterval(refreshSessions, 5000);
</script></body></html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="mac_agent 本地控制台")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mac_agent console: http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
