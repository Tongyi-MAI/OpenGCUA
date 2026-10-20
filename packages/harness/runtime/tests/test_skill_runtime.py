from pathlib import Path

import pytest

from skill_registry import SkillRegistry
from skill_runtime import SkillRuntime


def test_load_once_and_read_only_within_skill(tmp_path):
    bundled = tmp_path / "bundle"
    folder = bundled / "example"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: example\ndescription: Example workflow\n---\n\nUse the reference.\n")
    (folder / "reference.md").write_text("Reference text")
    registry = SkillRegistry(bundled, tmp_path / "personal")
    registry.discover()
    events = []
    runtime = SkillRuntime(registry, events.append)
    assert [info.name for info in runtime.list()] == ["example"]
    with pytest.raises(ValueError, match="load"):
        runtime.read("example", "reference.md")
    assert "Use the reference" in runtime.load("example").body
    assert runtime.load("example") == registry.load("example")
    assert len(events) == 1
    assert events[0]["type"] == "skill_loaded"
    assert events[0]["digest"] == registry.load("example").info.digest
    assert runtime.read("example", "reference.md") == "Reference text"
    with pytest.raises(ValueError):
        runtime.read("example", "../../outside")
    with pytest.raises(ValueError):
        runtime.load("does-not-exist")
    assert len(events) == 1
    runtime.reset_task()
    with pytest.raises(ValueError, match="load"):
        runtime.read("example", "reference.md")


def test_executor_serializes_only_explicit_roots(tmp_path):
    from code_executor import CodeExecutor

    executor = CodeExecutor(
        runtime_path=Path(__file__).parents[1] / "code_runtime.py",
        work_dir=tmp_path / "work", rpc_socket=tmp_path / "rpc.sock",
        timeout=1, rpc_timeout=1, coordinate_range=1000, pyautogui_pause=0,
        enable_bash=False, skill_roots=(tmp_path / "one",),
    )
    assert executor._config()["skill_roots"] == [str(tmp_path / "one")]


def test_active_skill_does_not_silently_switch_revision_after_catalog_reload(tmp_path):
    folder = tmp_path / "bundle" / "example"
    folder.mkdir(parents=True)
    document = folder / "SKILL.md"
    document.write_text("---\nname: example\ndescription: Example\n---\n\nOriginal body.\n")
    registry = SkillRegistry(tmp_path / "bundle", tmp_path / "personal")
    registry.discover()
    runtime = SkillRuntime(registry, lambda _event: None)
    assert "Original body" in runtime.load("example").body
    document.write_text("---\nname: example\ndescription: Example\n---\n\nChanged body.\n")
    runtime.list()
    assert "Original body" in runtime.load("example").body
