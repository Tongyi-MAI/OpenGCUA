#!/usr/bin/env python3
"""mac_agent 的 OBS 可用实时 HUD / 录制工作台（参考 demo_agent/demo_hud.py 适配）。

自动发现 ``runs/`` 下最新 run（或 --run 指定 / 页面 ?run= 选择），把
``traj.json`` + ``steps/step_N.json`` 转成稳定 JSON 视图模型：
phase（thinking/executing/completed/attention）、intent、reasoning 尾部、
动作批次与历史（AX/GUI/观察/bash）、最近 bash 输出、错误、耗时与统计。
内置脱敏（authorization/api_key/base64 图片）。

用法：
    python demo_hud.py --port 8799 --open
    OBS Browser Source:  http://127.0.0.1:8799/?mode=panel   （透明背景）
    录制工作台:          http://127.0.0.1:8799/studio
"""

from __future__ import annotations

import argparse
import json
import re
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "demo_overlay"


def clipped(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    edge = max(1, (limit - 32) // 2)
    return text[:edge] + "\n… content omitted …\n" + text[-edge:]


def redact_for_display(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;\"']+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,;\"']+", r"\1[REDACTED]", text)
    text = re.sub(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", "[IMAGE DATA]", text)
    return text


def find_latest_trajectory(runs_dir: Path) -> Path | None:
    candidates = list(runs_dir.glob("*/traj.json"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def resolve_trajectory(runs_dir: Path, explicit: str | None) -> Path | None:
    if not explicit:
        return find_latest_trajectory(runs_dir)
    candidate = Path(explicit)
    if not candidate.is_absolute():
        candidate = runs_dir / candidate
    if candidate.is_dir() or (not candidate.exists() and not candidate.suffix):
        candidate = candidate / "traj.json"
    return candidate


def _call_method(call: dict[str, Any]) -> str:
    return str((call.get("params") or {}).get("method") or call.get("rpc_method")
               or call.get("tool") or "")


def _cua_action_view(call: dict[str, Any], index: int) -> dict[str, Any]:
    args = ((call.get("params") or {}).get("arguments") or call.get("arguments") or {})
    method = _call_method(call)
    if method.startswith("mai_ui.action."):
        act = method.rsplit(".", 1)[-1]
        title, detail = f"SDK {act}", str(args.get("window_id") or args.get("app_id") or "")
        icon = "♿" if args.get("node_id") else "◎"
    elif method == "native_pixel_action":
        act = str(args.get("action") or "click")
        title = f"GUI {act}"
        detail = f"({args.get('x')}, {args.get('y')})" + (
            f" {args.get('direction')} x{args.get('amount')}" if act == "scroll" else "")
        icon = "◎"
    elif method == "ax_action":
        tool = str(args.get("tool") or "click")
        title = f"AX {tool}"
        detail = str(args.get("element_ref") or args.get("text") or "")[:80]
        icon = "♿"
    elif method in ("get_window_state", "mai_ui.observe"):
        ax = method == "mai_ui.observe" and bool(args.get("tree")) or bool(
            (call.get("params") or {}).get("include_accessibility_tree"))
        title = "AX observe" if ax else "screenshot"
        detail = f"window {args.get('window_id') or (call.get('params') or {}).get('window_id') or ''}"
        icon = "👁"
    elif method in ("mai_list_windows", "list_windows"):
        title, detail, icon = "inventory", "windows", "☰"
    elif method in ("mai_list_apps", "list_apps"):
        title, detail, icon = "inventory", "apps", "☰"
    else:
        title, detail, icon = method or "call", "", "·"
    return {
        "index": index, "type": method, "icon": icon, "title": title,
        "detail": clipped(redact_for_display(detail), 200),
        "status": "error" if call.get("is_error") or call.get("ok") is False else "ok",
        "elapsed": round(
            call.get("elapsed_seconds")
            or ((call.get("result") or {}).get("elapsed_seconds")
                if isinstance(call.get("result"), dict) else 0.0)
            or 0.0,
            2,
        ),
    }


def _step_file(run_dir: Path, step: int) -> dict[str, Any]:
    path = run_dir / "steps" / f"step_{step}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def latest_bash_view(steps: list[dict[str, Any]], run_dir: Path) -> dict[str, Any] | None:
    for step in reversed(steps):
        code = str(step.get("code") or "")
        if "bash(" not in code:
            continue
        match = re.search(r"bash\(\s*(?:r?'''|r?\"\"\"|r?'|r?\")", code)
        command = code[match.start():match.start() + 420] if match else code[:420]
        data = _step_file(run_dir, int(step.get("step") or 0))
        ex = data.get("execution") or {}
        parts: list[str] = []
        if ex.get("stdout"):
            parts.append(str(ex["stdout"]))
        if ex.get("stderr"):
            parts.append("stderr:\n" + str(ex["stderr"]))
        return {
            "step": step.get("step"),
            "command": clipped(redact_for_display(command), 420),
            "output": clipped(redact_for_display("\n".join(parts) or "(no output)"), 1200),
            "returncode": ex.get("returncode"),
        }
    return None


def phase_for(trajectory: dict[str, Any], run_dir: Path) -> str:
    status = str(trajectory.get("status") or "running")
    if status != "running":
        return "completed" if status == "success" else "attention"
    steps = trajectory.get("steps") or []
    last = steps[-1] if steps else {}
    if (last.get("metrics") or {}).get("returncode") not in (None, 0):
        return "attention"
    # 常驻多轮会话：进程存活期间 traj.status 一直是 "running"（在等下一条指令），
    # 但只要最后一个 task 已 terminate、拿到终态 status，本轮就已完成，必须显示
    # completed/attention；否则 HUD 会永远停在 thinking，看起来像“发送后卡死”。
    rounds = trajectory.get("rounds") or trajectory.get("tasks") or []
    if rounds:
        last_round = str(rounds[-1].get("status") or "").lower()
        if last_round in ("success", "succeeded", "ok", "done"):
            return "completed"
        if last_round and last_round not in ("running", "pending", "in_progress", "none"):
            return "attention"
    n_step_files = len(list((run_dir / "steps").glob("step_*.json"))) if (run_dir / "steps").is_dir() else 0
    if n_step_files > len(steps):
        return "executing"
    return "thinking"


def build_hud_state(trajectory: dict[str, Any], trajectory_path: Path) -> dict[str, Any]:
    run_dir = trajectory_path.parent
    steps = trajectory.get("steps") or []
    last = steps[-1] if steps else {}
    reasoning_step = next((s for s in reversed(steps) if str(s.get("reasoning") or "").strip()), {})
    last_data = _step_file(run_dir, int(last.get("step") or 0)) if last else {}
    calls = last_data.get("cua_calls") or []
    actions = [_cua_action_view(c, i) for i, c in enumerate(calls, start=1)]
    history: list[dict[str, Any]] = []
    for step in reversed(steps[:-1] if len(steps) > 1 else []):
        data = _step_file(run_dir, int(step.get("step") or 0))
        for i, c in reversed(list(enumerate(data.get("cua_calls") or [], start=1))):
            view = _cua_action_view(c, i)
            view["step"] = step.get("step")
            history.append(view)
            if len(history) >= 12:
                break
        if len(history) >= 12:
            break
    all_calls = []
    for step in steps:
        all_calls += (_step_file(run_dir, int(step.get("step") or 0)).get("cua_calls") or [])
    started = None
    for step in steps:
        started = step.get("timestamp") or started
        if started:
            break
    ask_user: str | None = None
    if str(trajectory.get("status") or "running") == "running":
        try:
            tail = (run_dir / "agent.log").read_text(encoding="utf-8", errors="replace")[-4000:]
            for line in reversed(tail.splitlines()):
                if "ask_user question=" in line:
                    ask_user = line.split("ask_user question=", 1)[1].strip().strip("'\"")[:400]
                    break
        except OSError:
            ask_user = None
    elapsed = None
    if started:
        try:
            elapsed = round(time.time() - float(started), 1)
        except (TypeError, ValueError):
            elapsed = None
    # 规划中的当前步还没有 blocks：保留最近一次有内容的批次，画面不闪空。
    block_step = next((s for s in reversed(steps) if s.get("blocks") or s.get("code")), {})
    blocks = block_step.get("blocks") or []
    if not blocks and (block_step.get("intent") or block_step.get("code")):
        blocks = [{"intent": block_step.get("intent") or "", "code": block_step.get("code") or ""}]
    steps_view = [
        {"index": i, "intent": clipped(redact_for_display(b.get("intent") or ""), 220),
         "code": clipped(redact_for_display(b.get("code") or ""), 1600)}
        for i, b in enumerate(blocks, start=1)
    ]
    gui_intents = [
        {"step": step.get("step"),
         "intent": clipped(redact_for_display(step.get("intent") or ""), 300)}
        for step in steps[-50:] if step.get("intent")
    ]
    gui_action_step = next((s for s in reversed(steps) if s.get("actions")), {})
    gui_actions = [
        {"source": clipped(redact_for_display(action.get("source") or ""), 1600),
         "status": str(action.get("status") or "pending")}
        for action in (gui_action_step.get("actions") or [])
    ]
    return {
        "ok": True,
        "task_id": run_dir.name,
        "blocks": steps_view,
        "blocks_step": block_step.get("step"),
        "gui_intents": gui_intents,
        "gui_actions": gui_actions,
        "gui_actions_step": gui_action_step.get("step"),
        "task": clipped(redact_for_display(
            trajectory.get("latest_user_message") or trajectory.get("task") or ""), 800),
        "status": trajectory.get("status") or "running",
        "phase": phase_for(trajectory, run_dir),
        "step": last.get("step", 0),
        "step_count": len(steps),
        "model": (trajectory.get("config") or {}).get("model_name") or "",
        "reasoning_effort": (trajectory.get("config") or {}).get("reasoning_effort"),
        "intent": clipped(redact_for_display(last.get("intent") or block_step.get("intent") or ""), 300),
        "reasoning": clipped(redact_for_display(reasoning_step.get("reasoning") or ""), 2400),
        "reasoning_step": reasoning_step.get("step"),
        "actions": actions,
        "batch_step": last.get("step"),
        "action_history": history,
        "bash": latest_bash_view(steps, run_dir),
        "ask_user": clipped(redact_for_display(ask_user or ""), 400) or None,
        "error": clipped(redact_for_display((last_data.get("execution") or {}).get("stderr") or ""), 800),
        "elapsed_seconds": elapsed,
        "stats": {
            "model_calls": len(steps),
            "actions": len(all_calls),
            "gui_actions": sum(
                _call_method(c) == "native_pixel_action"
                or _call_method(c).startswith("mai_ui.action.point_") for c in all_calls
            ),
            "ax_actions": sum(
                _call_method(c) == "ax_action"
                or (_call_method(c).startswith("mai_ui.action.")
                    and bool((c.get("arguments") or {}).get("node_id"))) for c in all_calls
            ),
            "observes": sum(
                _call_method(c) in ("get_window_state", "mai_ui.observe") for c in all_calls
            ),
            "bash_steps": sum("bash(" in str(s.get("code") or "") for s in steps),
        },
        "updated_at": last.get("timestamp"),
        "trajectory_mtime_ms": trajectory_path.stat().st_mtime_ns // 1_000_000,
        "trajectory_path": str(trajectory_path),
    }


class HudRequestHandler(SimpleHTTPRequestHandler):
    server_version = "MacAgentHUD/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        if route == "/api/state":
            trajectory_path = resolve_trajectory(
                self.server.runs_dir, (query.get("run") or [None])[0])
            if trajectory_path is None or not trajectory_path.exists():
                self.send_json({"ok": False, "error": "No traj.json found. Start mac_agent first."}, 404)
                return
            try:
                trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
                self.send_json(build_hud_state(trajectory, trajectory_path))
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                self.send_json({"ok": False, "error": f"Could not read trajectory: {exc}"}, 503)
            return
        if route == "/api/runs":
            runs = []
            for traj in sorted(self.server.runs_dir.glob("*/traj.json"),
                               key=lambda p: p.stat().st_mtime_ns, reverse=True)[:30]:
                try:
                    data = json.loads(traj.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                runs.append({"id": traj.parent.name,
                             "task": clipped(data.get("task") or "", 80),
                             "status": data.get("status"),
                             "steps": len(data.get("steps") or []),
                             "mtime": traj.stat().st_mtime})
            self.send_json({"ok": True, "runs": runs})
            return
        if route == "/health":
            self.send_json({"ok": True})
            return
        if route == "/":
            self.path = "/index.html" if query.get("mode") == ["panel"] else "/studio.html"
        elif route == "/studio":
            self.path = "/studio.html"
        elif route == "/panel":
            self.path = "/index.html"
        super().do_GET()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the mac_agent OBS HUD / recording studio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--runs-dir", type=Path, default=ROOT / "runs")
    parser.add_argument("--run", type=str, default=None,
                        help="Optional run dir name or traj.json path; default follows newest run")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not STATIC_DIR.exists():
        raise SystemExit(f"Missing HUD assets: {STATIC_DIR}")
    server = ThreadingHTTPServer((args.host, args.port), HudRequestHandler)
    server.runs_dir = args.runs_dir.expanduser().resolve()
    server.explicit_run = args.run
    server.verbose = args.verbose
    url = f"http://{args.host}:{args.port}"
    print(f"录制工作台: {url}/studio")
    print(f"OBS Browser Source: {url}/?mode=panel")
    print(f"Runs directory: {server.runs_dir}")
    if args.open:
        webbrowser.open(f"{url}/studio")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nHUD stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
