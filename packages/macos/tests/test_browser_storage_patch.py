"""构建补丁严格匹配，登录态临时页不激活，普通建页保持原语义。"""
import sys
import tempfile
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from browser_storage_patch import ORIGINAL, PATCHED, patch_storage_helpers


class BrowserStoragePatchTests(unittest.TestCase):
    def fixture(self, root, creation):
        bundle = Path(root) / 'lib/coreBundle.js'
        bundle.parent.mkdir(parents=True)
        bundle.write_text('this._creatingStorageStatePage = !!forStorageState;\n'
                          'isCreatingStorageStatePage() {}\n' + creation, encoding='utf-8')
        return bundle

    def test_patch_is_scoped_to_storage_helpers_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            bundle = self.fixture(root, ORIGINAL)
            patch_storage_helpers(Path(root))
            self.assertIn(PATCHED, bundle.read_text())
            self.assertIn('this.isCreatingStorageStatePage() ? { background: true } : {}', bundle.read_text())
            content = bundle.read_text()
            patch_storage_helpers(Path(root))
            self.assertEqual(bundle.read_text(), content)

    def test_dependency_upgrade_must_not_silently_skip_patch(self):
        for changed in ('changed API', ORIGINAL + '\n' + ORIGINAL):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as root:
                bundle = self.fixture(root, changed)
                content = bundle.read_text()
                with self.assertRaises(ValueError):
                    patch_storage_helpers(Path(root))
                self.assertEqual(bundle.read_text(), content)

    def test_missing_helper_flag_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            bundle = self.fixture(root, ORIGINAL)
            bundle.write_text(ORIGINAL)
            with self.assertRaises(ValueError):
                patch_storage_helpers(Path(root))


if __name__ == '__main__':
    unittest.main()
