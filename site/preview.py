#!/usr/bin/env python3
"""Preview the project site with optional media from the separate asset repository."""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, media=None, **kwargs):
        self.media = media.resolve() if media else None
        self.remaining = None
        super().__init__(*args, **kwargs)

    def translate_path(self, path):
        relative = unquote(urlsplit(path).path)
        if relative.startswith("/OpenGCUA/"):
            relative = relative[len("/OpenGCUA"):]
        if relative.startswith("/media/") and self.media:
            target = (self.media / relative[len("/media/"):]).resolve()
            if target.is_relative_to(self.media):
                return str(target)
            return str(self.media / ".missing")
        return super().translate_path(relative)

    def send_head(self):
        path = Path(self.translate_path(self.path))
        if not path.is_file() or not self.headers.get("Range"):
            return super().send_head()
        size = path.stat().st_size
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers["Range"])
        if not match or not any(match.groups()):
            self.send_error(416)
            return None
        first, last = match.groups()
        start = int(first) if first else max(0, size - int(last))
        end = min(int(last), size - 1) if first and last else size - 1
        if start >= size or end < start:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        file = path.open("rb")
        file.seek(start)
        self.remaining = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(self.remaining))
        self.end_headers()
        return file

    def copyfile(self, source, output):
        if self.remaining is None:
            return super().copyfile(source, output)
        try:
            while self.remaining:
                chunk = source.read(min(self.remaining, 256 * 1024))
                if not chunk:
                    break
                output.write(chunk)
                self.remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8879)
    parser.add_argument("--media", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent / "src"
    handler = partial(Handler, directory=str(root), media=args.media)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Preview: http://127.0.0.1:{args.port}/OpenGCUA/trajectories.html?assets=/media/", flush=True)
    server.serve_forever()
