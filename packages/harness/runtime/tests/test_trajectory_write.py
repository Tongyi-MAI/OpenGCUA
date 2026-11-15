"""Atomic trajectory publication under transient Windows reader contention."""

import json

import mac_agent
import pytest


@pytest.fixture
def writer(tmp_path, monkeypatch):
    # Test publication independently of native Windows directory ACL setup.
    monkeypatch.setattr(mac_agent, "IS_WINDOWS", False)
    return mac_agent.TrajectoryWriter(tmp_path, "fixture", "write-test", {"ui_api": "mai-ui"})


def windows_error(code):
    error = OSError("simulated replacement failure")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", [5, 32, 33])
def test_transient_sharing_error_publishes_complete_trajectory(writer, monkeypatch, code):
    monkeypatch.setattr(mac_agent, "IS_WINDOWS", True)
    original = writer.path.read_bytes()
    writer.data["answer"] = "New complete result"
    writer.data["steps"] = [{"step": 0}]
    temporary = writer.path.with_suffix(".json.tmp")
    real_replace = mac_agent.os.replace
    attempts, delays, synchronized = [], [], []
    monkeypatch.setattr(mac_agent.time, "sleep", delays.append)
    monkeypatch.setattr(writer, "_sync_step_metadata", synchronized.append)

    def replace(source, destination):
        attempts.append((source, destination))
        assert writer.path.read_bytes() == original
        assert json.loads(temporary.read_text("utf-8")) == writer.data
        assert not synchronized
        if len(attempts) < 3:
            raise windows_error(code)
        real_replace(source, destination)

    monkeypatch.setattr(mac_agent.os, "replace", replace)
    writer.flush()
    assert len(attempts) == 3
    assert delays == [0.02, 0.04]
    assert json.loads(writer.path.read_text("utf-8")) == writer.data
    assert not temporary.exists()
    assert synchronized == [{"step": 0}]


@pytest.mark.parametrize("code", [5, 32, 33])
def test_persistent_sharing_error_is_bounded_and_preserves_both_files(writer, monkeypatch, code):
    monkeypatch.setattr(mac_agent, "IS_WINDOWS", True)
    original = writer.path.read_bytes()
    writer.data["answer"] = "Unpublished result"
    temporary = writer.path.with_suffix(".json.tmp")
    error = windows_error(code)
    attempts, delays = [], []
    monkeypatch.setattr(mac_agent.time, "sleep", delays.append)

    def replace(source, destination):
        attempts.append((source, destination))
        raise error

    monkeypatch.setattr(mac_agent.os, "replace", replace)
    with pytest.raises(OSError) as raised:
        writer.flush()
    assert raised.value is error
    assert len(attempts) == 6
    assert sum(delays) == pytest.approx(0.62)
    assert writer.path.read_bytes() == original
    assert json.loads(temporary.read_text("utf-8")) == writer.data


@pytest.mark.parametrize("windows,code", [(False, 5), (True, 2), (True, 112), (True, None)])
def test_other_replacement_errors_are_not_retried(writer, monkeypatch, windows, code):
    monkeypatch.setattr(mac_agent, "IS_WINDOWS", windows)
    original = writer.path.read_bytes()
    error = windows_error(code)
    attempts = []

    def replace(*paths):
        attempts.append(paths)
        raise error

    def sleep(_delay):
        pytest.fail("non-sharing replacement failures must propagate immediately")

    monkeypatch.setattr(mac_agent.os, "replace", replace)
    monkeypatch.setattr(mac_agent.time, "sleep", sleep)
    with pytest.raises(OSError) as raised:
        writer.flush()
    assert raised.value is error
    assert len(attempts) == 1
    assert writer.path.read_bytes() == original


@pytest.mark.parametrize("metadata", [False, True])
def test_step_publication_also_retries_transient_sharing(writer, monkeypatch, metadata):
    monkeypatch.setattr(mac_agent, "IS_WINDOWS", True)
    path = writer.steps_dir / "step_0.json"
    path.write_text('{"step": 0, "status": "running"}', encoding="utf-8")
    real_replace = mac_agent.os.replace
    attempts, delays = [], []
    monkeypatch.setattr(mac_agent.time, "sleep", delays.append)

    def replace(source, destination):
        attempts.append((source, destination))
        if len(attempts) == 1:
            raise windows_error(32)
        real_replace(source, destination)

    monkeypatch.setattr(mac_agent.os, "replace", replace)
    if metadata:
        writer._sync_step_metadata({"step": 0, "metrics": {"status": "completed"}})
    else:
        writer._write_step_file(0, {"status": "completed"})
    assert json.loads(path.read_text("utf-8"))["status"] == "completed"
    assert len(attempts) == 2
    assert delays == [0.02]
    assert not path.with_suffix(".json.tmp").exists()
