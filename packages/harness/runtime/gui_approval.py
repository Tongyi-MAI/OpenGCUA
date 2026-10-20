"""Host-owned, task-scoped consent for SDK foreground input."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal

from agent_events import ConsentChoice, ForegroundApprovalPrompt

ConsentState = Literal["unset", "allowed", "denied"]


class GuiGrantStore:
    def __init__(self) -> None:
        self._states: dict[int, ConsentState] = {}

    def state(self, task_index: int) -> ConsentState:
        return self._states.get(task_index, "unset")

    def decide(self, task_index: int, *, allow: bool) -> None:
        self._states[task_index] = "allowed" if allow else "denied"

    def clear(self) -> None:
        self._states.clear()


async def request_gui_approval(
    grants: GuiGrantStore,
    task_index: int,
    event: Mapping[str, Any],
    choose: Callable[[ForegroundApprovalPrompt], Awaitable[ConsentChoice]],
) -> bool:
    request_id = event.get("request_id")
    app_id = event.get("app_id")
    window_id = event.get("window_id")
    if not all(isinstance(value, str) and value for value in (request_id, app_id, window_id)):
        return False
    state = grants.state(task_index)
    explicit_retry = event.get("explicit_retry") is True
    if explicit_retry and state != "denied":
        return False
    if state == "allowed":
        return True
    if state == "denied" and not explicit_retry:
        return False
    reason = event.get("reason")
    reason_text = f"原因：{reason}。" if isinstance(reason, str) and reason.strip() else ""
    prompt = ForegroundApprovalPrompt(
        request_id=request_id, app_id=app_id, window_id=window_id,
        task_index=task_index,
        text=(
            f"Agent 请求接管前台键鼠：App {app_id}，窗口 {window_id}。{reason_text}"
            "允许后，本次任务可反复激活窗口并使用你的鼠标和键盘，直到任务结束；"
            "期间请避免同时操作桌面。选择允许或拒绝；未答复前不会执行该动作。"
        ),
    )
    choice = await choose(prompt)
    if choice == "allow":
        grants.decide(task_index, allow=True)
        return True
    if choice == "reject":
        grants.decide(task_index, allow=False)
    return False
