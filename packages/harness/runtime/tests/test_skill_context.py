from pathlib import Path

import pytest

from mac_agent import ContextManager
from skill_context import SkillContext
from skill_registry import SkillRegistry


def context(tmp_path: Path):
    folder = tmp_path / "bundle" / "example"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: example\ndescription: Helps with examples\n---\n\nPrivate workflow body.\n", encoding="utf-8"
    )
    registry = SkillRegistry(tmp_path / "bundle", tmp_path / "personal")
    registry.discover()
    selected = SkillContext(registry)
    manager = ContextManager(
        task="Use an example", coordinate_range=1000, img_keep_n=0, img_interval=10,
        history_turns=1, micro_compaction=False, micro_trigger_steps=60,
        micro_keep_turns=25, micro_interval=35, todo_diff_snapshot=True,
        max_image_dimension=1920, jpeg_quality=85, skill_context=selected,
    )
    return manager, selected, folder


def messages(manager):
    return manager.build_messages(current_feedback="", current_screenshot=None, current_user_response="")


def test_catalog_precedes_task_and_body_is_lazy(tmp_path):
    manager, selected, _ = context(tmp_path)
    first = messages(manager)
    rendered = str(first)
    assert "Helps with examples" in rendered
    assert "Private workflow body" not in rendered
    assert rendered.index("Helps with examples") < rendered.index("Use an example")
    selected.activate("example")
    slot = selected.system_slot()
    assert slot.count("Private workflow body") == 1
    assert "name: example" in slot and "source: bundled" in slot
    assert selected.records()[0]["digest"] in slot
    assert selected.active_messages() == []
    manager.set_task("A different task")
    assert selected.system_slot() == slot
    assert selected.revision == 1


def test_digest_safe_restore(tmp_path):
    _, selected, folder = context(tmp_path)
    selected.activate("example")
    records = selected.records()
    assert records[0]["digest"] and records[0]["source"] == "bundled"
    same = SkillContext(selected.registry)
    assert same.restore(records) == ()
    assert "Private workflow body" in same.system_slot()
    assert same.selection_source == "restore"
    (folder / "SKILL.md").write_text(
        "---\nname: example\ndescription: Helps with examples\n---\n\nDifferent version.\n", encoding="utf-8"
    )
    selected.registry.discover()
    stale = SkillContext(selected.registry)
    warnings = stale.restore(records)
    assert warnings and "example" in warnings[0]
    assert stale.active_messages() == []
    assert stale.system_slot() == "" and stale.records() == []


def test_active_reference_survives_history_cutoff_and_summary(tmp_path):
    manager, selected, _ = context(tmp_path)
    selected.activate("example")
    manager.compaction_state.summary = "Earlier work summarized."
    manager.compaction_state.cutoff = 10
    rendered = str(messages(manager))
    assert selected.system_slot().count("Private workflow body") == 1
    assert "Earlier work summarized" in rendered


def test_model_pins_one_skill_and_only_user_can_replace_or_clear(tmp_path):
    _, selected, _ = context(tmp_path)
    other = tmp_path / "bundle" / "other"
    other.mkdir()
    (other / "SKILL.md").write_text("---\nname: other\ndescription: Other\n---\n\nOther body.\n")
    selected.registry.discover()
    assert selected.selected_name is None and selected.selection_source is None
    assert selected.revision == 0 and selected.system_slot() == ""
    document = selected.activate("example")
    assert selected.selection_source == "model" and selected.revision == 1
    assert selected.activate("example") is document
    with pytest.raises(ValueError, match="单槽"):
        selected.activate("other")
    with pytest.raises(ValueError, match="changed"):
        selected.activate("example", expected_digest="wrong")
    assert selected.revision == 1
    selected.start_task(7)
    assert selected.task_index == 7 and selected.selected_name == "example"
    selected.select("example")
    assert selected.selection_source == "user" and selected.revision == 1
    assert selected.activate("example") is document and selected.selection_source == "user"
    selected.select("other")
    assert selected.selected_name == "other" and selected.revision == 2
    assert "Private workflow body" not in selected.system_slot()
    assert len(selected.records()) == 1
    selected.select(None)
    assert selected.selected_name is None and selected.selection_source is None
    assert selected.revision == 3 and selected.records() == []
    selected.select(None)
    assert selected.revision == 3
    selected.activate("example")
    assert selected.revision == 4


def test_failed_selection_does_not_pin_or_replace(tmp_path):
    _, selected, _ = context(tmp_path)
    with pytest.raises(ValueError):
        selected.activate("missing")
    with pytest.raises(ValueError, match="changed"):
        selected.activate("example", expected_digest="wrong")
    assert selected.revision == 0 and selected.selected_name is None
    original = selected.select("example")
    with pytest.raises(ValueError):
        selected.select("missing")
    with pytest.raises(ValueError):
        selected.select("example", source="")
    with pytest.raises(ValueError, match="changed"):
        selected.select("example", expected_digest="wrong")
    assert selected.activate("example") is original and selected.revision == 1


def test_user_must_explicitly_reselect_changed_document(tmp_path):
    _, selected, folder = context(tmp_path)
    original = selected.activate("example")
    (folder / "SKILL.md").write_text("---\nname: example\ndescription: Updated\n---\n\nUpdated body.\n")
    selected.registry.discover()
    assert selected.activate("example") is original
    assert "Private workflow body" in selected.system_slot()
    with pytest.raises(ValueError, match="changed"):
        selected.select("example", expected_digest=original.info.digest)
    assert selected.revision == 1
    selected.select("example")
    assert "Updated body" in selected.system_slot()
    assert selected.revision == 2


@pytest.mark.parametrize("invalid", [
    [], [{"name": "example", "source": "personal", "digest": "same"}],
    [{"name": "example"}], [None],
    [{"name": "example", "source": "bundled", "digest": None}],
])
def test_restore_clears_existing_selection_on_empty_or_invalid_record(tmp_path, invalid):
    _, selected, _ = context(tmp_path)
    selected.activate("example")
    records = selected.records()
    if invalid and isinstance(invalid[0], dict) and invalid[0].get("digest") == "same":
        invalid = [{**records[0], "source": "personal"}]
    warnings = selected.restore(invalid)
    assert bool(warnings) == bool(invalid)
    assert selected.selected_name is None and selected.system_slot() == ""
    assert selected.revision == 2


def test_restore_rejects_legacy_multiple_skills_instead_of_selecting_first(tmp_path):
    _, selected, _ = context(tmp_path)
    selected.activate("example")
    records = selected.records()
    warnings = selected.restore(records + records)
    assert warnings and "0/1" in warnings[0]
    assert selected.records() == [] and selected.revision == 2


def test_same_restore_does_not_increment_revision_and_records_are_detached(tmp_path):
    _, selected, _ = context(tmp_path)
    selected.activate("example")
    records = selected.records()
    assert selected.restore(records) == () and selected.revision == 1
    records[0]["name"] = "other"
    assert selected.selected_name == "example"


def test_changed_restore_disables_already_pinned_body(tmp_path):
    _, selected, folder = context(tmp_path)
    selected.activate("example")
    records = selected.records()
    (folder / "SKILL.md").write_text("---\nname: example\ndescription: Updated\n---\n\nNew body.\n")
    selected.registry.discover()
    assert selected.restore(records)
    assert selected.system_slot() == "" and selected.revision == 2
