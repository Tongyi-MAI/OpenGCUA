from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from resume_catalog import ResumeError, inspect_resume_run, recent_resume_runs


def saved_run(root: Path, name: str, *, ui_api: str = "mai-ui", task: str = "find a file") -> Path:
    run = root / name
    run.mkdir(parents=True)
    (run / "traj.json").write_text(json.dumps({
        "session_id": f"session-{name}", "status": "success",
        "config": {"ui_api": ui_api, "ui_api_version": "mai-ui/0.3-sdk-macos"},
        "tasks": [{"task": task, "status": "success"}],
        "steps": [{"step": 0, "turn": {"user": task}}],
        "todos": [{"text": "next"}],
    }), encoding="utf-8")
    return run


def test_recent_runs_are_newest_first_limited_and_exclude_active(tmp_path: Path):
    runs = [saved_run(tmp_path, f"run-{i:02}") for i in range(12)]
    for i, run in enumerate(runs):
        os.utime(run / "traj.json", (1000 + i, 1000 + i))
    chosen = recent_resume_runs(tmp_path, current_run=runs[11], ui_api="mai-ui")
    assert len(chosen) == 10
    assert [item.path for item in chosen] == list(reversed(runs[1:11]))
    assert chosen[0].last_task == "find a file"
    assert chosen[0].status == "success"
    assert chosen[0].turns == 1
    assert chosen[0].todos == 1


def test_catalog_skips_bad_entries_and_empty_root(tmp_path: Path):
    assert recent_resume_runs(tmp_path, current_run=None, ui_api="mai-ui") == ()
    saved_run(tmp_path, "good")
    saved_run(tmp_path, "legacy", ui_api="legacy-cua")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "traj.json").write_text("{broken", encoding="utf-8")
    empty = saved_run(tmp_path, "no-task")
    data = json.loads((empty / "traj.json").read_text())
    data["tasks"] = []
    (empty / "traj.json").write_text(json.dumps(data))
    assert [item.path.name for item in recent_resume_runs(tmp_path, current_run=None, ui_api="mai-ui")] == ["good"]


def test_explicit_run_outside_root_with_spaces_is_read_only(tmp_path: Path):
    run = saved_run(tmp_path / "outside root", "run with spaces")
    before = (run / "traj.json").read_bytes()
    candidate = inspect_resume_run(run, ui_api="mai-ui")
    assert candidate.path == run.resolve()
    assert candidate.session_id == "session-run with spaces"
    assert (run / "traj.json").read_bytes() == before
    assert not (run / "runtime").exists()


def test_symlink_to_incompatible_target_is_rejected(tmp_path: Path):
    bad = saved_run(tmp_path, "bad", ui_api="legacy-cua")
    link = tmp_path / "linked"
    link.symlink_to(bad, target_is_directory=True)
    with pytest.raises(ResumeError, match="UI API"):
        inspect_resume_run(link, ui_api="mai-ui")


def test_discovery_does_not_follow_symlink_outside_root(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir()
    outside = saved_run(tmp_path, "outside")
    (root / "shortcut").symlink_to(outside, target_is_directory=True)
    assert recent_resume_runs(root, current_run=None, ui_api="mai-ui") == ()
