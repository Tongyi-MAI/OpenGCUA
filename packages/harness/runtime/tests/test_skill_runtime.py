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
    assert runtime.read("example", "reference.md") == "Reference text"
    assert runtime.selected_name == "example" and len(events) == 1


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


def test_new_task_refreshes_persistent_worker_registry(tmp_path):
    bundled = tmp_path / "bundle"
    folder = bundled / "example"
    folder.mkdir(parents=True)
    document = folder / "SKILL.md"
    document.write_text("---\nname: example\ndescription: Example\n---\n\nOriginal body.\n")
    registry = SkillRegistry(bundled, tmp_path / "personal")
    registry.discover()
    runtime = SkillRuntime(registry, lambda _event: None)
    assert "Original body" in runtime.load("example").body
    document.write_text("---\nname: example\ndescription: Example\n---\n\nUpdated body.\n")
    added = bundled / "added"
    added.mkdir()
    (added / "SKILL.md").write_text("---\nname: added\ndescription: New skill\n---\n\nNew body.\n")

    runtime.reset_task()

    assert "Original body" in runtime.load("example").body
    assert {info.name for info in runtime.list()} == {"example", "added"}
    with pytest.raises(ValueError, match="单槽"):
        runtime.load("added")
    updated_digest = registry.load("example").info.digest
    runtime.configure("example", updated_digest, 1)
    assert "Updated body" in runtime.load("example").body
    runtime.configure("added", None, 2)
    assert "New body" in runtime.load("added").body


def single_slot_runtime(tmp_path):
    bundled = tmp_path / "bundle"
    for name in ("example", "other"):
        folder = bundled / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}\n---\n\n{name} body.\n")
        (folder / "reference.md").write_text(f"{name} reference")
    registry = SkillRegistry(bundled, tmp_path / "personal")
    registry.discover()
    events = []
    return SkillRuntime(registry, events.append), events


def test_model_cannot_replace_session_slot_and_failed_load_does_not_pin(tmp_path):
    runtime, events = single_slot_runtime(tmp_path)
    with pytest.raises(ValueError):
        runtime.load("missing")
    assert runtime.selected_name is None and events == []
    runtime.configure(None, None, 0)
    original = runtime.load("example")
    runtime.configure(None, None, 0)
    assert runtime.load("example") is original
    with pytest.raises(ValueError, match="单槽"):
        runtime.load("other")
    with pytest.raises(ValueError, match="selected"):
        runtime.read("other", "reference.md")
    runtime.reset_task()
    assert runtime.load("example") is original and len(events) == 1
    assert events[0]["selection_source"] == "model"


def test_host_selection_replace_clear_and_same_revision_are_idempotent(tmp_path):
    runtime, events = single_slot_runtime(tmp_path)
    digest = runtime.registry.load("example").info.digest
    runtime.configure("example", digest, 1)
    with pytest.raises(ValueError, match="load"):
        runtime.read("example", "reference.md")
    original = runtime.load("example")
    runtime.configure("example", digest, 1)
    runtime.reset_task()
    assert runtime.load("example") is original and len(events) == 1
    assert events[0]["selection_source"] == "user"
    assert events[0]["revision"] == 1
    runtime.configure("other", None, 2)
    with pytest.raises(ValueError, match="单槽"):
        runtime.load("example")
    assert runtime.load("other").info.name == "other"
    runtime.configure(None, None, 3)
    assert runtime.selected_name is None
    with pytest.raises(ValueError, match="load"):
        runtime.read("other", "reference.md")
    runtime.load("example")
    runtime.configure(None, None, 3)
    assert runtime.selected_name == "example" and len(events) == 3


def test_host_acknowledges_model_pin_without_reloading(tmp_path):
    runtime, events = single_slot_runtime(tmp_path)
    runtime.configure(None, None, 0)
    original = runtime.load("example")
    runtime.configure("example", original.info.digest, 1)
    assert runtime.load("example") is original and len(events) == 1


def test_digest_mismatch_does_not_emit_or_pin_body(tmp_path):
    runtime, events = single_slot_runtime(tmp_path)
    runtime.configure("example", "wrong", 1)
    with pytest.raises(ValueError, match="changed"):
        runtime.load("example")
    assert events == []
    with pytest.raises(ValueError, match="load"):
        runtime.read("example", "reference.md")
    runtime.configure(None, None, 2)
    assert runtime.load("other").info.name == "other"


def test_read_rejects_changed_digest_even_after_task_catalog_refresh(tmp_path):
    runtime, _events = single_slot_runtime(tmp_path)
    original = runtime.load("example")
    (original.info.path / "SKILL.md").write_text("---\nname: example\ndescription: New\n---\n\nChanged.\n")
    runtime.reset_task()
    assert runtime.load("example") is original
    with pytest.raises(ValueError, match="changed"):
        runtime.read("example", "reference.md")


@pytest.mark.parametrize("name,digest,revision", [
    ("other", None, 1), ("example", "different", 1), ("example", None, 0),
    (None, "digest", 2), ("", None, 2), ("other", "", 2),
    ("other", None, -1), ("other", None, True),
])
def test_invalid_or_stale_host_config_cannot_replace_pin(tmp_path, name, digest, revision):
    runtime, events = single_slot_runtime(tmp_path)
    runtime.configure("example", None, 1)
    original = runtime.load("example")
    with pytest.raises(ValueError):
        runtime.configure(name, digest, revision)
    assert runtime.load("example") is original and len(events) == 1


def test_new_revision_without_digest_explicitly_repins_even_for_same_name(tmp_path):
    runtime, events = single_slot_runtime(tmp_path)
    runtime.configure("example", None, 1)
    original = runtime.load("example")
    (original.info.path / "SKILL.md").write_text("---\nname: example\ndescription: New\n---\n\nChanged.\n")
    runtime.configure("example", None, 1)
    assert runtime.load("example") is original and len(events) == 1
    runtime.configure("example", None, 2)
    assert "Changed." in runtime.load("example").body and len(events) == 2


def test_emit_failure_does_not_pin_model_selection(tmp_path):
    runtime, events = single_slot_runtime(tmp_path)
    def fail(_event):
        raise OSError("pipe failed")
    runtime.emit = fail
    with pytest.raises(OSError, match="pipe failed"):
        runtime.load("example")
    assert runtime.selected_name is None
    runtime.emit = events.append
    assert runtime.load("other").info.name == "other"
