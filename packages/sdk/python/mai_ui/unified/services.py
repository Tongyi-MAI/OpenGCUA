"""Device services use the same routed operation core as UI actions."""

from __future__ import annotations

import base64
from dataclasses import dataclass

from .._errors import MuError
from .._shell import ShellClient


class Shell:
    def __init__(self, handle):
        self._handle = handle
        self._shell = ShellClient(handle._backend)

    async def run(
        self,
        script,
        *,
        cwd=None,
        env=None,
        stdin=None,
        timeout_ms=30000,
        check=False,
        action_id=None,
        max_output_bytes=1048576,
    ):
        self._handle._whole_device()
        self._handle._require("shell.run")
        import dataclasses

        from ..types import ShellIdentity, ShellResult, ShellScript
        from .runtime import state

        if stdin is not None and not isinstance(stdin, bytes):
            raise MuError("INVALID_ARGUMENT", "stdin must be bytes")
        prepared = state(
            self._handle._session._runtime._core,
            op="prepare_shell",
            description=self._handle._endpoint.description,
            command=dataclasses.asdict(script) if isinstance(script, ShellScript) else script,
            cwd=cwd,
            env=env,
            stdin=base64.b64encode(stdin).decode() if stdin is not None else None,
            timeout_ms=timeout_ms,
            action_id=action_id,
            max_output_bytes=max_output_bytes,
        )
        data = await self._handle._backend._request(prepared["wire"])
        result = ShellResult(
            command_id=prepared["command_id"],
            session_epoch=self._handle._session.id,
            provider_id=prepared["provider"]["id"],
            identity=ShellIdentity(**prepared["provider"]["identity"]),
            state=data["state"],
            exit_code=data["exit_code"],
            signal=None,
            stdout=base64.b64decode(data["stdout"]),
            stderr=base64.b64decode(data["stderr"]),
            stdout_truncated=data["stdout_truncated"],
            stderr_truncated=data["stderr_truncated"],
            stderr_merged=False,
            duration_ms=data["duration_ms"],
            cleanup=data["cleanup"],
        )
        if check:
            result.check_returncode()
        return result


class Processes:
    def __init__(self, handle):
        self._handle = handle

    async def run(self, argv, **options):
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or not all(isinstance(a, str) and "\x00" not in a for a in argv)
        ):
            raise MuError("INVALID_ARGUMENT", "argv must be a nonempty sequence of strings")
        return await self._handle.shell.run(argv, **options)

    async def spawn(self, *args, **kwargs):
        raise MuError("UNSUPPORTED_CAPABILITY", "managed process streaming and ownership are WIP")


@dataclass(frozen=True)
class FileRef:
    path: str
    _files: object


