import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

SITE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("site_build", SITE / "build.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "src"
        self.source.mkdir()
        (self.source / "index.html").write_text(
            '<title>Home</title><main id="main"><a href="contributing.html#rules">Contribute</a></main>'
        )
        (self.source / "contributing.html").write_text(
            '<title>Contribute</title><main id="rules"><a href="index.html#main">Home</a></main>'
        )
        self.output = self.root / "dist"

    def test_build_is_repeatable_and_removes_stale_output(self):
        builder.build(self.source, self.output)
        first = {p.name: p.read_bytes() for p in self.output.iterdir()}
        self.assertIn(".nojekyll", first)
        self.assertEqual(first["index.html"], (self.source / "index.html").read_bytes())
        (self.output / "stale.html").write_text("obsolete")
        builder.build(self.source, self.output)
        self.assertEqual(first, {p.name: p.read_bytes() for p in self.output.iterdir()})

    def test_missing_local_link_blocks_build(self):
        (self.source / "contributing.html").unlink()
        with self.assertRaisesRegex(ValueError, "Missing source page"):
            builder.build(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_anchor_blocks_build(self):
        (self.source / "index.html").write_text('<title>Home</title><a href="contributing.html#absent">Bad</a>')
        with self.assertRaisesRegex(ValueError, "Missing anchor"):
            builder.build(self.source, self.output)

    def test_internal_repository_links_block_build(self):
        (self.source / "index.html").write_text('<title>Home</title>https://code.alibaba-inc.com/example')
        with self.assertRaisesRegex(ValueError, "Internal infrastructure"):
            builder.build(self.source, self.output)

    def test_project_path_and_source_escape_are_rejected(self):
        for href in ("/contributing.html", "../outside.html"):
            with self.subTest(href=href):
                (self.source / "index.html").write_text(f'<title>Home</title><a href="{href}">Bad</a>')
                with self.assertRaises(ValueError):
                    builder.build(self.source, self.output)

    def test_output_cannot_replace_sources(self):
        for output in (self.source, self.root, self.source / "dist"):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, "separate"):
                builder.build(self.source, output)
        self.assertTrue((self.source / "index.html").is_file())


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "checkout"
        self.remote = self.root / "origin.git"
        self.repo.mkdir()
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "core.hooksPath", "/dev/null")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "user.name", "Site test")
        self.git("config", "user.email", "site@example.invalid")
        (self.repo / "source-only.txt").write_text("not a deployment asset")
        self.git("add", "source-only.txt")
        self.git("commit", "--quiet", "-m", "Source")
        self.main = self.git("rev-parse", "HEAD")
        self.git("init", "--quiet", "--bare", str(self.remote))
        self.git("remote", "add", "origin", self.remote.as_uri())
        self.git("push", "--quiet", "origin", "main")
        self.dist = self.root / "dist"
        self.dist.mkdir()
        for name in ("index.html", "contributing.html", ".nojekyll"):
            (self.dist / name).write_text("" if name.startswith(".") else "page")

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.repo, text=True, capture_output=True)
        if result.returncode:
            self.fail(result.stderr)
        return result.stdout.strip()

    def publish(self):
        result = subprocess.run(["bash", str(SITE / "publish.sh"), str(self.dist)],
                                cwd=self.repo, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.main)
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_first_publish_updates_and_noop_preserve_branch_history(self):
        (self.dist / "stale.css").write_text("old")
        self.publish()
        first = self.git("--git-dir", str(self.remote), "rev-parse", "site")
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-list", "--count", "site"), "1")
        self.assertNotIn("source-only.txt", self.git("--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "site"))

        (self.dist / "stale.css").unlink()
        (self.dist / "index.html").write_text("updated page")
        self.publish()
        second = self.git("--git-dir", str(self.remote), "rev-parse", "site")
        self.assertNotEqual(first, second)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "site^"), first)
        tree = self.git("--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "site").splitlines()
        self.assertEqual(tree, [".nojekyll", "contributing.html", "index.html"])
        self.publish()
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "site"), second)
        self.assertEqual(len(self.git("worktree", "list", "--porcelain").split("worktree ")) - 1, 1)


if __name__ == "__main__":
    unittest.main()
