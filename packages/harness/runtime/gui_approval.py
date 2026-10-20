"""Host-owned consent for SDK visual and foreground input."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any


class GuiGrantStore:
    def __init__(self) -> None:
        self._grants: set[tuple[int, str, str]] = set()

    def allowed(self, task_index: int, app_id: str, window_id: str) -> bool:
        return (task_index, app_id, window_id) in self._grants

    def grant(self, task_index: int, app_id: str, window_id: str) -> None:
        if not app_id or not window_id:
            raise ValueError("GUI grant requires an exact app and window")
        self._grants.add((task_index, app_id, window_id))

    def clear(self) -> None:
        self._grants.clear()


async def request_gui_approval(
    grants: GuiGrantStore,
    task_index: int,
    event: Mapping[str, Any],
    ask: Callable[[str], Awaitable[str | None]],
) -> bool:
    app_id = event.get("app_id")
    window_id = event.get("window_id")
    if not isinstance(app_id, str) or not app_id or not isinstance(window_id, str) or not window_id:
        return False
    if grants.allowed(task_index, app_id, window_id):
        return True
    question = (
        f"Agent 请求操作 App {app_id} 的窗口 {window_id}。"
        "本任务可能使用视觉点击（定向操作不一定移动真实鼠标），"
        "必要时还会短暂接管前台焦点、鼠标和键盘。"
        "同意后，该任务在此窗口可连续执行这些操作，直到任务结束；"
        "切换 App/窗口会重新询问。是否同意？请输入 yes 或 同意；其他回答均拒绝。"
    )
    answer = await ask(question)
    if not isinstance(answer, str) or answer.strip().casefold() not in {
        "yes", "y", "同意", "允许", "可以",
    }:
        return False
    grants.grant(task_index, app_id, window_id)
    return True
