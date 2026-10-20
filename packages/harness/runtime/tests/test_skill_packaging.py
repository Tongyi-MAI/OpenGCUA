from pathlib import Path
import importlib.util
import sys

import pytest

from mac_agent import MAI_UI_SYSTEM_PROMPT
from skill_registry import SkillRegistry, default_skill_roots


ROOT = Path(__file__).resolve().parents[4]


def test_builtin_skills_are_discoverable_from_source_and_app_layout(tmp_path):
    source_runtime = ROOT / "packages/harness/runtime/mac_agent.py"
    source_bundle, _ = default_skill_roots(source_runtime)
    names = {info.name for info in SkillRegistry(source_bundle, tmp_path / "personal").discover()}
    assert {"pdf-form", "dingtalk-workflow"} <= names

    script = ROOT / "packages/macos/scripts/build.py"
    sys.path.insert(0, str(script.parent))
    try:
        spec = importlib.util.spec_from_file_location("ogcua_build_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(script.parent))
    resources = tmp_path / "OpenGCUA.app" / "Contents/Resources"
    module.copy_bundled_skills(source_bundle, resources / "skills")
    app_runtime = resources / "runtime/mac_agent.py"
    app_bundle, _ = default_skill_roots(app_runtime)
    for name in ("pdf-form", "dingtalk-workflow"):
        assert (app_bundle / name / "SKILL.md").is_file()
    assert not (resources / "skills/personal").exists()


def test_prompt_points_to_skill_instead_of_embedding_pdf_workflow():
    assert "pdf-form" in MAI_UI_SYSTEM_PROMPT
    assert "## PDF 表单例外" not in MAI_UI_SYSTEM_PROMPT


def test_app_packaging_rejects_symlinked_skill_content(tmp_path):
    script = ROOT / "packages/macos/scripts/build.py"
    sys.path.insert(0, str(script.parent))
    try:
        spec = importlib.util.spec_from_file_location("ogcua_build_symlink_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(script.parent))
    source = tmp_path / "source"
    folder = source / "example"
    folder.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    (folder / "SKILL.md").symlink_to(secret)
    with pytest.raises(ValueError, match="symlink"):
        module.copy_bundled_skills(source, tmp_path / "bundle/skills")
