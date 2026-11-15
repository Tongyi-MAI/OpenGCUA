"""Offline installer integrity tests; no driver execution, downloads, or desktop access."""

import hashlib
import importlib.util
from pathlib import Path
import zipfile

import pytest


@pytest.fixture
def setup_module():
    path = Path(__file__).resolve().parents[1] / "python/mai_ui/windows_setup.py"
    spec = importlib.util.spec_from_file_location("windows_setup_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_fixture(module, path, *, extra=None):
    with zipfile.ZipFile(path, "w") as archive:
        for name in module.MEMBERS:
            archive.writestr(name, f"fixture for {name}")
        if extra:
            archive.writestr(extra, "unexpected")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_corrupt_archive_changes_nothing(setup_module, tmp_path):
    archive = tmp_path / "driver.zip"
    archive_fixture(setup_module, archive)
    destination = tmp_path / "stage"
    destination.mkdir()
    with pytest.raises(ValueError, match="SHA256"):
        setup_module._extract_verified(archive, destination)
    assert not list(destination.iterdir())


@pytest.mark.parametrize("extra", ["../escape.exe", "nested/cua-driver.exe", "unexpected.dll"])
def test_archive_members_are_exact_before_extraction(setup_module, tmp_path, monkeypatch, extra):
    archive = tmp_path / "driver.zip"
    digest = archive_fixture(setup_module, archive, extra=extra)
    monkeypatch.setattr(setup_module, "ARCHIVE_SHA256", digest)
    destination = tmp_path / "stage"
    destination.mkdir()
    with pytest.raises(ValueError, match="Unexpected files"):
        setup_module._extract_verified(archive, destination)
    assert not list(destination.iterdir())
    assert not (tmp_path / "escape.exe").exists()


def test_verified_archive_retains_required_uia_helper(setup_module, tmp_path, monkeypatch):
    archive = tmp_path / "driver.zip"
    monkeypatch.setattr(setup_module, "ARCHIVE_SHA256", archive_fixture(setup_module, archive))
    destination = tmp_path / "stage"
    destination.mkdir()
    hashes = setup_module._extract_verified(archive, destination)
    assert set(hashes) == setup_module.MEMBERS
    assert (destination / "cua-driver-uia.exe").read_text() == "fixture for cua-driver-uia.exe"
    for name, digest in hashes.items():
        assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == digest


def test_resolution_never_searches_path_or_accepts_relative_executable(
    setup_module, tmp_path, monkeypatch
):
    monkeypatch.setenv("MAI_UI_WINDOWS_DRIVER", "cua-driver.exe")
    with pytest.raises(ValueError, match="absolute"):
        setup_module.resolve_driver()
    file = tmp_path / "explicit.exe"
    file.write_bytes(b"fixture")
    assert setup_module.resolve_driver(str(file)) == file.resolve()


def test_setup_hash_failure_preserves_previous_installation(setup_module, tmp_path, monkeypatch):
    root = tmp_path / "installed"
    root.mkdir()
    previous = root / "cua-driver.exe"
    previous.write_bytes(b"previous installation")
    archive = tmp_path / "untrusted.zip"
    archive_fixture(setup_module, archive)
    monkeypatch.setattr(setup_module.sys, "platform", "win32")
    monkeypatch.setattr(setup_module.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("MAI_UI_WINDOWS_HOME", str(root))
    with pytest.raises(ValueError, match="SHA256"):
        setup_module.setup(archive=str(archive), replace=True)
    assert previous.read_bytes() == b"previous installation"


def test_non_windows_setup_fails_before_network_or_files(setup_module, tmp_path, monkeypatch):
    monkeypatch.setattr(setup_module.sys, "platform", "linux")
    monkeypatch.setenv("MAI_UI_WINDOWS_HOME", str(tmp_path / "never-created"))
    monkeypatch.setattr(setup_module, "_download", lambda path: pytest.fail("network must not run"))
    with pytest.raises(ValueError, match="native Windows"):
        setup_module.setup()
    assert not (tmp_path / "never-created").exists()


def test_failed_publication_restores_complete_previous_directory(
    setup_module, tmp_path, monkeypatch
):
    root, staged, backup = [tmp_path / name for name in ("installed", "staged", "backup")]
    root.mkdir()
    staged.mkdir()
    (root / "cua-driver.exe").write_bytes(b"old executable")
    (root / "cua-driver-uia.exe").write_bytes(b"old helper")
    (staged / "cua-driver.exe").write_bytes(b"new executable")
    original_replace = setup_module.os.replace

    def fail_publication(source, destination):
        if source == staged:
            raise PermissionError("controlled publication failure")
        return original_replace(source, destination)

    monkeypatch.setattr(setup_module.os, "replace", fail_publication)
    with pytest.raises(PermissionError, match="controlled"):
        setup_module._publish(staged, root, backup)
    assert (root / "cua-driver.exe").read_bytes() == b"old executable"
    assert (root / "cua-driver-uia.exe").read_bytes() == b"old helper"
    assert not backup.exists()


def test_replace_refuses_unrelated_directory_contents(setup_module, tmp_path):
    root, staged = tmp_path / "installed", tmp_path / "staged"
    root.mkdir()
    staged.mkdir()
    (root / "personal.txt").write_text("keep this")
    with pytest.raises(ValueError, match="unrelated"):
        setup_module._publish(staged, root, tmp_path / "backup")
    assert (root / "personal.txt").read_text() == "keep this"
