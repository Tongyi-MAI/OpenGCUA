from pathlib import Path

import pytest

from skill_registry import SkillRegistry, parse_explicit_skill


def skill(root: Path, name: str, description: str = "Useful workflow", body: str = "Do the work.") -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8"
    )
    return folder


def test_discovery_sources_and_no_cwd_scan(tmp_path, monkeypatch):
    bundled, personal, extra, cwd = (tmp_path / x for x in ("bundle", "personal", "extra", "cwd"))
    for root in (bundled, personal, extra, cwd):
        root.mkdir()
    skill(bundled, "pdf-form")
    skill(personal, "mail-draft")
    skill(extra, "web-research")
    skill(cwd, "surprise")
    monkeypatch.chdir(cwd)
    registry = SkillRegistry(bundled, personal, (extra,))
    infos = registry.discover()
    assert {s.name for s in infos} == {"pdf-form", "mail-draft", "web-research"}
    assert {s.source for s in infos} == {"bundled", "personal", str(extra)}
    assert registry.load("pdf-form").body.strip() == "Do the work."


def test_collision_is_not_silently_shadowed(tmp_path):
    bundled, personal = tmp_path / "bundle", tmp_path / "personal"
    skill(bundled, "same")
    skill(personal, "same")
    registry = SkillRegistry(bundled, personal)
    assert registry.discover() == ()
    assert any("same" in message and "duplicate" in message.lower() for message in registry.diagnostics)
    with pytest.raises(ValueError, match="same"):
        registry.load("same")


def test_malformed_and_oversized_metadata_are_skipped(tmp_path):
    bundled = tmp_path / "bundle"
    bad = skill(bundled, "bad")
    (bad / "SKILL.md").write_text("---\nname: bad\ndescription:\n  nested: no\n---\n", encoding="utf-8")
    skill(bundled, "huge", description="x" * 241)
    registry = SkillRegistry(bundled, tmp_path / "personal")
    assert registry.discover() == ()
    assert len(registry.diagnostics) == 2


def test_symlink_and_relative_reference_escape_rejected(tmp_path):
    bundled = tmp_path / "bundle"
    folder = skill(bundled, "safe")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (folder / "linked.txt").symlink_to(outside)
    registry = SkillRegistry(bundled, tmp_path / "personal")
    assert registry.discover()[0].name == "safe"
    for relative in ("../secret.txt", "/tmp/secret.txt", "linked.txt"):
        with pytest.raises(ValueError):
            registry.read("safe", relative)
    assert "secret" not in " ".join(registry.diagnostics)
    (folder / "local.txt").write_text("allowed", encoding="utf-8")
    assert registry.read("safe", "local.txt") == "allowed"


def test_skill_directory_symlink_escape_is_rejected(tmp_path):
    bundled = tmp_path / "bundle"
    bundled.mkdir()
    outside = skill(tmp_path / "elsewhere", "linked")
    (bundled / "linked").symlink_to(outside, target_is_directory=True)
    registry = SkillRegistry(bundled, tmp_path / "personal")
    assert registry.discover() == ()
    assert registry.diagnostics


def test_looping_skill_document_symlink_is_diagnostic_not_startup_failure(tmp_path):
    bundled = tmp_path / "bundle"
    folder = skill(bundled, "looped")
    document = folder / "SKILL.md"
    document.unlink()
    document.symlink_to("SKILL.md")
    skill(bundled, "healthy")
    registry = SkillRegistry(bundled, tmp_path / "personal")

    assert [item.name for item in registry.discover()] == ["healthy"]
    assert any("looped" in message for message in registry.diagnostics)


def test_symlink_loops_after_discovery_become_validation_errors(tmp_path):
    bundled = tmp_path / "bundle"
    folder = skill(bundled, "example")
    reference = folder / "reference.md"
    reference.write_text("Safe reference")
    registry = SkillRegistry(bundled, tmp_path / "personal")
    registry.discover()
    reference.unlink()
    reference.symlink_to("reference.md")
    with pytest.raises(ValueError, match="reference unavailable"):
        registry.read("example", "reference.md")
    document = folder / "SKILL.md"
    document.unlink()
    document.symlink_to("SKILL.md")
    with pytest.raises(ValueError, match="document unavailable"):
        registry.load("example")


def test_create_personal_template_refuses_overwrite(tmp_path):
    registry = SkillRegistry(tmp_path / "bundle", tmp_path / "personal")
    folder = registry.create("my-workflow")
    assert folder == tmp_path / "personal" / "my-workflow"
    assert "name: my-workflow" in (folder / "SKILL.md").read_text()
    with pytest.raises(FileExistsError):
        registry.create("my-workflow")


def test_explicit_marker_only_exact_known_name():
    clean, names = parse_explicit_skill("Please use $pdf-form and $unknown here", {"pdf-form"})
    assert clean == "Please use and $unknown here"
    assert names == ("pdf-form",)
    assert parse_explicit_skill("$pdf-form-extra", {"pdf-form"}) == ("$pdf-form-extra", ())


def test_skill_dir_cli_destination():
    from mac_agent import build_parser

    args = build_parser().parse_args(["--skills-dir", "/tmp/local-skills"])
    assert args.skills_dirs == [Path("/tmp/local-skills")]
