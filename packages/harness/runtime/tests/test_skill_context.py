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
    second = messages(manager)
    assert str(second).count("Private workflow body") == 1
    assert second[1]["role"] == "user"
    manager.set_task("A different task")
    assert "Private workflow body" not in str(messages(manager))


def test_digest_safe_restore(tmp_path):
    _, selected, folder = context(tmp_path)
    selected.activate("example")
    records = selected.records()
    assert records[0]["digest"] and records[0]["source"] == "bundled"
    same = SkillContext(selected.registry)
    assert same.restore(records) == ()
    assert "Private workflow body" in str(same.active_messages())
    (folder / "SKILL.md").write_text(
        "---\nname: example\ndescription: Helps with examples\n---\n\nDifferent version.\n", encoding="utf-8"
    )
    selected.registry.discover()
    stale = SkillContext(selected.registry)
    warnings = stale.restore(records)
    assert warnings and "example" in warnings[0]
    assert stale.active_messages() == []


def test_active_reference_survives_history_cutoff_and_summary(tmp_path):
    manager, selected, _ = context(tmp_path)
    selected.activate("example")
    manager.compaction_state.summary = "Earlier work summarized."
    manager.compaction_state.cutoff = 10
    rendered = str(messages(manager))
    assert rendered.count("Private workflow body") == 1
    assert "Earlier work summarized" in rendered
