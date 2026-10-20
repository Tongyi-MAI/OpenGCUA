#!/usr/bin/env python3
"""mac_agent 单会话录制工作台（demo_hub）。

`python demo_hub.py` 打开一个页面即可完成一场 demo：
- 左：getDisplayMedia 选择 Agent 操作屏并实时预览；
- 中：实时 HUD——STEP / INTENT / THINKING / CODE；
- 右：对话流与指令输入框；
- 录制：单独捕获整个工作台标签页，优先 MP4，包含左侧屏幕、中间 HUD 与右侧对话。

会话进程与 SSE 复用 console_server.Session；视图模型复用 demo_hud.build_hud_state。
所有画面只在本机合成，不上传。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

import console_server  # noqa: E402
import demo_hud  # noqa: E402
from agent_events import HUB_EVENT_PREFIX, decode_hub_event  # noqa: E402
from mobile_bridge import MOBILE_ROOT, SERVER_VERSION, MobileBridge  # noqa: E402


class HubSession(console_server.Session):
    """单会话变体：延迟到首条指令再启动；不做 Space 锚定、不限制 foreground（录制要看真实操作）。"""

    def push(self, event: dict) -> None:
        if event.get("type") == "model_status":
            self.model_request = dict(event)
        super().push(event)

    def _push_line(self, text: str) -> None:
        if text.startswith(HUB_EVENT_PREFIX):
            try:
                event = decode_hub_event(text.rstrip("\n"))
            except ValueError:
                return
            if event is not None:
                self.push(event)
            return
        super()._push_line(text)

    def _create_space_anchor(self) -> None:  # 录制场景需要看到真实前台操作
        self.push({"type": "space", "anchor": None, "note": "录制模式：不做 Space 锚定，前台操作可见"})

    def start(self) -> None:
        with self._lifecycle_lock:
            self.model_request = None
            self._close_transport_locked()
            self.fifo = self._new_fifo_path()
            os.mkfifo(self.fifo)
            self._fifo_fd = os.open(self.fifo, os.O_RDWR)
            read_fd = os.open(self.fifo, os.O_RDONLY)
            if not self.config.get("background_mode", True):
                self._create_space_anchor()
            if str(getattr(self, "hub_config", {}).get("agent_mode")) == "gui-only":
                # GUI-only 基线 agent（原生 pyautogui，无 SDK/无 shell）：常驻 stdin 多轮会话。
                cmd = [sys.executable, str(REPO / "gui_agent.py"), "--countdown", "0",
                       self.first_message]
                self.gui_coalesce = True  # 控制台层把 [Think]/[Action 块合并成单条气泡
            else:
                self.gui_coalesce = False
                cmd = [sys.executable, str(REPO / "mac_agent.py"), "--startup-observation", "none",
                       self.first_message]
                cmd += self._config_args()
                if getattr(self, "config_path", None):  # 让 spawn 的 agent 读同一份 config（含可选 api_key）
                    cmd += ["--config", str(self.config_path)]
            env = {**os.environ, "MAI_CONSOLE_SESSION": self.sid}
            if not self.gui_coalesce:
                env["MAC_AGENT_HUB_EVENTS"] = "1"
                env.pop("GUI_AGENT_HUB_EVENTS", None)
            else:
                env.pop("MAC_AGENT_HUB_EVENTS", None)
                env["GUI_AGENT_HUB_EVENTS"] = "1"
            serial = getattr(self, "hub_config", {}).get("mobile_serial")
            env.pop("GENERALIST_ANDROID_SERIAL", None)
            if serial and not self.gui_coalesce:
                env["GENERALIST_ANDROID_SERIAL"] = serial
            try:
                proc = subprocess.Popen(
                    cmd, stdin=read_fd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env=env, cwd=str(REPO), text=True, bufsize=1)
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

    def submit(self, text: str) -> str:
        """首条指令启动 agent；之后按终端语义投递到 stdin。"""
        text = (text or "").strip()
        if not text:
            return "empty"
        # 进程可能已自然结束或被外部 kill：此时 fifo 没有读者，投递会被静默丢弃，
        # 因此把这条指令当作新会话的首个任务重新拉起，而不是假装发送成功。
        with self._lifecycle_lock:
            if self.proc is None or self.proc.poll() is not None:
                if self.proc is not None:
                    self.push({"type": "stdout", "text": "[新会话] 上一个 agent 已结束，用这条指令重新开始"})
                self._reset_for_restart()
                self.first_message = text
                self.push({"type": "user", "text": text})
                self.start()
                return "started"
            try:
                self.send(text)
                return "sent"
            except (OSError, RuntimeError):
                # poll() can still report alive immediately before the FIFO reader
                # disappears. Treat a failed write as the same restart boundary.
                self.push({"type": "stdout", "text": "[新会话] agent 输入通道已关闭，用这条指令重新开始"})
                self._reset_for_restart()
                self.first_message = text
                self.push({"type": "user", "text": text})
                self.start()
                return "started"

    def _reset_for_restart(self) -> None:
        """重启前清掉上一会话的进程与产物引用，避免 HUD 继续读旧 run。"""
        with self._lifecycle_lock:
            self.model_request = None
            if self.proc is not None and self.proc.poll() is None:
                self._stop_process_locked()
            else:
                self._close_transport_locked()
            self.proc = None
            self.run_dir = ""
            self.status = "idle"

    def abort(self) -> bool:
        """中止本轮（SIGUSR1）：只取消当前规划，会话进程与上下文保留。"""
        if self.proc is None or self.proc.poll() is not None:
            return False
        try:
            os.kill(self.proc.pid, signal.SIGUSR1)
        except OSError:
            return False
        self.push({"type": "stdout", "text": "[中止] 已请求停止本轮（上下文保留）"})
        return True

    def stop(self) -> None:  # 覆盖父类：无锚定窗需要关闭
        with self._lifecycle_lock:
            self.status = "stopping"
            self._stop_process_locked()
            self.status = "stopped"
        self.push({"type": "status", "status": "stopped"})


class Hub:
    def __init__(self, config: dict) -> None:
        self.schema = console_server.build_schema()
        defaults = {"background_mode": not bool(config.get("demo_mode"))}
        self.session = HubSession("hub", "demo", {**defaults, **config}, "")
        self.session.hub_config = {}
        self.mobile_bridge = None
        self._target_lock = threading.Lock()

    def submit(self, text: str) -> str:
        with self._target_lock:
            return self.session.submit(text)

    def set_mobile_target(self, serial: str) -> None:
        with self._target_lock:
            self._set_mobile_target(serial)

    def _set_mobile_target(self, serial: str) -> None:
        if not isinstance(serial, str) or serial != serial.strip() or any(c.isspace() for c in serial):
            raise ValueError("An explicit ADB serial without whitespace is required")
        if serial and not self.mobile_bridge:
            raise ValueError("Start the studio with --mobile to bind an Android device")
        if serial and self.session.hub_config.get("agent_mode") == "gui-only":
            raise ValueError("Phone SDK control requires our harness; gui-only supports PC only")
        current = self.session.hub_config.get("mobile_serial", "")
        if current != serial and self.session.proc is not None and self.session.proc.poll() is None:
            raise RuntimeError("End the agent session before changing its phone target")
        self.session.hub_config["mobile_serial"] = serial

    def state(self) -> dict:
        run_dir = self.session.run_dir
        if not run_dir:
            # gui-only 等无 traj 的 agent 也必须有 session_status，否则前端按钮/发送锁全死。
            return {"ok": False, "error": "尚未启动：在右侧输入第一条指令即可开始",
                    "task": self.session.first_message or "", "status": self.session.status,
                    "session_status": self.session.status, "phase": None, "answer": None,
                    "agent_mode": getattr(self.session, "hub_config", {}).get("agent_mode", "mai-ui"),
                    "background_mode": bool(self.session.config.get("background_mode")),
                    "demo_mode": bool(self.session.config.get("demo_mode"))}
        traj = Path(run_dir) / "traj.json"
        try:
            data = json.loads(traj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"trajectory 未就绪：{exc}", "status": self.session.status}
        state = demo_hud.build_hud_state(data, traj)
        state["session_status"] = self.session.status
        state["model_request"] = getattr(self.session, "model_request", None)
        if self.session.status in {"exited", "stopped"} and data.get("status") == "running":
            state["phase"] = "attention"
        # poll 兜底通道：SSE 丢 Agent> 事件时由这里补渲染回答气泡。
        rounds = data.get("rounds") or data.get("tasks") or []
        state["answer"] = (rounds[-1].get("answer") if rounds else None)
        state["agent_mode"] = getattr(self.session, "hub_config", {}).get("agent_mode", "mai-ui")
        state["background_mode"] = bool(self.session.config.get("background_mode"))
        state["demo_mode"] = bool(self.session.config.get("demo_mode"))
        return state


HUB: Hub | None = None


def _persist_mode(key: str, value: Any) -> None:
    """把 UI 开关写回 config 文件，hub 重启后不丢用户选择。"""
    path = getattr(HUB.session, "config_path", None) if HUB else None
    if not path:
        return
    try:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        data[key] = value
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 - 持久化失败不影响本次会话
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str) -> None:
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # demo_hub 是单文件实时工作台；禁止浏览器复用旧 HTML/JS，否则服务重启后仍会运行旧录制逻辑。
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        assert HUB is not None
        route = self.path.split("?", 1)[0]
        if route in ("/", "/panel"):
            self._html(HUB_HTML)
        elif route == "/api/mobile/config":
            bridge = HUB.mobile_bridge
            alive = bridge is not None and bridge.proc.poll() is None
            self._json({"ws_url": bridge.ws_url if alive else None,
                        "server_version": SERVER_VERSION,
                        "serial": HUB.session.hub_config.get("mobile_serial", "")})
        elif route in ("/mobile/mobile.js", "/mobile/scrcpy-server"):
            asset = (MOBILE_ROOT / "dist/mobile.js" if route.endswith(".js") else
                     MOBILE_ROOT / f"vendor/scrcpy-server-v{SERVER_VERSION}")
            if not asset.is_file():
                self._json({"error": "Mobile assets missing; run npm ci, npm run setup, npm run build in packages/harness/runtime/mobile"}, 404)
                return
            body = asset.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript" if route.endswith(".js") else "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._json(HUB.state())
        elif self.path == "/api/schema":
            self._json(HUB.schema)
        elif self.path == "/api/events":
            session = HUB.session
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            backlog, subscriber = session.subscribe_events()
            try:
                for event in backlog:
                    self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        event = subscriber.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                session.unsubscribe_events(subscriber)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        assert HUB is not None
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/mobile/target":
            # Match bridge origin checks: remote websites cannot retarget the SDK.
            origin = self.headers.get("Origin")
            expected = {f"http://{host}:{self.server.server_port}" for host in ("127.0.0.1", "localhost")}
            if origin not in expected:
                self._json({"error": "Phone target must be selected from this studio"}, 403)
                return
            try:
                HUB.set_mobile_target(body.get("serial"))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except RuntimeError as exc:
                self._json({"error": str(exc)}, 409)
            else:
                _persist_mode("mobile_serial", body["serial"])
                self._json({"ok": True, "serial": body["serial"]})
        elif self.path == "/api/message":
            result = HUB.submit(str(body.get("text") or ""))
            self._json({"ok": result != "empty", "result": result})
        elif self.path == "/api/abort":
            self._json({"ok": HUB.session.abort()})
        elif self.path == "/api/stop":
            HUB.session.stop()
            self._json({"ok": True})
        elif self.path == "/api/reset":
            # 结束会话并清掉旧 run 引用，HUD 不再读旧轨迹；下一条指令自动开新会话。
            HUB.session.stop()
            HUB.session._reset_for_restart()
            HUB.session.push({"type": "reset", "text": "[新会话] 已结束上一个会话，输入指令开始新的"})
            self._json({"ok": True})
        elif self.path == "/api/bgmode":
            HUB.session.config["background_mode"] = bool(body.get("on"))
            if HUB.session.config["background_mode"]:
                HUB.session.config["demo_mode"] = False
            _persist_mode("background_mode", HUB.session.config["background_mode"])
            _persist_mode("demo_mode", HUB.session.config.get("demo_mode", False))
            self._json({"ok": True, "background_mode": HUB.session.config["background_mode"],
                        "demo_mode": HUB.session.config.get("demo_mode", False)})
        elif self.path == "/api/agentmode":
            mode = str(body.get("mode") or "mai-ui")
            if mode not in ("mai-ui", "gui-only"):
                mode = "mai-ui"
            HUB.session.hub_config["agent_mode"] = mode
            _persist_mode("agent_mode", mode)
            if HUB.session.proc is not None and HUB.session.proc.poll() is None:
                HUB.session.stop()
                HUB.session._reset_for_restart()
                HUB.session.push({"type": "reset", "text": f"[模式切换] 已结束旧会话；下一条指令以 {mode} 启动"})
            self._json({"ok": True, "agent_mode": mode})
        elif self.path == "/api/demomode":
            HUB.session.config["demo_mode"] = bool(body.get("on"))
            if HUB.session.config["demo_mode"]:
                HUB.session.config["background_mode"] = False
            _persist_mode("demo_mode", HUB.session.config["demo_mode"])
            _persist_mode("background_mode", HUB.session.config.get("background_mode", False))
            self._json({"ok": True, "demo_mode": HUB.session.config["demo_mode"],
                        "background_mode": HUB.session.config.get("background_mode", False)})
        else:
            self._json({"error": "not found"}, 404)


HUB_HTML = r"""
<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Demo Studio</title>
<style>
:root {
  color-scheme: dark;
  --bg: #04080a; --card: #0a1418; --card2: #0c1a1f; --line: #122a30; --line2: #1b3b43;
  --ink: #d8ecef; --mut: #7fa3aa; --faint: #4d6f76;
  --cy: #22d3ee; --te: #2dd4bf; --warn: #fbbf24; --err: #fb7185;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --sans: -apple-system, "SF Pro Text", "PingFang SC", "Helvetica Neue", Arial, sans-serif;
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { display: flex; flex-direction: column; background: radial-gradient(1200px 700px at 16% -12%, #08181d, var(--bg)) fixed;
  color: var(--ink); font: 14px/1.55 -apple-system, "PingFang SC", sans-serif;
  -webkit-font-smoothing: antialiased; }
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: #12343b; border-radius: 8px; }
.lbl { font: 700 10px/1 var(--mono); letter-spacing: 2px; text-transform: uppercase; color: var(--faint); }
#head { display: flex; align-items: center; gap: 14px; padding: 16px 24px 10px; }
#head h1 { margin: 3px 0 0; font-size: 22px; font-weight: 750; letter-spacing: -.3px; }
#head .tools { margin-left: auto; display: flex; align-items: center; gap: 8px; }
.tbtn { font: 600 12px/1 inherit; color: var(--ink); background: #06222a; border: 1px solid var(--line2);
  border-radius: 9px; padding: 9px 13px; cursor: pointer; transition: .15s; white-space: nowrap; }
.tbtn:hover { border-color: #2a5f6a; background: #082c34; }
.tbtn.on { color: #d9f7ee; background: linear-gradient(180deg, #0d3a3a, #0a2b2c); border-color: #1c5b58; }
.tbtn.warn { color: var(--warn); border-color: #4d3a12; background: #221a08; }
.tbtn:disabled { opacity: .35; cursor: not-allowed; }
.status { display: flex; align-items: center; gap: 7px; font: 700 10px/1 var(--mono);
  letter-spacing: 1.6px; color: var(--mut); margin-left: 4px; }
.status .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--te);
  box-shadow: 0 0 10px var(--te); }
#wrap { display: grid; grid-template-columns: minmax(0,1.15fr) minmax(0,1fr); gap: 14px;
  padding: 0 24px 16px; flex: 1; min-height: 0; }
#head, #foot { flex-shrink: 0; }
#rightpane { display: grid; grid-template-columns: 300px minmax(0,1fr); gap: 14px; min-width: 0; min-height: 0; }
.zone { background: linear-gradient(180deg, #071317, #050f12); border: 1px solid var(--line);
  border-radius: 18px; padding: 13px; display: flex; flex-direction: column; gap: 10px; min-width: 0; min-height: 0; }
#stagebar { display: flex; align-items: center; gap: 10px; }
.badge { font: 700 10px/1 var(--mono); letter-spacing: 1.3px; padding: 6px 10px; border-radius: 7px;
  border: 1px solid var(--line2); color: var(--cy); background: #061a1f; }
#screen { flex: 1; position: relative; border: 1px solid var(--line); border-radius: 14px;
  background: #02080a; overflow: hidden; display: flex; align-items: center; justify-content: center; }
#screen video { width: 100%; height: 100%; object-fit: contain; }
#screen .preview { min-width: 0; min-height: 0; height: 100%; position: relative; }
#pcpreview { flex: 1; }
#phonepreview { flex: 1; display: none; align-items: center; justify-content: center; }
#phonecanvas { width: 100%; height: 100%; object-fit: contain; }
#phonehint { position: absolute; color: var(--faint); text-align: center; padding: 20px; }
.source-label { position: absolute; top: 12px; left: 12px; z-index: 1; color: var(--cy); background: #061a1fcc; border: 1px solid var(--line2); border-radius: 7px; padding: 4px 8px; font: 700 10px var(--mono); }
#screen[data-layout="mobile"] #pcpreview { display: none; }
#screen[data-layout="mobile"] #phonepreview { display: flex; }
#screen[data-layout="both"] #phonepreview { display: flex; flex: .25; border-left: 1px solid var(--line); }
#mobilebar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
#mobilebar select { min-width: 0; max-width: 280px; color: var(--ink); background: var(--card); padding: 6px; border: 1px solid var(--line2); border-radius: 6px; }
#phonestatus { color: var(--mut); font-size: 12px; flex-basis: 100%; }
#head .tools { flex-wrap: wrap; justify-content: flex-end; }
#screenhint { position: absolute; text-align: center; color: var(--faint); padding: 20px; }
#screenhint { inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
#screenhint b { display: block; font-size: 16px; color: #9fc4ca; margin-bottom: 6px; }
#rec { position: absolute; top: 12px; left: 12px; display: none; align-items: center; gap: 7px;
  font: 700 10px/1 var(--mono); letter-spacing: 1.5px; color: #fecdd3; background: #2a0d12cc;
  border: 1px solid #5d1f28; border-radius: 999px; padding: 6px 11px; }
#rec.on { display: flex; }
#rec .rd { width: 7px; height: 7px; border-radius: 50%; background: var(--err); animation: blink 1.1s infinite; }
@keyframes blink { 50% { opacity: .25; } }
#hud { overflow-y: auto; }
.pill { display: inline-flex; align-items: center; gap: 6px; font: 700 9.5px/1 var(--mono);
  letter-spacing: 1.4px; padding: 5px 9px; border-radius: 999px; border: 1px solid var(--line2);
  color: var(--te); background: #06181a; align-self: flex-start; }
.pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.pill.executing::before { animation: blink 1.2s infinite; }
.pill.attention { color: var(--warn); border-color: #4d3a12; }
#steprow { display: flex; align-items: center; gap: 12px; }
#stepnum { font: 750 28px/1 var(--mono); color: var(--cy); letter-spacing: -1px; }
.panel { background: var(--card2); border: 1px solid var(--line); border-radius: 11px; padding: 9px 11px; }
.panel .lbl { color: var(--cy); }
.panel .body { margin-top: 6px; font: 400 11.5px/1.65 var(--mono); color: var(--mut);
  white-space: pre-wrap; word-break: break-word; }
.panel.grow { flex: 1; min-height: 88px; display: flex; flex-direction: column; }
.panel.grow .body { flex: 1; overflow-y: auto; }
.intent { display: flex; gap: 8px; padding: 3px 0; font-size: 12.5px; align-items: baseline; }
.intent .n { font: 700 10.5px var(--mono); color: var(--te); flex: none; }
.intent .x { color: #cfe9ec; }
#intentpanel .body { max-height: 22vh; overflow-y: auto; }
#intents .intent { font-size: 12.5px; }
#code .body { color: #a7e8c9; }
#code .blkhead { font: 700 9.5px var(--mono); letter-spacing: 1.2px; color: var(--faint); margin-top: 6px; }
#chat { display: flex; flex-direction: column; }
#msgs { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 9px; padding: 2px 2px 6px; }
.bub { max-width: 88%; padding: 9px 12px; border-radius: 13px; font-size: 13px; white-space: pre-wrap;
  word-break: break-word; }
.bub.me { align-self: flex-end; background: #0c2f38; border: 1px solid #17505d; color: #cdf1f7;
  border-bottom-right-radius: 5px; }
.bub.ag { align-self: flex-start; background: #0b2a24; border: 1px solid #1d5546; color: #cdf3e4;
  border-bottom-left-radius: 5px; }
.bub.ask { align-self: flex-start; background: #221a08; border: 1px solid #4d3a12; color: #f7e0a8; }
.bub.live { align-self: flex-start; background: #0b2230; border: 1px dashed #2b5a6b; color: #9fd4e4;
  opacity: .9; font-style: italic; }
.bub.think { align-self: stretch; max-width: 100%; background: #0e1626; border: 1px solid #1d2b40;
  border-left: 3px solid #2f6f8f; color: #c3d2e6; font: 400 12.5px/1.62 var(--sans);
  white-space: pre-wrap; border-radius: 10px; padding: 10px 12px; }
.bub.think::before { content: 'THINKING'; display: block; margin-bottom: 6px;
  font: 600 10px/1 var(--mono); letter-spacing: .12em; color: #6fa8c9; }
.bub.out { align-self: flex-start; background: transparent; border: 1px dashed var(--line2);
  color: var(--faint); font: 400 11px var(--mono); opacity: .85; }
.bub.sys { align-self: center; background: transparent; border: 1px dashed var(--line2);
  color: var(--faint); font: 400 11px var(--mono); }
.stepcard { align-self: stretch; max-width: 100%; background: var(--card2); border: 1px solid var(--line);
  border-left: 3px solid var(--cy); border-radius: 11px; padding: 8px 11px; }
.stepcard .h { display: flex; align-items: baseline; gap: 8px; }
.stepcard .sn { font: 700 10px var(--mono); letter-spacing: 1.2px; color: var(--cy); }
.stepcard .ph { font: 700 9.5px var(--mono); letter-spacing: 1.2px; color: var(--faint); }
.stepcard .list { margin-top: 4px; }
.stepcard .row { display: flex; gap: 7px; font-size: 12.5px; padding: 2px 0; align-items: baseline; }
.stepcard .row .n { font: 700 10.5px var(--mono); color: var(--te); flex: none; }
.stepcard .row .x { color: #cfe9ec; white-space: pre-wrap; }
.stepcard.live { border-left-color: var(--te); }
#modelstatus { padding: 9px 10px; border: 1px solid var(--line); border-radius: 8px;
  color: var(--cy); font: 400 12px/1.6 var(--sans); white-space: pre-wrap; }
#modelstatus[data-stage="retrying"], #modelstatus[data-stage="error"] { color: #f5bd62; border-color: #745a30; }
#inputwrap { border-top: 1px solid var(--line); padding-top: 10px; display: flex; flex-direction: column; gap: 8px; }
#cmd { width: 100%; resize: none; height: 68px; font: 400 13px/1.6 -apple-system, sans-serif;
  color: var(--ink); background: #02090b; border: 1px solid var(--line); border-radius: 12px;
  padding: 10px 12px; outline: none; }
#cmd:focus { border-color: var(--cy); box-shadow: 0 0 0 3px #22d3ee22; }
#inputrow { display: flex; gap: 8px; align-items: center; }
#inputrow .hint { font: 400 11px var(--mono); color: var(--faint); margin-right: auto; }
.send { font: 700 12.5px/1 inherit; color: #04211c; background: linear-gradient(135deg, #7de3f0, #34d399);
  border: 0; border-radius: 10px; padding: 10px 16px; cursor: pointer; }
.send:hover { filter: brightness(1.06); }
.send:disabled { opacity: .38; cursor: not-allowed; filter: none; }
body.busy #inputwrap { position: relative; }
body.busy #inputwrap::before { content: "● Agent 执行中，输入已锁定 — 点右上「中止本轮」可打断";
  position: absolute; top: -22px; left: 0; right: 0; font: 700 10.5px/1 var(--mono);
  letter-spacing: .5px; color: var(--warn); }
#cmd:disabled { opacity: .5; cursor: not-allowed; }
#foot { display: flex; justify-content: space-between; padding: 0 24px 12px;
  font-size: 11.5px; color: var(--faint); }
/* OBS Browser Source 叠加层：透明背景、只留 HUD、放大字体、文字带阴影保证任意画面上可读。 */
body.panel { background: transparent !important; }
body.panel #head, body.panel #foot { display: none !important; }
body.panel #wrap { display: block !important; height: 100vh !important; padding: 20px !important; }
body.panel #wrap > .zone:first-child { display: none !important; }
body.panel #rightpane { display: block !important; }
body.panel #chat { display: none !important; }
body.panel #hud { overflow: visible !important; background: transparent !important; border: 0 !important; padding: 0 !important; gap: 14px !important; max-width: 720px !important; }
body.panel .panel { background: rgba(4,12,14,.58) !important; border: 1px solid rgba(34,211,238,.22) !important; border-radius: 14px !important; padding: 12px 15px !important; }
body.panel .panel .body { font-size: 15px !important; line-height: 1.62 !important; color: #d6f0f3 !important; text-shadow: 0 1px 4px rgba(0,0,0,.92) !important; }
body.panel .panel.grow { min-height: 130px !important; }
body.panel #stepnum { font-size: 46px !important; text-shadow: 0 2px 8px rgba(0,0,0,.9) !important; }
body.panel .intent, body.panel #intents .intent { font-size: 16.5px !important; }
body.panel .intent .x { text-shadow: 0 1px 4px rgba(0,0,0,.9) !important; }
body.panel .pill { font-size: 12.5px !important; padding: 7px 13px !important; }
body.panel .lbl { font-size: 11px !important; }
body.panel #intentpanel .body { max-height: 30vh !important; }
</style></head><body>
<script>
// OBS 叠加层模式：/panel 或 ?mode=panel 时只渲染透明 HUD（隐藏视频/聊天/头部）。
if (location.pathname.replace(/\/+$/, '') === '/panel' || /[?&]mode=panel\b/.test(location.search))
  document.body.classList.add('panel');
</script>
<div id="head">
  <div><div class="lbl">local recording workspace</div><h1>Agent Demo Studio</h1></div>
  <div class="tools">
    <button class="tbtn" id="pick">选择屏幕</button>
    <label class="tbtn" style="display:inline-flex;align-items:center;gap:6px;cursor:pointer"><input type="checkbox" id="rechub"> 复刻hub</label>
    <label class="tbtn" style="display:inline-flex;align-items:center;gap:6px;cursor:pointer"><input type="checkbox" id="bgmode"> 后台模式</label>
    <label class="tbtn" style="display:inline-flex;align-items:center;gap:6px;cursor:pointer"><input type="checkbox" id="demomode"> 演示模式</label>
    <label class="tbtn" style="display:inline-flex;align-items:center;gap:6px">Agent
      <select id="agentmode" style="background:#0d222b;color:#cdf1f7;border:1px solid #17505d;border-radius:6px;padding:2px 4px">
        <option value="mai-ui">our harness</option>
        <option value="gui-only">gui-only(pyautogui)</option>
      </select>
    </label>
    <button class="tbtn" id="rectoggle" disabled>● 录制</button>
    <button class="tbtn warn" id="abort" disabled>■ 中止本轮</button>
    <button class="tbtn" id="endsess" disabled>⏻ 结束会话</button>
    <button class="tbtn" id="newsess">＋ 新会话</button>
    <span class="status"><span class="dot" id="condot"></span><span id="conn">HUD READY</span></span>
  </div>
</div>
<div id="wrap">
  <div class="zone">
    <div id="stagebar"><span class="badge" id="outbadge">OUTPUT — × — · 60 FPS</span>
      <span style="flex:1"></span><span class="lbl">gui + cli · live execution</span></div>
    <div id="mobilebar">
      <select id="layout" aria-label="预览与录制布局"><option value="pc">PC</option><option value="mobile">手机</option><option value="both">PC + 手机</option></select>
      <select id="phoneselect" aria-label="Android 手机"><option value="">请选择手机</option></select>
      <button class="tbtn" id="phonerefresh">刷新手机</button>
      <button class="tbtn" id="phoneconnect" disabled>连接手机</button>
      <span id="phonestatus">手机串流初始化中…</span>
    </div>
    <div id="screen" data-layout="pc">
      <div id="pcpreview" class="preview">
      <span class="source-label">PC</span>
      <video id="video" autoplay muted playsinline></video>
      <div id="screenhint"><b>点击右上「选择屏幕」开始预览</b>建议选 Agent 操作的“应用窗口”（非整块屏幕），录制即为 studio 合成视图</div>
      </div>
      <div id="phonepreview" class="preview"><span class="source-label">ANDROID</span><canvas id="phonecanvas" width="0" height="0"></canvas><div id="phonehint">连接 Android 手机开始预览</div></div>
      <div id="rec"><span class="rd"></span><span id="rectime">REC 00:00</span></div>
    </div>
  </div>
  <div id="rightpane">
    <div class="zone" id="hud">
      <span class="pill" id="phase">READY</span>
      <div id="steprow"><div><div id="stepnum">00</div><div class="lbl">step</div></div>
        <div style="flex:1"><div class="lbl" id="phtext">ready</div></div></div>
      <div id="modelstatus" role="status" aria-live="polite" hidden></div>
      <div class="panel" id="intentpanel"><div class="lbl">intent</div>
        <div class="body" id="intents"><div class="intent"><span class="x" style="color:var(--faint)">等待首条指令…</span></div></div></div>
      <div class="panel grow"><div class="lbl">thinking</div><div class="body" id="think">—</div></div>
      <div class="panel grow" id="code"><div class="lbl">code · <span id="codestep">step 00</span> <span id="codestate"></span></div>
        <div class="body" id="codebody">等待 agent 写下本轮代码</div></div>
      <div class="lbl" id="counts" style="text-align:right">0 actions · 0 steps</div>
    </div>
    <div class="zone" id="chat">
      <div class="lbl">conversation</div>
      <div id="msgs"><div class="bub sys">输入指令开始会话（Enter 发送，Shift+Enter 换行）</div></div>
      <div id="inputwrap">
        <textarea id="cmd" placeholder="例：用 Chrome 打开知乎热榜，告诉我第 1 条标题和热度"></textarea>
        <div id="inputrow"><span class="hint">Enter 发送 · Shift+Enter 换行</span>
          <button class="send" id="send">发送</button></div>
      </div>
    </div>
  </div>
</div>
<canvas id="composite" width="1920" height="1080" style="position:fixed;left:-3000px;top:0"></canvas>
<div id="foot"><span>所有画面只在本机合成，不上传。</span><span>中止只停当前一轮，上下文与会话保留。</span></div>
<script>
const $ = id => document.getElementById(id);
let stream = null, recordStream = null, recorder = null, recStart = 0, recTimer = null;
function hasPreview() {
  if (window.mobileStudio && window.mobileStudio.connecting) return false;
  const layout = $('layout').value;
  const pc = layout !== 'mobile' && stream && stream.getVideoTracks()[0].readyState === 'live';
  const phone = layout !== 'pc' && window.mobileStudio && window.mobileStudio.ready;
  return !!(pc || phone);
}
window.studioMediaChanged = () => {
  $('screen').dataset.layout = $('layout').value;
  $('rectoggle').disabled = !hasPreview() && !(recorder && recorder.state === 'recording');
};
window.studioSourcesLocked = () => !!(recorder && recorder.state === 'recording');
$('layout').onchange = window.studioMediaChanged;
function lockSources(locked) {
  for (const id of ['pick', 'layout', 'phoneselect', 'phonerefresh', 'phoneconnect']) $(id).disabled = locked;
}
let hudState = null; const chatLog = []; let compRaf = null;
let lastSeq = -1, lastAsk = '', lastAnswer = '', guiReasoningText = '';
window.__guiStep = -1;
const stepCards = new Map();          // step -> DOM 卡片（聊天流内按步渲染）
const PH = {thinking: 'THINKING', executing: 'EXECUTING', completed: 'COMPLETED', attention: 'ATTENTION'};
const esc = s => (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');

let prevScrollHeight = 0;  // 用上一次内容高度推算“追加前用户是否在底部”，不依赖 scroll 事件
function scrollChat() {
  const b = $('msgs');
  const userAtBottom = b.scrollTop >= prevScrollHeight - b.clientHeight - 40;
  prevScrollHeight = b.scrollHeight;
  if (userAtBottom) b.scrollTop = b.scrollHeight;  // 仅在底部才自动滚；上滑查阅不拉回
}
function clearPlaceholder() {
  const box = $('msgs');
  if (box.children.length === 1 && box.firstChild.classList.contains('sys')) box.innerHTML = '';
}
let liveBubbleEl = null;
function liveEnd() {
  if (liveBubbleEl) { liveBubbleEl.remove(); liveBubbleEl = null; }
  while (chatLog.length && chatLog[chatLog.length - 1].cls === 'live') chatLog.pop();
}
function liveStream(text) {
  if (!liveBubbleEl || !liveBubbleEl.isConnected) {
    clearPlaceholder();
    liveBubbleEl = document.createElement('div');
    liveBubbleEl.className = 'bub live';
    $('msgs').appendChild(liveBubbleEl);
    chatLog.push({cls: 'live', text: ''});
    if (chatLog.length > 40) chatLog.shift();
  }
  liveBubbleEl.textContent = text;
  const last = chatLog[chatLog.length - 1];
  if (last && last.cls === 'live') last.text = text;
  scrollChat();
}
function bubble(cls, text) {
  liveEnd();
  chatLog.push({cls: cls, text: String(text == null ? '' : text)});
  if (chatLog.length > 40) chatLog.shift();
  clearPlaceholder();
  const d = document.createElement('div');
  d.className = 'bub ' + cls;
  d.textContent = text;
  $('msgs').appendChild(d); scrollChat();
}
function stepCard(step) {
  liveEnd();
  let card = stepCards.get(step);
  if (!card) {
    clearPlaceholder();
    card = document.createElement('div');
    card.className = 'stepcard live';
    card.innerHTML = `<div class="h"><span class="sn">STEP ${String(step).padStart(2, '0')}</span>
      <span class="ph">planning…</span></div><div class="list"></div>`;
    $('msgs').appendChild(card); stepCards.set(step, card); scrollChat();
  }
  return card;
}
function renderStep(step, phase, blocks, liveIntent) {
  const card = stepCard(step);
  card.querySelector('.ph').textContent = PH[phase] ? PH[phase].toLowerCase() : (phase || '');
  const list = card.querySelector('.list');
  if (blocks && blocks.length) {
    list.innerHTML = blocks.map(b =>
      `<div class="row"><span class="n">${b.index}.</span><span class="x">${esc(b.intent) || '—'}</span></div>`).join('');
    card.classList.remove('live');
  } else if (liveIntent) {
    list.innerHTML = `<div class="row"><span class="n">·</span><span class="x">${esc(liveIntent)}</span></div>`;
  }
  scrollChat();
}

$('pick').onclick = async () => {
  let selected;
  try { selected = await navigator.mediaDevices.getDisplayMedia({video: {frameRate: 30}, audio: false}); }
  catch (e) { bubble('sys', '屏幕共享已取消'); return; }
  if (stream) stream.getTracks().forEach(tr => tr.stop());
  stream = selected;
  $('video').srcObject = stream;
  $('screenhint').style.display = 'none';
  const track = stream.getVideoTracks()[0];
  const t = track.getSettings();
  $('outbadge').textContent = `OUTPUT ${t.width || '—'} × ${t.height || '—'} · ${t.frameRate ? Math.round(t.frameRate) : '—'} FPS`;
  $('rectoggle').disabled = false;
  bubble('sys', '已选择：' + (track.label || 'screen') + '（显示在左栏）。提示：要录出 studio 合成效果，请选 Agent 操作的“应用窗口”；若选整块屏幕且本工作台在同一屏，左栏会把工作台嵌套进去、看起来像录整屏。');
  if (t.displaySurface === 'browser')
    bubble('sys', '提示：左侧预览建议共享 Agent 操作的整个屏幕或窗口，不要选当前工作台标签页');
  track.onended = () => {
    if (stream !== selected) return;
    stream = null;
    $('video').srcObject = null;
    $('screenhint').style.display = '';
    window.studioMediaChanged();
    bubble('sys', recorder && recorder.state === 'recording'
      ? 'Agent 屏幕预览已结束；工作台仍在录制，可点“停止录制”保存'
      : '屏幕预览已结束');
  };
};
function pickMime() {
  // Chromium 当前支持 MP4/H.264：优先它以获得可靠 duration、QuickTime 兼容与可拖动进度条。
  // 旧浏览器才回退 WebM；回退时会在界面明确提示兼容性限制。
  for (const m of ['video/mp4;codecs=avc1', 'video/mp4',
                   'video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'])
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m;
  return '';
}
$('rectoggle').onclick = async () => {
  if (recorder && recorder.state === 'recording') {
    $('rectoggle').disabled = true;
    $('rectoggle').textContent = '保存中…';
    try { recorder.requestData(); } catch (_) {}
    recorder.stop();
    return;
  }
  if (!hasPreview()) {
    bubble('sys', '请先选择 Agent 正在操作的屏幕，让左侧出现实时画面，再开始录制');
    return;
  }
  if (!window.MediaRecorder) { bubble('sys', '当前浏览器不支持 MediaRecorder，无法录制'); return; }
  // canvas 自合成录制（参考 demo_agent/demo_overlay/studio.js）：每帧把左侧屏幕视频 + HUD + 对话
  // 画到 <canvas>，录制 canvas.captureStream(60)。由 rAF 驱动、帧率恒定，不受标签页限流影响，
  // 不会像标签页捕获那样产出损坏/只有首帧的视频；且复用预览流、无需第二次选择屏幕。
  let output;
  if ($('rechub').checked) {
    // 复刻 hub：捕获 hub 窗口或其所在屏幕（像素级=真实 hub 界面）。
    // 不能用标签页捕获（preferCurrentTab）：标签页一旦切走就被限流→h264 损坏。
    try {
      output = await navigator.mediaDevices.getDisplayMedia({video:{frameRate:30}, audio:false,
        selfBrowserSurface:'include', surfaceSwitching:'exclude'});
    } catch (e) { bubble('sys','复刻hub录制已取消'); return; }
    const surf = output.getVideoTracks()[0].getSettings().displaySurface;
    if (surf === 'browser') {
      output.getTracks().forEach(t => t.stop());
      bubble('sys','复刻hub请勿选“标签页”（切走会损坏）；请重点录制并选 hub 窗口或它所在的屏幕');
      return;
    }
    bubble('sys','复刻hub录制：捕获 hub 窗口/屏幕（像素级）；保持该窗口可见即可，无需前台。');
  } else {
    startComposite();
    output = compCanvas.captureStream(60);
  }
  const mimeType = pickMime();
  const options = {videoBitsPerSecond: 16e6};
  if (mimeType) options.mimeType = mimeType;
  let currentRecorder;
  try { currentRecorder = new MediaRecorder(output, options); }
  catch (e) { bubble('sys', '无法创建录制器：' + e.message); return; }
  const chunks = [];
  let recordError = null;
  currentRecorder.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
  currentRecorder.onerror = e => { recordError = e.error || new Error('录制失败'); bubble('sys', '录制出错：' + recordError.message); };
  currentRecorder.onstop = () => {
    clearInterval(recTimer); $('rec').classList.remove('on');
    $('rectoggle').classList.remove('on'); $('rectoggle').textContent = '● 录制';
    recorder = null;
    lockSources(false);
    window.studioMediaChanged();
    try { output.getTracks().forEach(tr => tr.stop()); } catch (_) {}
    if (recordError || !chunks.length) { bubble('sys', '录制未保存：' + (recordError ? recordError.message : '没有收到视频数据')); return; }
    const blob = new Blob(chunks, {type: currentRecorder.mimeType});
    if (!blob.size) { bubble('sys', '录制未保存：视频文件为空'); return; }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `agent-demo-${Date.now()}.${(currentRecorder.mimeType || '').includes('mp4') ? 'mp4' : 'webm'}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    bubble('sys', '工作台录制已下载：' + a.download + '（' + Math.round(blob.size / 1048576) + ' MB）');
    if (a.download.endsWith('.mp4'))
      bubble('sys', 'MP4 为 fragmented：QuickTime 若只显示第一帧，请用 Chrome/VLC 播放，或 ffmpeg -i "' + a.download + '" -c copy -movflags +faststart out.mp4 转封装');
  };
  try { currentRecorder.start(1000); }
  catch (e) { try { output.getTracks().forEach(tr => tr.stop()); } catch (_) {} bubble('sys', '无法开始录制：' + e.message); return; }
  recorder = currentRecorder;
  lockSources(true);
  bubble('sys', '正在录制（canvas 自合成：屏幕 + HUD + 对话，帧率恒定不损坏）');
  recStart = Date.now(); $('rec').classList.add('on');
  $('rectoggle').classList.add('on'); $('rectoggle').textContent = '■ 停止录制';
  recTimer = setInterval(() => {
    const s = Math.floor((Date.now() - recStart) / 1000);
    $('rectime').textContent = `REC ${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }, 500);
};
$('abort').onclick = async () => {
  $('abort').disabled = true;
  await fetch('/api/abort', {method: 'POST'});
  setSendLock(false);
};
$('endsess').onclick = async () => {
  $('endsess').disabled = true;
  await fetch('/api/stop', {method: 'POST'});
  bubble('sys', '已结束会话；输入下一条指令会开启新会话');
};
$('newsess').onclick = async () => {
  await fetch('/api/reset', {method: 'POST'});
  clearView();
};
function clearView() {
  currentModelRequest = null; modelSessionStatus = 'idle'; renderModelStatus();
  stepCards.clear(); lastAsk = ''; lastAnswer = ''; guiReasoningText = '';
  guiIntentCommitted.clear(); liveIntent.step = null; liveIntent.text = '';
  liveCode.step = null; liveCode.blocks.clear(); liveCode.active = false; liveCode.ended = false;
  window.__guiStep = -1; liveEnd();
  $('msgs').innerHTML = '<div class="bub sys">输入指令开始会话（Enter 发送，Shift+Enter 换行）</div>';
  $('stepnum').textContent = '00';
  $('phase').textContent = 'READY'; $('phase').className = 'pill';
  $('phtext').textContent = 'ready';
  $('intents').innerHTML = '<div class="intent"><span class="x" style="color:var(--faint)">等待首条指令…</span></div>';
  $('think').textContent = '—';
  $('codebody').textContent = '等待 agent 写下本轮代码';
  $('codestep').textContent = 'step 00';
  $('codestate').textContent = '';
  $('counts').textContent = '0 actions · 0 steps';
  setSendLock(false);
}
let sendLocked = false;
function setSendLock(locked) {
  sendLocked = locked;
  $('send').disabled = locked;
  $('cmd').disabled = locked;
  $('send').textContent = locked ? '执行中…' : '发送';
  $('cmd').placeholder = locked
    ? 'Agent 执行中…点右上「中止本轮」可打断后继续输入'
    : '例：用 Chrome 打开知乎热榜，告诉我第 1 条标题和热度';
  document.body.classList.toggle('busy', locked);
}
async function send() {
  if (sendLocked) return;
  const text = $('cmd').value.trim();
  if (!text) return;
  $('cmd').value = '';
  window.__guiOnly = $('agentmode').value === 'gui-only';
  bubble('me', text);
  setSendLock(true);
  await fetch('/api/message', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text})});
}
$('send').onclick = send;
$('cmd').onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } };

// SSE：intent 流式预览进聊天流；Agent 最终回复与中止/结束提示也走这里
const es = new EventSource('/api/events');
let currentModelRequest = null, modelSessionStatus = 'idle';
function modelStatusLabel(request, sessionStatus, now) {
  if (!request || request.stage === 'ready') return '';
  const active = ['waiting', 'generating', 'validating', 'retrying'].includes(request.stage);
  if (active && ['exited', 'stopped', 'idle'].includes(sessionStatus))
    return 'Agent 已结束，当前请求未完成；可以重新发送任务';
  let text = request.text || '';
  if (request.stage === 'waiting' || request.stage === 'generating') {
    const elapsed = Math.max(0, Math.floor((now - Number(request.at_ms || now)) / 1000));
    text += ` · ${request.stage === 'waiting' ? '已等待' : '生成已用'} ${elapsed} 秒`;
    if (request.attempt) text += ` · 第 ${request.attempt}/${request.max_attempts || request.attempt} 次请求`;
    if (request.timeout_seconds) text += ` · 超时上限 ${request.timeout_seconds} 秒`;
    if (request.stage === 'waiting' && elapsed >= 30)
      text += '\n暂未收到模型输出，可继续等待或点击“中止本轮”';
  }
  return text;
}
function renderModelStatus() {
  const text = modelStatusLabel(currentModelRequest, modelSessionStatus, Date.now());
  const box = $('modelstatus');
  box.hidden = !text; box.textContent = text;
  box.dataset.stage = currentModelRequest?.stage || '';
}
function handleModelStatus(ev, notify = true) {
  currentModelRequest = ev;
  renderModelStatus();
  if (notify && ['retrying', 'error', 'cancelled'].includes(ev.stage)) bubble('sys', ev.text);
}
setInterval(renderModelStatus, 1000);
const liveIntent = {step: null, text: ''};
const guiIntentCommitted = new Map();
function renderGuiIntent(step, intent, committed) {
  const shown = Number(step) + 1;
  if (!Number.isInteger(shown) || shown < 1) return;
  const text = String(intent || '').slice(0, 2000);
  if (committed) guiIntentCommitted.set(shown, text);
  renderStep(shown, committed ? 'executing' : 'thinking',
    committed && text ? [{index: 1, intent: text}] : null, committed ? null : text);
  $('intents').innerHTML = text
    ? `<div class="intent"><span class="n">1.</span><span class="x">${esc(text)}</span></div>` : '';
}
function handleGuiPreview(ev) {
  const step = Number(ev.step);
  if (!Number.isInteger(step) || step < 0) return;
  if (ev.preview_type === 'preview_reset') {
    if (liveIntent.step === step) {
      liveIntent.step = null; liveIntent.text = '';
      const card = stepCards.get(step + 1);
      if (card && !guiIntentCommitted.has(step + 1)) card.querySelector('.list').innerHTML = '';
    }
    return;
  }
  if (ev.preview_type === 'intent_start' || liveIntent.step !== step) {
    liveIntent.step = step; liveIntent.text = '';
  }
  if (ev.preview_type === 'intent_delta') {
    liveIntent.text += String(ev.delta || '');
    renderGuiIntent(step, liveIntent.text, false);
  } else if (ev.preview_type === 'intent_end') {
    liveIntent.text = String(ev.intent || liveIntent.text);
    renderGuiIntent(step, liveIntent.text, false);
  } else if (ev.preview_type === 'intent_final') {
    liveIntent.text = String(ev.intent || '');
    renderGuiIntent(step, liveIntent.text, true);
  }
}
const liveCode = {step: null, blocks: new Map(), active: false, ended: false};
function codeForHud(s) {
  const blocks = s.blocks || [];
  const committed = liveCode.active && liveCode.ended &&
    Number(s.blocks_step) === liveCode.step && blocks.length > 0;
  if (committed) liveCode.active = false;
  if (liveCode.active && (s.blocks_step == null || liveCode.step >= Number(s.blocks_step))) {
    return {step: liveCode.step, preview: true,
      text: [...liveCode.blocks].map(([index, code]) => `block ${index}\n${code}`).join('\n')};
  }
  return {step: s.blocks_step == null ? (s.step == null ? 0 : s.step) : s.blocks_step,
    preview: false, text: blocks.map(b => b.code || '').join('\n')};
}
function renderCodePanel(s) {
  if (window.__guiOnly) {
    const actions = s.gui_actions || [];
    const step = s.gui_actions_step == null ? Number(s.step ?? 0) : Number(s.gui_actions_step);
    $('codestep').textContent = 'step ' + String(step + 1).padStart(2, '0');
    $('codestate').textContent = !actions.length && s.phase === 'thinking' ? '· 生成中' : '';
    $('codebody').innerHTML = actions.length
      ? actions.map((a, i) => `<div class="blkhead">action ${i + 1} · ${esc(a.status || 'pending')}</div>${esc(a.source || '')}`).join('\n')
      : '等待 agent 生成动作';
    return;
  }
  const state = codeForHud(s);
  const shown = window.__guiOnly ? Number(state.step) + 1 : Number(state.step);
  $('codestep').textContent = 'step ' + String(shown).padStart(2, '0');
  $('codestate').textContent = state.preview ? (liveCode.ended ? '· 待校验' : '· 生成中') : '';
  if (state.preview) {
    $('codebody').textContent = state.text || '等待 agent 写下本轮代码';
  } else {
    const blocks = s.blocks || [];
    $('codebody').innerHTML = blocks.length
      ? blocks.map(b => `<div class="blkhead">block ${b.index}</div>${esc(b.code)}`).join('')
      : '等待 agent 写下本轮代码';
  }
}
function handleCodePreview(ev) {
  if (ev.type === 'code_reset') {
    if (liveCode.step === null || Number(ev.step) >= liveCode.step) {
      liveCode.step = null; liveCode.blocks.clear(); liveCode.active = false; liveCode.ended = false;
    }
  } else {
    const step = Number(ev.step), index = Number(ev.block_index);
    if (!Number.isInteger(step) || !Number.isInteger(index) || index < 1) return;
    if (liveCode.step !== step) { liveCode.step = step; liveCode.blocks.clear(); }
    liveCode.active = true;
    if (ev.type === 'code_start') { liveCode.blocks.set(index, ''); liveCode.ended = false; }
    if (ev.type === 'code_delta')
      liveCode.blocks.set(index, (liveCode.blocks.get(index) || '') + String(ev.text || ''));
    if (ev.type === 'code_end') liveCode.ended = true;
  }
  renderCodePanel(hudState || {});
}
// 页面加载时按服务端配置回显开关，避免 UI 与服务端状态不一致。
fetch('/api/state', {cache: 'no-store'}).then(r => r.json()).then(s => {
  if (s.agent_mode) $('agentmode').value = s.agent_mode;
  $('bgmode').checked = !!s.background_mode;
  $('demomode').checked = !!s.demo_mode;
}).catch(() => {});
$('bgmode').onchange = () => { $('demomode').checked = false;
  fetch('/api/bgmode', {method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({on: $('bgmode').checked})}).then(r => r.json())
  .then(j => bubble('sys', '后台工作模式：' + (j.background_mode ? '开启（任务浏览器有界面、按需启动；优先 DOM/AX；新会话生效）' : '关闭（新会话生效）'))); };
$('agentmode').onchange = () => { window.__guiOnly = $('agentmode').value === 'gui-only';
  fetch('/api/agentmode', {method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({mode: $('agentmode').value})}).then(r => r.json())
  .then(j => bubble('sys', 'Agent 模式：' + j.agent_mode + (j.agent_mode === 'gui-only'
    ? '（gui-only=原生 pyautogui 基线；新会话生效）' : '（新会话生效）'))); };
$('demomode').onchange = () => { $('bgmode').checked = false;
  fetch('/api/demomode', {method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({on: $('demomode').checked})}).then(r => r.json())
  .then(j => bubble('sys', '演示/前台模式：' + (j.demo_mode ? '开启（浏览器可见+前台真实键鼠优先，适合录制；新会话生效）' : '关闭（新会话生效）'))); };
es.onmessage = e => {
  const ev = JSON.parse(e.data);
  if (ev.seq !== undefined) { if (ev.seq <= lastSeq) return; lastSeq = ev.seq; }
  const text = ev.text || '';
  if (ev.type === 'model_status') {
    modelSessionStatus = 'running'; handleModelStatus(ev); return;
  }
  if (ev.type === 'skill_loaded') {
    bubble('sys', 'Skill loaded: ' + String(ev.name || 'unknown')); return;
  }
  if (['code_reset', 'code_start', 'code_delta', 'code_end'].includes(ev.type)) {
    handleCodePreview(ev); return;
  }
  if (ev.type === 'gui_turn_start') {
    guiReasoningText = ''; liveEnd(); window.__guiStep = -1;
    $('intents').innerHTML = '';
    $('phase').textContent = 'THINKING'; $('phase').className = 'pill thinking';
    $('phtext').textContent = 'thinking'; $('conn').textContent = 'AGENT RUNNING';
    setSendLock(true);
    return;
  }
  if (ev.type === 'gui_reasoning_delta') {
    guiReasoningText += String(ev.delta || '');
    $('think').textContent = guiReasoningText.slice(-1400);
    $('phase').textContent = 'THINKING'; $('phase').className = 'pill thinking';
    $('phtext').textContent = 'thinking';
    return;
  }
  if (ev.type === 'stream') { liveStream(ev.text); return; }
  if (ev.type === 'warn') { bubble('sys', String(ev.text || '').slice(0, 600)); return; }
  if (ev.type === 'gui_step') {
    if (window.__guiStep !== ev.step) {
      window.__guiStep = ev.step; guiReasoningText = ''; liveEnd();
      $('intents').innerHTML = '';
    }
    const shown = Number(ev.step) + 1;
    $('stepnum').textContent = String(shown).padStart(2, '0');
    $('codestep').textContent = 'step ' + String(shown).padStart(2, '0');
    $('phase').textContent = 'THINKING'; $('phase').className = 'pill thinking';
    $('phtext').textContent = 'thinking';
    if (window.__guiOnly) renderStep(shown, 'thinking', null, null);
    return;
  }
  if (ev.type === 'gui_preview') {
    if (window.__guiOnly) handleGuiPreview(ev);
    return;
  }
  if (ev.type === 'gui_turn_complete') {
    liveEnd();
    const ok = ev.status === 'success';
    $('phase').textContent = ok ? 'COMPLETED' : 'ATTENTION';
    $('phase').className = 'pill ' + (ok ? 'completed' : 'attention');
    $('phtext').textContent = ok ? 'completed' : 'attention';
    $('conn').textContent = ok ? 'READY · 本轮完成，可发下一条' : '需要注意 · 见对话';
    setSendLock(false);
    return;
  }
  if (ev.type === 'answer') {
    guiReasoningText = '';
    lastAnswer = text; bubble('ag', text); setSendLock(false);
    $('phase').textContent = 'COMPLETED'; $('phase').className = 'pill completed';
    $('phtext').textContent = 'completed';
    return;
  }
  if (ev.type === 'status' && (ev.status === 'exited' || ev.status === 'stopped')) {
    modelSessionStatus = ev.status; renderModelStatus();
    setSendLock(false);
  }
  // gui-only 基线的块级渲染；mac_agent 仍走下方的 intent 预览/step 卡片老路径，不受影响。
  if (window.__guiOnly) {
    if (ev.type === 'gui_intent') {
      const step = Number(window.__guiStep);
      if (!guiIntentCommitted.has(step + 1)) renderGuiIntent(step, ev.text, true);
      $('phase').textContent = 'EXECUTING'; $('phase').className = 'pill executing';
      $('phtext').textContent = 'executing';
      return;
    }
    if (ev.type === 'reasoning') {
      $('think').textContent = String(text).slice(-1400);
      $('phase').textContent = 'THINKING'; $('phase').className = 'pill thinking';
      $('phtext').textContent = 'thinking';
      return;
    }
    if (ev.type === 'intent') {
      const box = $('intents');
      if (!box.children.length) {  // 模型没给 <intent> 时用动作名兜底一行
        const label = (text.match(/\] ([a-z_]+\.[A-Za-z_]+)/) || [])[1] || text.slice(0, 60);
        renderGuiIntent(Number(window.__guiStep), label, true);
      }
      $('phase').textContent = 'EXECUTING'; $('phase').className = 'pill executing';
      $('phtext').textContent = 'executing';
      return;
    }
    if (ev.type === 'stdout') { bubble('out', String(text).slice(0, 600)); return; }
  }
  const m = text.match(/^\[Step (\d+)\] intent \d+（预览）: (.*)$/);
  if (m) {
    const step = parseInt(m[1], 10);
    liveIntent.step = step; liveIntent.text = m[2];
    renderStep(step, 'thinking', null, m[2]);
    return;
  }
  if (/^\s*Agent>/.test(text)) { const ans = text.replace(/^\s*Agent>\s*/, ''); lastAnswer = ans; bubble('ag', ans); setSendLock(false); return; }
  if (ev.type === 'reset') { clearView(); bubble('sys', text.trim()); return; }
  if (ev.type === 'response_rejected') { bubble('sys', '模型输出格式异常（' + (ev.error || 'parse error') + '），已自动重试'); return; }
  if (ev.type === 'output_salvaged') {
    const now = Date.now();
    if (!window.__lastSalvage || now - window.__lastSalvage > 5000) {
      window.__lastSalvage = now;
      bubble('sys', '模型输出含多余标签/文字/空块，已自动忽略并继续（salvage，不浪费生成）');
    }
    return;
  }
  if (/\[中止\]|\[新会话\]|会话已结束|已请求停止本轮/.test(text)) bubble('sys', text.trim());
};

async function poll() {
  let s;
  try { s = await (await fetch('/api/state', {cache: 'no-store'})).json(); }
  catch (e) { setTimeout(poll, 1500); return; }
  const running = s.session_status === 'running';
  modelSessionStatus = s.session_status;
  if (s.model_request && (!currentModelRequest || Number(s.model_request.at_ms) >= Number(currentModelRequest.at_ms)))
    handleModelStatus(s.model_request, false);
  else renderModelStatus();
  window.__guiOnly = (s.agent_mode === 'gui-only');
  $('abort').disabled = !running;
  $('endsess').disabled = !running;
  // 只有明确“已结束/空闲”才解锁；启动中 session_status 可能短暂为 null，不能当作结束。
  if (s.session_status === 'exited' || s.session_status === 'idle') setSendLock(false);
  if (!s.ok) {
    $('conn').textContent = running ? 'AGENT STARTING' : 'HUD READY';
    $('condot').style.background = running ? 'var(--te)' : 'var(--faint)';
    setTimeout(poll, 1200); return;
  }
  // 左侧 HUD 列
  $('phase').textContent = PH[s.phase] || s.phase.toUpperCase();
  $('phase').className = 'pill ' + s.phase;
  $('phtext').textContent = PH[s.phase] || s.phase;
  const displayStep = window.__guiOnly ? Number(s.step ?? 0) + 1 : Number(s.step ?? 0);
  $('stepnum').textContent = String(displayStep).padStart(2, '0');
  if (hudState && hudState.task_id !== s.task_id) {
    stepCards.clear(); guiIntentCommitted.clear();
    liveIntent.step = null; liveIntent.text = '';
  }
  hudState = s;
  const blocks = s.blocks || [];
  $('intents').innerHTML = blocks.length
    ? blocks.map(b => `<div class="intent"><span class="n">${b.index}.</span><span class="x">${esc(b.intent) || '—'}</span></div>`).join('')
    : `<div class="intent"><span class="x">${esc(s.intent) || '—'}</span></div>`;
  $('think').textContent = s.reasoning ? s.reasoning.slice(-1400) : '—';
  $('think').style.font = '400 12.5px/1.62 var(--sans)';
  renderCodePanel(s);
  const st = s.stats || {};
  $('counts').textContent = `${st.actions ?? 0} actions · ${s.step_count ?? 0} steps · ${st.ax_actions ?? 0} ax / ${st.gui_actions ?? 0} gui`;
  // 右侧聊天流：按步渲染 intent（已完成的步用 blocks 定稿）
  if (window.__guiOnly) {
    for (const item of s.gui_intents || []) {
      if (item.step == null) continue;
      const shown = Number(item.step) + 1;
      if (guiIntentCommitted.get(shown) !== item.intent)
        renderGuiIntent(item.step, item.intent, true);
    }
    if (s.step !== undefined && s.step !== null && !guiIntentCommitted.has(Number(s.step) + 1))
      renderStep(Number(s.step) + 1, s.phase, null,
        liveIntent.step === Number(s.step) ? liveIntent.text : null);
  } else {
    if (s.blocks_step !== undefined && s.blocks_step !== null && blocks.length)
      renderStep(s.blocks_step, s.phase, blocks, null);
    if (s.step !== undefined && s.step !== null && s.step !== s.blocks_step)
      renderStep(s.step, s.phase, null, liveIntent.step === s.step ? liveIntent.text : null);
  }
  if (s.ask_user && s.ask_user !== lastAsk) { lastAsk = s.ask_user; bubble('ask', s.ask_user); }
  if (s.answer && s.answer !== lastAnswer) { lastAnswer = s.answer; bubble('ag', s.answer); }
  if (s.ask_user) setSendLock(false);         // agent 在等你答复：允许输入
  if (s.phase === 'completed') setSendLock(false);  // 本轮终态的 poll 兜底；gui_turn_start 会在新消息真正开始处理时重新锁定
  $('conn').textContent = s.ask_user ? 'ASK_USER 等待答复'
    : (s.phase === 'completed' ? 'READY · 本轮完成，可发下一条'
    : (s.phase === 'attention' ? '需要注意 · 见对话'
    : (running ? 'AGENT RUNNING' : 'SESSION ENDED')));
  $('condot').style.background = (s.ask_user || s.phase === 'attention') ? 'var(--warn)' : (running ? 'var(--te)' : 'var(--faint)');
  setTimeout(poll, 1000);
}
poll();

// ===== canvas 自合成录制：把屏幕视频 + HUD + 对话每帧画到 canvas（参考 demo_agent/demo_overlay/studio.js）=====
const compCanvas = document.getElementById('composite');
const cctx = compCanvas.getContext('2d');
const CC = {panel:'rgba(8,18,22,.9)', line:'rgba(34,211,238,.22)', mut:'#7fa3aa', cy:'#22d3ee', te:'#2dd4bf', warn:'#fbbf24', code:'#a7e8c9'};
function cRound(x,y,w,h,r){const rr=Math.min(r,w/2,h/2);cctx.beginPath();cctx.moveTo(x+rr,y);cctx.arcTo(x+w,y,x+w,y+h,rr);cctx.arcTo(x+w,y+h,x,y+h,rr);cctx.arcTo(x,y+h,x,y,rr);cctx.arcTo(x,y,x+w,y,rr);cctx.closePath();}
function cPanel(x,y,w,h,fill,stroke){cRound(x,y,w,h,14);cctx.fillStyle=fill;cctx.fill();if(stroke){cctx.strokeStyle=stroke;cctx.lineWidth=1.5;cctx.stroke();}}
function cWrap(text,maxW,maxLines){const v=String(text==null?'':text).trim();if(!v)return[];const out=[];let line='';for(const ch of v){if(ch==='\n'){out.push(line||' ');line='';if(out.length>=maxLines)return out;continue;}const nx=line+ch;if(cctx.measureText(nx).width>maxW&&line){out.push(line);line=ch;if(out.length>=maxLines)return out;}else line=nx;}if(line)out.push(line);return out;}
function cLabel(t,x,y,c){cctx.fillStyle=c||CC.mut;cctx.font='700 13px ui-monospace,Menlo,monospace';cctx.fillText(t,x,y);}
function drawScreen(x,y,w,h){
  const layout=$('layout').value,phone=window.mobileStudio;
  if(layout==='both'){
    const gap=12,pw=Math.round((w-gap)*.8); // PC 80%, Android 20%, matching the preview's 4:1 split.
    drawSource($('video'),!!(stream&&$('video').videoWidth),x,y,pw,h,'PC');
    drawSource(phone&&phone.canvas,!!(phone&&phone.ready),x+pw+gap,y,w-pw-gap,h,'ANDROID');
  }else if(layout==='mobile'){
    drawSource(phone&&phone.canvas,!!(phone&&phone.ready),x,y,w,h,'ANDROID');
  }else drawSource($('video'),!!(stream&&$('video').videoWidth),x,y,w,h,'PC');
}
function drawSource(v,ready,x,y,w,h,label){
  cPanel(x,y,w,h,'#020608','rgba(34,211,238,.2)');
  cctx.save();cRound(x,y,w,h,14);cctx.clip();
  if(ready){
    const sr=(v.videoWidth||v.width)/(v.videoHeight||v.height),tr=w/h;let dw,dh,dx,dy;
    if(sr>tr){dw=w;dh=w/sr;dx=x;dy=y+(h-dh)/2;}else{dh=h;dw=h*sr;dx=x+(w-dw)/2;dy=y;}
    cctx.drawImage(v,dx,dy,dw,dh);
  }else{
    cctx.fillStyle='#0b1921';cctx.fillRect(x,y,w,h);cctx.textAlign='center';cctx.fillStyle=CC.mut;
    cctx.font='600 20px -apple-system,sans-serif';cctx.fillText(label+' · 等待画面',x+w/2,y+h/2);cctx.textAlign='left';
  }
  cctx.restore();
  cLabel(label,x+14,y+24,CC.cy);
}
function drawHud(x,y,w,h){
  const s=hudState||{};cPanel(x,y,w,h,CC.panel,CC.line);
  const px=x+18;let cy=y+32;
  cctx.fillStyle=s.phase==='attention'?CC.warn:(s.phase==='completed'?CC.te:CC.cy);
  cctx.font='700 16px ui-monospace,Menlo,monospace';cctx.fillText('● '+(PH[s.phase]||'READY'),px,cy);
  cctx.fillStyle=CC.mut;cctx.font='700 14px ui-monospace,Menlo,monospace';
  cctx.fillText('STEP '+String(s.step==null?0:s.step).padStart(2,'0'),x+w-120,cy);cy+=30;
  const blks=s.blocks||[];
  cLabel('INTENT',px,cy,CC.cy);cy+=22;
  cctx.fillStyle='#cfe9ec';cctx.font='400 16px -apple-system,PingFang SC,sans-serif';
  const it=blks.length?blks.map(b=>b.index+'. '+(b.intent||'')).join('\n'):(s.intent||'—');
  const il=cWrap(it,w-36,4);il.forEach((l,i)=>cctx.fillText(l,px,cy+i*22));cy+=22*il.length+16;
  cLabel('THINKING',px,cy,CC.cy);cy+=22;
  cctx.fillStyle=CC.mut;cctx.font='400 14px ui-monospace,Menlo,monospace';
  const tl=cWrap(String(s.reasoning||'—').slice(-700),w-36,8);tl.forEach((l,i)=>cctx.fillText(l,px,cy+i*20));cy+=20*tl.length+16;
  const codeState = codeForHud(s);
  cLabel('CODE · STEP '+String(codeState.step).padStart(2,'0')+(codeState.preview?' · PREVIEW':''),px,cy,CC.cy);cy+=22;
  cctx.fillStyle=CC.code;cctx.font='400 13.5px ui-monospace,Menlo,monospace';
  const ct=codeState.text||'等待 agent 写下本轮代码';
  cWrap(ct,w-36,9).forEach((l,i)=>cctx.fillText(l,px,cy+i*19));
}
function drawChat(x,y,w,h){
  cPanel(x,y,w,h,CC.panel,CC.line);const px=x+18;let cy=y+30;
  cLabel('CONVERSATION',px,cy,CC.cy);cy+=24;
  cctx.font='400 14.5px -apple-system,PingFang SC,sans-serif';
  chatLog.slice(-8).forEach(m=>{
    const role=m.cls==='me'?'你':(m.cls==='ag'?'Agent':(m.cls==='ask'?'询问':'系统'));
    cctx.fillStyle=m.cls==='me'?'#cdf1f7':(m.cls==='ag'?'#cdf3e4':(m.cls==='ask'?CC.warn:CC.mut));
    const ls=cWrap(role+'：'+m.text,w-36,3);ls.forEach((l,i)=>cctx.fillText(l,px,cy+i*20));
    cy+=20*ls.length+9;if(cy>y+h-24)return;
  });
}
function drawComposite(){
  const W=compCanvas.width,H=compCanvas.height;
  const g=cctx.createLinearGradient(0,0,W,H);g.addColorStop(0,'#07131a');g.addColorStop(1,'#020609');
  cctx.fillStyle=g;cctx.fillRect(0,0,W,H);
  cctx.textAlign='left';cctx.fillStyle=CC.mut;cctx.font='700 13px ui-monospace,Menlo,monospace';
  cctx.fillText('AGENT DEMO STUDIO · GUI + CODE · LIVE',24,40);
  drawScreen(24,64,1180,992);drawHud(1228,64,668,596);drawChat(1228,688,668,368);
}
let compWorker=null;
function startComposite(){
  if(compWorker||compRaf!==null)return;
  drawComposite();
  // Worker 定时器驱动重绘(~60fps)：Worker 不受隐藏标签页节流，录制时即使切走标签页，
  // canvas 仍持续重绘、captureStream 仍产出连续帧，不会退化成只有首帧(rAF 在隐藏时会被冻结)。
  try{
    const bu=URL.createObjectURL(new Blob(['setInterval(function(){postMessage(1)},16)'],{type:'application/javascript'}));
    compWorker=new Worker(bu);
    compWorker.onmessage=function(){drawComposite();};
  }catch(e){
    const loop=function(){drawComposite();compRaf=requestAnimationFrame(loop);};
    compRaf=requestAnimationFrame(loop);
  }
}
</script><script type="module" src="/mobile/mobile.js" onerror="document.getElementById('phonestatus').textContent='手机串流需先在 mobile 目录完成 npm ci、npm run setup、npm run build'"></script></body></html>
"""


# 允许从 config JSON 透传给 mac_agent 的 dest 白名单（避免注入无效 flag）。
_FORWARD_KEYS = {
    "model_name", "llm_base_url", "llm_protocol", "reasoning_effort", "history_turns",
    "img_keep_n", "img_interval", "max_image_dimension", "jpeg_quality", "driver_timeout",
    "api_timeout", "api_retries", "parse_retries", "coordinate_range", "max_tokens", "context_limit",
    "startup_observation", "background_mode", "web_profile", "demo_mode",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="mac_agent 单会话录制工作台")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "low", "medium", "high", "xhigh", "max"),
        default=None,
        help="override config reasoning effort; omitted keeps the config/agent default",
    )
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--mobile", action="store_true", help="Enable local web ADB/scrcpy Android screen streaming")
    parser.add_argument(
        "--config", default=None,
        help="JSON 配置（键为 dest 名）；默认读 packages/harness/configs/macos_demo.json（若存在）。",
    )
    return parser


def main() -> int:
    global HUB
    args = build_parser().parse_args()
    cfg_path = Path(args.config) if args.config else None
    if cfg_path is None:
        cbase = Path(__file__).resolve().parents[1] / "configs"
        for cand in (os.environ.get("MAC_AGENT_CONFIG"), cbase / "local.json",
                     cbase / "macos_demo.json"):
            if cand and Path(cand).is_file():
                cfg_path = Path(cand)
                break
    config: dict = {}
    hub_cfg: dict = {}
    if cfg_path and cfg_path.is_file():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        config = {k: v for k, v in raw.items() if k in _FORWARD_KEYS}
        # hub 级开关：不进 session.config（否则会被 _config_args 转发成 mac_agent 不认的 flag），
        # 单独存 hub_config 并跨重启持久。
        for hub_key in ("agent_mode", "mobile_serial"):
            if hub_key in raw:
                hub_cfg[hub_key] = raw[hub_key]
    if args.reasoning_effort is not None:  # 只有显式 CLI 值才覆盖配置文件
        config["reasoning_effort"] = args.reasoning_effort
    HUB = Hub(config)
    HUB.session.hub_config = hub_cfg
    if not args.mobile:
        HUB.session.hub_config.pop("mobile_serial", None)
    HUB.session.config_path = str(cfg_path) if cfg_path else None
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    if args.mobile:
        try:
            HUB.mobile_bridge = MobileBridge(url)
        except Exception:
            server.server_close()
            raise
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    print(f"Agent Demo Studio: {url}")
    print("① 选屏 ② 开始录制 ③ 右侧输入指令即启动 agent（Enter 发送，Shift+Enter 换行）")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStudio stopped.")
        HUB.session.stop()
    finally:
        server.server_close()
        if HUB.mobile_bridge:
            HUB.mobile_bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
