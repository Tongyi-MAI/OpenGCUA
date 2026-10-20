"""UI-independent turn and in-memory message queue transitions."""

from __future__ import annotations

from collections import deque
from typing import Literal

from agent_events import TerminalCommand


class TerminalInteraction:
    def __init__(self) -> None:
        self._turn_active = False
        self._queue_suspended = False
        self._queue: deque[str | TerminalCommand] = deque()

    @property
    def turn_active(self) -> bool:
        return self._turn_active

    @property
    def queue_suspended(self) -> bool:
        return self._queue_suspended

    def submit(self, text: str) -> Literal["send", "queued", "ignored"]:
        if not text.strip():
            return "ignored"
        if self._turn_active:
            self._queue.append(text)
            return "queued"
        return "send"

    def submit_control(self, command: TerminalCommand) -> Literal["send", "queued"]:
        if self._turn_active:
            self._queue.append(command)
            return "queued"
        return "send"

    def begin_turn(self) -> None:
        self._turn_active = True

    def finish_turn(self, *, normal: bool) -> None:
        self._turn_active = False
        if not normal:
            self._queue_suspended = True

    def next_message(self) -> str | None:
        if self._turn_active or self._queue_suspended or not self._queue:
            return None
        if isinstance(self._queue[0], TerminalCommand):
            return None
        return self._queue.popleft()

    def next_item(self) -> str | TerminalCommand | None:
        if self._turn_active or self._queue_suspended or not self._queue:
            return None
        return self._queue.popleft()

    def queued_messages(self) -> tuple[str, ...]:
        return tuple(item for item in self._queue if isinstance(item, str))

    def queued_items(self) -> tuple[str | TerminalCommand, ...]:
        return tuple(self._queue)

    def suspend_queue(self) -> None:
        self._queue_suspended = True

    def resume_queue(self) -> None:
        self._queue_suspended = False

    def clear_queue(self) -> None:
        self._queue.clear()
