import importlib.util
import json
from pathlib import Path
import unittest

SITE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_trajectories", SITE / "prepare_trajectories.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class PublicationTests(unittest.TestCase):
    def test_redaction_preserves_scores_frames_and_originals(self):
        original = {"source_dir": "/home/admin/private", "frame_index": 42, "score": 0.7,
                    "feedback": "AKIA" + "A" * 16 + " https://private.alibaba-inc.com /Users/private/data 10.1.2.3 password is 'example-secret'"}
        clean = prepare.sanitize(original)
        self.assertNotIn("source_dir", clean)
        self.assertEqual((clean["frame_index"], clean["score"]), (42, .7))
        self.assertNotIn("alibaba-inc", clean["feedback"])
        self.assertNotIn("AKIA", clean["feedback"])
        self.assertNotIn("example-secret", clean["feedback"])
        self.assertIn("source_dir", original)

    def test_latest_evaluated_retry_is_selected_not_best_score(self):
        records = {}
        for key, score in [("a", 1), ("b", .2), ("c", None)]:
            records[key] = {"task_id": "001", "run_id": key, "traj": [],
                            "result": f"score: {score}" if score is not None else None}
        task = prepare.task_index(records)[0]
        self.assertEqual(task["default_run"], "b")
        self.assertEqual(len(task["runs"]), 3)

    def test_public_indexes_match_complete_archive(self):
        root = SITE / "src/trajectory-data"
        catalog = json.loads((root / "catalog.json").read_text())
        self.assertEqual(catalog["leaderboard_tasks"], 82)
        self.assertEqual(catalog["asset_base"], "https://tongyi-mai.github.io/OpenGCUA-blog/")
        self.assertEqual(len(catalog["models"]), 8)
        attempts = 0
        for model in catalog["models"]:
            tasks = json.loads((SITE / "src" / model["index"]).read_text())["tasks"]
            self.assertEqual(len(tasks), 108)
            self.assertEqual(len({task["id"] for task in tasks}), 108)
            self.assertEqual(sum(len(t["runs"]) for t in tasks), model["record_count"])
            for task in tasks:
                self.assertIn(task["default_run"], [r["key"] for r in task["runs"]])
            attempts += model["record_count"]
            self.assertRegex(model["zip_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(attempts, 3480)
        for path in root.glob("*.json"):
            text = path.read_text()
            for pattern, _ in prepare.REDACTIONS:
                self.assertIsNone(pattern.search(text), f"Unredacted public index: {path.name}")


if __name__ == "__main__":
    unittest.main()
