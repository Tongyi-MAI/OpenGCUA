"""Persistent-worker binding for local, validated Skill instructions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from skill_registry import SkillDocument, SkillInfo, SkillRegistry


class SkillRuntime:
    def __init__(self, registry: SkillRegistry, emit: Callable[[dict[str, Any]], None]) -> None:
        self.registry = registry
        self.emit = emit
        self._loaded: set[str] = set()

    def list(self) -> tuple[SkillInfo, ...]:
        return self.registry.discover()

    def load(self, name: str) -> SkillDocument:
        document = self.registry.load(name)
        if name not in self._loaded:
            info = document.info
            self.emit({"type": "skill_loaded", "name": info.name, "source": info.source, "digest": info.digest})
            self._loaded.add(name)
        return document

    def read(self, name: str, relative_path: str) -> str:
        if name not in self._loaded:
            raise ValueError(f"load Skill '{name}' before reading its references")
        return self.registry.read(name, relative_path)

    def reset_task(self) -> None:
        self._loaded.clear()
