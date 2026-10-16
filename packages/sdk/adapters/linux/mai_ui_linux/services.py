"""Local user services. All spawned commands have bounded output and owned process groups."""

import base64
import ctypes
import errno
import os
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from common import Error, b64, completed


def io_error(exc):
    code = {
        errno.ENOENT: "TARGET_NOT_FOUND",
        errno.EEXIST: "ALREADY_EXISTS",
        errno.EACCES: "PERMISSION_DENIED",
        errno.EPERM: "PERMISSION_DENIED",
    }.get(exc.errno, "PROVIDER_ERROR")
    return Error(code, str(exc))


def kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=2)


def run(argv, control, *, data=b"", cwd=None, env=None, cap=1048576, track=None):
    control.check()
    begin = time.monotonic()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd or "/",
        env={**os.environ, **(env or {})},
        start_new_session=True,
    )
    if track:
        track("process", proc.pid, True)
    control.started = True
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    pending = memoryview(data)
    for stream, name in [(proc.stdout, "stdout"), (proc.stderr, "stderr")]:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    if data:
        os.set_blocking(proc.stdin.fileno(), False)
        selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        proc.stdin.close()
    try:
        while selector.get_map() or proc.poll() is None:
            control.check()
            if proc.poll() is not None:
                # A completed bounded run cannot leave background descendants holding pipes.
                kill_group(proc)
            for key, _ in selector.select(min(0.02, control.remaining())):
                stream, name = key.fileobj, key.data
                if name == "stdin":
                    try:
                        n = os.write(stream.fileno(), pending[:65536])
                        pending = pending[n:]
                    except BrokenPipeError:
                        pending = b""
                    if not pending:
                        selector.unregister(stream)
                        stream.close()
                else:
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    available = cap - len(buffers[name])
                    buffers[name].extend(chunk[:available])
                    truncated[name] |= len(chunk) > available
        code = proc.wait()
        return dict(
            state="exited",
            exit_code=code if code >= 0 else None,
            stdout=b64(buffers["stdout"]),
            stderr=b64(buffers["stderr"]),
            stdout_truncated=truncated["stdout"],
            stderr_truncated=truncated["stderr"],
            duration_ms=round((time.monotonic() - begin) * 1000),
            cleanup="complete",
        )
    finally:
        kill_group(proc)
        if track:
            track("process", proc.pid, False)
        selector.close()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


def file_info(path):
    s = os.lstat(path)
    return dict(
        path=str(path),
        size=s.st_size,
        type="symlink"
        if stat.S_ISLNK(s.st_mode)
        else "directory"
        if stat.S_ISDIR(s.st_mode)
        else "file",
        modified_ms=int(s.st_mtime * 1000),
    )


def file_operation(q, control):
    path = Path(q["path"])
    op = q["operation"]
    control.check()
    if op == "stat":
        return file_info(path)
    if op == "list":
        entries = []
        with os.scandir(path) as rows:
            for entry in rows:
                control.check()
                if len(entries) == 1000:
                    return dict(entries=sorted(entries, key=lambda r: r["path"]), complete=False)
                entries.append(file_info(entry.path))
        return dict(entries=sorted(entries, key=lambda r: r["path"]), complete=True)
    if op == "read":
        # Reject devices/FIFOs so a file read cannot hang the worker on a local pipe.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise Error("UNSUPPORTED_CAPABILITY", "Read requires a regular file")
            data = f.read(q.get("max_bytes", 1048576) + 1)
        if len(data) > q.get("max_bytes", 1048576):
            raise Error("FILE_TOO_LARGE")
        return dict(data=b64(data), bytes=len(data), eof=True, truncated=False)
    if op == "write":
        data = base64.b64decode(q["data"], validate=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
            tmp = Path(f.name)
            try:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
                control.check()
                control.started = True
                if q.get("overwrite", False):
                    os.replace(tmp, path)
                else:
                    os.link(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
        return dict(bytes=len(data), atomic=True)
    control.started = True
    if op == "mkdir":
        path.mkdir(parents=q.get("parents", False), exist_ok=q.get("parents", False))
        return dict(created=True)
    if op == "remove":
        if path.is_dir() and not path.is_symlink():
            if q.get("recursive", False):
                shutil.rmtree(path)
            else:
                path.rmdir()
        else:
            path.unlink()
        return dict(removed=True)
    if op == "move":
        if q.get("overwrite", False):
            os.replace(path, q["destination"])
        else:
            libc = ctypes.CDLL(None, use_errno=True)
            rename = getattr(libc, "renameat2", None)
            if rename is None:
                raise Error("UNSUPPORTED_CAPABILITY", "renameat2 required for exclusive move")
            if rename(-100, os.fsencode(path), -100, os.fsencode(q["destination"]), 1):
                number = ctypes.get_errno()
                raise OSError(number, os.strerror(number))
        return dict(moved=True, atomic=True)
    raise Error("UNSUPPORTED_CAPABILITY")


class Clipboard:
    def __init__(self):
        self.owner = None
        self.track = None

    def read(self, control):
        result = run(
            ["xclip", "-selection", "clipboard", "-out", "-target", "UTF8_STRING"],
            control,
            track=self.track,
        )
        if result["exit_code"] != 0:
            raise Error("UNSUPPORTED_CAPABILITY", "Clipboard has no readable UTF-8 text")
        if result["stdout_truncated"]:
            raise Error("FILE_TOO_LARGE")
        return dict(
            formats={"text/plain": base64.b64decode(result["stdout"]).decode("utf-8")},
            available_formats=["text/plain"],
            omitted_formats=[],
        )

    def write(self, text, control):
        data = text.encode("utf-8")
        if len(data) > 1048576:
            raise Error("FILE_TOO_LARGE")
        control.check()
        # -quiet keeps the selection owner in the foreground, owned by this worker.
        owner = subprocess.Popen(
            ["xclip", "-selection", "clipboard", "-in", "-target", "UTF8_STRING", "-quiet"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if self.track:
            self.track("process", owner.pid, True)
        old = self.owner
        self.owner = owner
        control.started = True
        try:
            os.set_blocking(owner.stdin.fileno(), False)
            pending = memoryview(data)
            while pending:
                control.check()
                try:
                    pending = pending[os.write(owner.stdin.fileno(), pending[:65536]) :]
                except BlockingIOError:
                    control.wait(0.005)
            owner.stdin.close()
            # Round-trip selection conversion establishes the new owner has processed stdin.
            while True:
                control.check()
                try:
                    if self.read(control)["formats"]["text/plain"] == text:
                        break
                except Error as exc:
                    if exc.code != "UNSUPPORTED_CAPABILITY":
                        raise
                if owner.poll() is not None:
                    raise Error(
                        "ACTION_OUTCOME_UNKNOWN", "Clipboard owner exited before publication"
                    )
                control.wait(0.005)
        finally:
            if old:
                kill_group(old)
                if self.track:
                    self.track("process", old.pid, False)
        return completed("x11.clipboard")

    def close(self):
        if self.owner:
            kill_group(self.owner)
            if self.track:
                self.track("process", self.owner.pid, False)
            self.owner = None
