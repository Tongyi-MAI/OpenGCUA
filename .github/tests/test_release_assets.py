"""Exercise the checks that prevent incomplete or mismatched releases."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release_assets.py"
spec = importlib.util.spec_from_file_location("release_assets", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
VERSION = "0.1.0"
SHA = "a" * 40


def package_metadata(version=VERSION):
    return f"Metadata-Version: 2.4\nName: qwen-intelligence-sdk\nVersion: {version}\n\n"


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        for python in release.PYTHONS:
            for platform in ("macosx_14_0_arm64", "manylinux_2_28_x86_64"):
                wheel = self.assets / f"{release.WHEEL_PREFIX}-{VERSION}-{python}-{python}-{platform}.whl"
                self.write_wheel(wheel)
        with tarfile.open(self.assets / f"{release.WHEEL_PREFIX}-{VERSION}.tar.gz", "w:gz") as archive:
            content = package_metadata().encode()
            info = tarfile.TarInfo(f"{release.WHEEL_PREFIX}-{VERSION}/PKG-INFO")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        (self.assets / release.DMG).write_bytes(b"fixture disk image")
        digest = hashlib.sha256(b"fixture disk image").hexdigest()
        (self.assets / f"{release.DMG}.sha256").write_text(f"{digest}  {release.DMG}\n")

    def write_wheel(self, path, version=VERSION):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"{release.WHEEL_PREFIX}-{VERSION}.dist-info/METADATA", package_metadata(version))

    def test_stable_and_prerelease_versions(self):
        stable = release.release_metadata("v0.1.0", VERSION, SHA)
        self.assertEqual(stable["app_version"], VERSION)
        self.assertEqual(stable["prerelease"], "false")
        for version in ("0.2.0a1", "0.2.0b2", "0.2.0rc3"):
            metadata = release.release_metadata(f"v{version}", version, SHA)
            self.assertEqual(metadata["app_version"], "0.2.0")
            self.assertEqual(metadata["prerelease"], "true")

    def test_reject_mismatched_or_malformed_source(self):
        for tag, version, sha in (("v0.2.0", VERSION, SHA), ("v0.1.0\nother=x", VERSION, SHA),
                                  ("v0.1.0", VERSION, "main"), ("v0.1.0-dev", "0.1.0-dev", SHA)):
            with self.subTest(tag=tag, version=version, sha=sha), self.assertRaises(ValueError):
                release.release_metadata(tag, version, sha)

    def test_complete_downloads_and_reproducible_checksums(self):
        metadata = release.release_metadata("v0.1.0", VERSION, SHA)
        notes = self.root / "notes.md"
        release.write_downloads(self.assets, metadata, notes)
        manifest = (self.assets / "SHA256SUMS").read_text()
        self.assertEqual(len(manifest.splitlines()), 11)
        for line in manifest.splitlines():
            digest, name = line.split("  ")
            self.assertEqual(digest, hashlib.sha256((self.assets / name).read_bytes()).hexdigest())
        self.assertIn(SHA, notes.read_text())
        release.write_downloads(self.assets, metadata, notes)
        self.assertEqual(manifest, (self.assets / "SHA256SUMS").read_text())

    def test_missing_wheel_blocks_publication(self):
        next(self.assets.glob("*.whl")).unlink()
        with self.assertRaisesRegex(ValueError, "eight SDK wheels"):
            release.validate_assets(self.assets, VERSION)

    def test_duplicate_target_blocks_publication(self):
        linux = next(self.assets.glob("*cp311-cp311-manylinux*"))
        linux.rename(linux.with_name(linux.name.replace("manylinux_2_28_x86_64", "macosx_15_0_arm64")))
        with self.assertRaisesRegex(ValueError, "Duplicate wheel target"):
            release.validate_assets(self.assets, VERSION)

    def test_wrong_embedded_version_blocks_publication(self):
        self.write_wheel(next(self.assets.glob("*.whl")), "9.0.0")
        with self.assertRaisesRegex(ValueError, "package name/version"):
            release.validate_assets(self.assets, VERSION)

    def test_corrupt_wheel_blocks_publication(self):
        next(self.assets.glob("*.whl")).write_bytes(b"not a wheel")
        with self.assertRaises(zipfile.BadZipFile):
            release.validate_assets(self.assets, VERSION)

    def test_missing_sdist_blocks_publication(self):
        next(self.assets.glob("*.tar.gz")).unlink()
        with self.assertRaises(FileNotFoundError):
            release.validate_assets(self.assets, VERSION)

    def test_modified_dmg_blocks_publication(self):
        (self.assets / release.DMG).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "DMG checksum"):
            release.validate_assets(self.assets, VERSION)

    def test_nonportable_dmg_checksum_blocks_publication(self):
        checksum = self.assets / f"{release.DMG}.sha256"
        checksum.write_text(checksum.read_text().replace(release.DMG, f"build/{release.DMG}"))
        with self.assertRaisesRegex(ValueError, "portable download filename"):
            release.validate_assets(self.assets, VERSION)

    def test_unexpected_files_block_publication(self):
        (self.assets / "local.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Unexpected files"):
            release.validate_assets(self.assets, VERSION)

    def test_unexpected_architecture_blocks_publication(self):
        linux = next(self.assets.glob("*manylinux*"))
        linux.rename(linux.with_name(linux.name.replace("x86_64", "aarch64")))
        with self.assertRaisesRegex(ValueError, "Unexpected platform"):
            release.validate_assets(self.assets, VERSION)


if __name__ == "__main__":
    unittest.main()
