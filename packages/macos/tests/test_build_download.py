"""构建驱动下载：分段续传、响应校验和最终 hash 均使用离线夹具。"""
import hashlib
import importlib.util
import io
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("macos_build_download", SCRIPTS / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class Response(io.BytesIO):
    status = 206

    def __init__(self, body, start, end, total):
        super().__init__(body)
        self.headers = {"Content-Range": f"bytes {start}-{end}/{total}"}


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Path(self.temp.name) / "driver.tar.gz"
        self.data = b"0123456789abcdefghijklmnop"
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.requests = []

    def response(self, request, timeout):
        start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", request.get_header("Range")).groups())
        self.requests.append((start, end))
        return Response(self.data[start:end + 1], start, end, len(self.data))

    def download(self):
        build.download_archive("https://example.invalid/driver", self.archive, self.digest,
                               chunk_size=4, workers=2)

    def test_verified_cache_never_calls_network(self):
        self.archive.write_bytes(self.data)
        with patch.object(build.urllib.request, "urlopen", side_effect=AssertionError("network")):
            self.download()
        self.assertEqual(self.archive.read_bytes(), self.data)

    def test_truncated_old_download_resumes_and_promotes_only_after_hash(self):
        self.archive.write_bytes(self.data[:8])
        with patch.object(build.urllib.request, "urlopen", side_effect=self.response):
            self.download()
        self.assertEqual(self.archive.read_bytes(), self.data)
        self.assertIn((8, 11), self.requests)
        self.assertNotIn((0, 3), self.requests)
        self.assertFalse(self.archive.with_name(self.archive.name + ".part").exists())

    def test_bad_content_range_never_promotes_cache(self):
        def wrong(request, timeout):
            result = self.response(request, timeout)
            if request.get_header("Range") != "bytes=0-0":
                result.headers["Content-Range"] = "bytes 0-3/26"
            return result
        with patch.object(build.urllib.request, "urlopen", side_effect=wrong):
            with self.assertRaisesRegex(RuntimeError, "Content-Range"):
                self.download()
        self.assertFalse(self.archive.exists())

    def test_short_segment_is_not_appended(self):
        partial = self.archive.with_name(self.archive.name + ".part")
        partial.write_bytes(self.data[:8])
        def short(request, timeout):
            result = self.response(request, timeout)
            if request.get_header("Range") != "bytes=0-0":
                result = Response(b"x", 8, 11, len(self.data))
            return result
        with patch.object(build.urllib.request, "urlopen", side_effect=short):
            with self.assertRaisesRegex(RuntimeError, "segment"):
                self.download()
        self.assertEqual(partial.read_bytes(), self.data[:8])
        self.assertFalse(self.archive.exists())

    def test_transient_network_failure_retries_segment(self):
        attempts = {}
        def flaky(request, timeout):
            key = request.get_header("Range")
            attempts[key] = attempts.get(key, 0) + 1
            if key != "bytes=0-0" and attempts[key] == 1:
                raise TimeoutError("temporary interruption")
            return self.response(request, timeout)
        with patch.object(build.urllib.request, "urlopen", side_effect=flaky):
            self.download()
        self.assertEqual(self.archive.read_bytes(), self.data)
        self.assertEqual(attempts["bytes=0-3"], 2)

    def test_server_ignoring_range_is_rejected(self):
        def ignored(request, timeout):
            result = Response(self.data, 0, 0, len(self.data))
            result.status = 200
            return result
        with patch.object(build.urllib.request, "urlopen", side_effect=ignored):
            with self.assertRaisesRegex(RuntimeError, "Content-Range"):
                self.download()
        self.assertFalse(self.archive.exists())

    def test_hash_mismatch_never_promotes_archive(self):
        self.digest = "0" * 64
        with patch.object(build.urllib.request, "urlopen", side_effect=self.response):
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                self.download()
        self.assertFalse(self.archive.exists())


if __name__ == "__main__":
    unittest.main()
