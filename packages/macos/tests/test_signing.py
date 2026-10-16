import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import subprocess

spec = importlib.util.spec_from_file_location("signing", Path(__file__).parents[1] / "scripts/signing.py")
signing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(signing)


class SigningSelectionTests(unittest.TestCase):
    def test_timestamp_failure_retries_without_weakening_signing(self):
        failed = subprocess.CompletedProcess([], 1, "", "The timestamp service is not available.")
        passed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(signing.subprocess, "run", side_effect=[failed, passed]) as run, patch.object(signing.time, "sleep"):
            signing.sign("--timestamp", "--sign", "certificate", "app")
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0], run.call_args_list[1])

    def test_other_signing_errors_fail_immediately(self):
        failed = subprocess.CompletedProcess([], 1, "", "Identity is invalid")
        with patch.object(signing.subprocess, "run", return_value=failed) as run:
            with self.assertRaises(RuntimeError):
                signing.sign("--sign", "certificate", "app")
            self.assertEqual(run.call_count, 1)

    def test_certificate_is_selected_and_saved_identity_wins(self):
        ids = {"A" * 40: "Apple Development: Example", "B" * 40: "Developer ID Application: Example"}
        self.assertEqual(signing.select_identity(None, ids, "A" * 40), "A" * 40)
        self.assertEqual(signing.select_identity("Developer ID Application: Example", ids), "B" * 40)
        self.assertEqual(signing.select_identity(None, {"A" * 40: ids["A" * 40]}), "A" * 40)

    def test_missing_pin_never_falls_back_to_different_identity(self):
        with self.assertRaises(ValueError):
            signing.select_identity(None, {"B" * 40: "Apple Development: Other"}, "A" * 40)

    def test_multiple_certificates_require_an_explicit_choice(self):
        with self.assertRaises(ValueError):
            signing.select_identity(None, {"A" * 40: "Apple Development: A", "B" * 40: "Apple Development: B"})

    def test_unsigned_ci_is_explicit_and_release_can_reject_it(self):
        self.assertEqual(signing.select_identity(None, {}), "-")
        self.assertEqual(signing.select_identity("-", {}, "A" * 40), "-")
        for request in (None, "-"):
            with self.assertRaises(ValueError):
                signing.select_identity(request, {}, require_stable=True)


if __name__ == "__main__":
    unittest.main()
