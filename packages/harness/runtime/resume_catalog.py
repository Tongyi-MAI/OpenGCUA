"""Read-only discovery and compatibility preflight for saved agent runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from model_stream import AgentError


UI_API_MARKERS = {"mai-ui": "mai-ui/0.3-sdk-macos"}


class ResumeError(AgentError):
    """A saved trajectory cannot be safely used for a new conversation."""


@dataclass(frozen=True)
class ResumeCandidate:
    path: Path
    session_id: str
    last_task: str
    status: str
    updated_at: str
    turns: int
    todos: int


def validate_trajectory_ui_api(data: dict[str, Any], ui_api: str) -> None:
    """Reject unsupported run formats without rewriting historical markers."""
    if not isinstance(data, dict):
        raise ResumeError("轨迹必须是 JSON 对象，无法确认 UI API；拒绝恢复")
    config = data.get("config", {})
    if not isinstance(config, dict):
        raise ResumeError("轨迹 config 无效，无法确认 UI API；拒绝恢复")
    recorded_api = config.get("ui_api", "legacy-cua")
    if not isinstance(recorded_api, str) or recorded_api not in UI_API_MARKERS or recorded_api != ui_api:
        raise ResumeError(
            f"轨迹 UI API={recorded_api!r} 与当前 --ui-api {ui_api} 不兼容；"
            "迁移前的本地 Cua 轨迹不能改写标签或直接恢复到 SDK runtime"
        )
    if "ui_api_version" in config and config["ui_api_version"] != UI_API_MARKERS[ui_api]:
        raise ResumeError(f"轨迹 UI API 版本不兼容：{config['ui_api_version']!r}；拒绝恢复")


def inspect_resume_run(path: Path, *, ui_api: str) -> ResumeCandidate:
    """Validate one explicit run directory, including symlink targets, without writes."""
    try:
        run = Path(path).expanduser().resolve(strict=True)
        if not run.is_dir():
            raise ResumeError(f"恢复目标不是目录：{run}")
        trajectory = run / "traj.json"
        data = json.loads(trajectory.read_text(encoding="utf-8"))
        validate_trajectory_ui_api(data, ui_api)
        tasks = data.get("tasks")
        if not isinstance(tasks, list) or not tasks or not all(isinstance(item, dict) for item in tasks):
            raise ResumeError(f"轨迹缺少有效任务历史：{run}")
        last_task = tasks[-1].get("task")
        if not isinstance(last_task, str) or not last_task.strip():
            raise ResumeError(f"轨迹最后一个任务为空：{run}")
        steps = data.get("steps", [])
        if not isinstance(steps, list):
            raise ResumeError(f"轨迹步骤格式无效：{run}")
        todos = data.get("todos", [])
        if not isinstance(todos, list):
            raise ResumeError(f"轨迹 TODO 格式无效：{run}")
        updated = datetime.fromtimestamp(trajectory.stat().st_mtime).astimezone().isoformat(timespec="minutes")
        return ResumeCandidate(
            path=run,
            session_id=str(data.get("session_id") or ""),
            last_task=last_task,
            status=str(tasks[-1].get("status") or data.get("status") or "unknown"),
            updated_at=updated,
            turns=sum(isinstance(item, dict) and isinstance(item.get("turn"), dict) for item in steps),
            todos=len(todos),
        )
    except ResumeError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        raise ResumeError(f"无法读取恢复目标 {path}：{exc}") from exc


def recent_resume_runs(
    root: Path, *, current_run: Path | None, ui_api: str, limit: int = 10,
) -> tuple[ResumeCandidate, ...]:
    """Return most recently modified valid runs beneath the configured root."""
    if limit <= 0:
        return ()
    try:
        folders = tuple(Path(root).iterdir())
    except OSError:
        return ()
    active = Path(current_run).resolve() if current_run is not None else None
    found: list[tuple[float, ResumeCandidate]] = []
    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            continue
        try:
            candidate = inspect_resume_run(folder, ui_api=ui_api)
            if candidate.path == active:
                continue
            found.append(((candidate.path / "traj.json").stat().st_mtime, candidate))
        except (ResumeError, OSError):
            continue
    found.sort(key=lambda item: (item[0], str(item[1].path)), reverse=True)
    return tuple(candidate for _, candidate in found[:limit])
