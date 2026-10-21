"""Persistent-worker binding for local, validated Skill instructions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from skill_registry import SkillDocument, SkillInfo, SkillRegistry


class SkillRuntime:
    def __init__(self, registry: SkillRegistry, emit: Callable[[dict[str, Any]], None]) -> None:
        self.registry = registry
        self.emit = emit
        self._loaded: SkillDocument | None = None
        self._selected_name: str | None = None
        self._digest: str | None = None
        self._revision: int | None = None
        self._configured_selection: tuple[str | None, str | None] = (None, None)
        self._selection_source = "model"

    @property
    def selected_name(self) -> str | None:
        return self._selected_name

    @property
    def revision(self) -> int:
        return self._revision if self._revision is not None else 0

    def configure(self, name: str | None, digest: str | None, revision: int) -> None:
        if name is not None and (not isinstance(name, str) or not name):
            raise ValueError("selected_skill 必须是非空名称或 None")
        if digest is not None and (not isinstance(digest, str) or not digest):
            raise ValueError("skill_digest 必须是非空字符串或 None")
        if name is None and digest is not None:
            raise ValueError("空 Skill 槽不能携带 digest")
        if type(revision) is not int or revision < 0:
            raise ValueError("skill_revision 必须是非负整数")
        selection = (name, digest)
        if self._revision is not None:
            if revision < self._revision:
                raise ValueError("不能回退 Skill 选择 revision")
            if revision == self._revision:
                if selection != self._configured_selection:
                    raise ValueError("Skill 选择改变必须递增 revision")
                # host 尚未接到模型首次选择时，重复的空配置也不能清掉该 pin。
                return
        self.registry.discover()
        keep_loaded = (
            self._loaded is not None and name == self._selected_name
            and digest == self._loaded.info.digest
        )
        if not keep_loaded:
            self._loaded = None
        self._selected_name = name
        self._digest = self._loaded.info.digest if keep_loaded else digest
        self._revision = revision
        self._configured_selection = selection
        self._selection_source = "user" if name is not None else "model"

    def list(self) -> tuple[SkillInfo, ...]:
        return self.registry.discover()

    def load(self, name: str) -> SkillDocument:
        if self._selected_name is not None and name != self._selected_name:
            raise ValueError(f"Skill '{self._selected_name}' 已占用 session 单槽；请由用户显式替换或清空")
        if self._loaded is not None:
            return self._loaded
        document = self.registry.load(name)
        info = document.info
        if self._digest is not None and info.digest != self._digest:
            raise ValueError(f"Skill '{name}' changed; re-select it explicitly")
        self.emit({
            "type": "skill_loaded", "name": info.name, "source": info.source, "digest": info.digest,
            "selection_source": self._selection_source, "revision": self.revision,
        })
        self._selected_name = name
        self._digest = info.digest
        self._loaded = document
        return document

    def read(self, name: str, relative_path: str) -> str:
        if name != self._selected_name or self._loaded is None:
            raise ValueError(f"load 当前 selected Skill '{name}' before reading its references")
        current = self.registry.load(name).info
        if current.digest != self._digest or current.source != self._loaded.info.source:
            raise ValueError(f"Skill '{name}' changed; re-select it explicitly")
        return self.registry.read(name, relative_path)

    def reset_task(self) -> None:
        self.registry.discover()