class Files:
    def __init__(self, handle):
        self._handle = handle

    def path(self, path):
        self._handle._whole_device()
        self._path(path)
        return FileRef(path, self)

    def _path(self, path):
        if isinstance(path, FileRef):
            if path._files is not self:
                raise MuError("INVALID_ARGUMENT", "foreign file reference")
            return path.path
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
            raise MuError("INVALID_ARGUMENT", "use an absolute path on the target device")
        return path

    async def _call(self, operation, path, *, timeout_ms=5000, action_id=None, **args):
        self._handle._whole_device()
        self._handle._require("files." + operation)
        wire = dict(
            op="file", operation=operation, path=self._path(path), timeout_ms=timeout_ms, **args
        )
        if action_id is not None:
            wire["command_id"] = action_id
        return await self._handle._backend._request(wire)

    async def read(self, path, *, max_bytes=1048576, timeout_ms=5000):
        if type(max_bytes) is not int or not 0 <= max_bytes <= 1048576:
            raise MuError("INVALID_ARGUMENT", "max_bytes must be in 0..1048576")
        result = await self._call("read", path, max_bytes=max_bytes, timeout_ms=timeout_ms)
        return base64.b64decode(result["data"])

    async def write(self, path, data, *, overwrite=False, timeout_ms=5000, action_id=None):
        if not isinstance(data, bytes) or len(data) > 1048576 or type(overwrite) is not bool:
            raise MuError(
                "INVALID_ARGUMENT", "write accepts up to 1 MiB of bytes and a boolean overwrite"
            )
        return await self._call(
            "write",
            path,
            data=base64.b64encode(data).decode(),
            overwrite=overwrite,
            timeout_ms=timeout_ms,
            action_id=action_id,
        )

    async def stat(self, path, *, timeout_ms=5000):
        return await self._call("stat", path, timeout_ms=timeout_ms)

    async def list(self, path, *, timeout_ms=5000):
        return await self._call("list", path, timeout_ms=timeout_ms)

    async def mkdir(self, path, *, parents=False, timeout_ms=5000, action_id=None):
        if type(parents) is not bool:
            raise MuError("INVALID_ARGUMENT")
        return await self._call(
            "mkdir", path, parents=parents, timeout_ms=timeout_ms, action_id=action_id
        )

    async def remove(self, path, *, recursive=False, timeout_ms=5000, action_id=None):
        if type(recursive) is not bool:
            raise MuError("INVALID_ARGUMENT")
        return await self._call(
            "remove", path, recursive=recursive, timeout_ms=timeout_ms, action_id=action_id
        )

    async def move(self, source, destination, *, overwrite=False, timeout_ms=5000, action_id=None):
        if type(overwrite) is not bool:
            raise MuError("INVALID_ARGUMENT")
        return await self._call(
            "move",
            source,
            destination=self._path(destination),
            overwrite=overwrite,
            timeout_ms=timeout_ms,
            action_id=action_id,
        )


class Transfers:
    def __init__(self, session):
        self._session = session

    async def copy(self, source, destination, *, overwrite=False, timeout_ms=10000, action_id=None):
        from .runtime import call

        if not all(
            isinstance(r, FileRef) and r._files._handle._session is self._session
            for r in (source, destination)
        ):
            raise MuError("INVALID_ARGUMENT", "copy requires two file references from this session")

        def ref(value):
            h = value._files._handle
            h._whole_device()
            return dict(attachment=h.id, resource=h.resource_id, path=value.path)

        return await call(
            self._session._runtime._core,
            dict(
                op="copy_files",
                session=self._session.id,
                source=ref(source),
                destination=ref(destination),
                overwrite=overwrite,
                timeout_ms=timeout_ms,
                action_id=action_id,
            ),
        )


@dataclass(frozen=True)
class ClipboardContent:
    formats: dict[str, str | bytes]
    available_formats: tuple[str, ...] = ()
    omitted_formats: tuple[str, ...] = ()


class Clipboard:
    def __init__(self, handle):
        self._handle = handle

    async def read(self, formats=None, *, timeout_ms=5000):
        h = self._handle
        h._whole_device()
        h._require("clipboard.read")
        if formats is not None and list(formats) != ["text/plain"]:
            raise MuError("UNSUPPORTED_CAPABILITY", "clipboard currently supports text/plain")
        data = await h._backend._request(
            dict(op="clipboard", operation="read", timeout_ms=timeout_ms)
        )
        return ClipboardContent(
            data["formats"], tuple(data["available_formats"]), tuple(data["omitted_formats"])
        )

    async def write(self, content, *, timeout_ms=5000, action_id=None):
        h = self._handle
        h._whole_device()
        h._require("clipboard.write")
        values = content.formats if isinstance(content, ClipboardContent) else content
        if (
            not isinstance(values, dict)
            or set(values) != {"text/plain"}
            or not isinstance(values["text/plain"], str)
        ):
            raise MuError("UNSUPPORTED_CAPABILITY", 'provide {"text/plain": text}')
        return await h._backend._action(
            "clipboard_write", text=values["text/plain"], timeout_ms=timeout_ms, action_id=action_id
        )
