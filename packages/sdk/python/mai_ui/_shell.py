"""Shell value marshalling shared by the public facades."""

import base64
from collections.abc import Sequence
from uuid import uuid4

from ._errors import MuError, ShellError
from .types import ShellIdentity, ShellProvider, ShellResult, ShellScript


def uid():
    return uuid4().hex


def unsupported(message):
    raise MuError("UNSUPPORTED_CAPABILITY", message)


class ShellClient:
    def __init__(self, session):
        self._session = session
        self._identity = None

    async def providers(self):
        description = self._session._description
        result = []
        for raw in description.get("shell_providers", []):
            row = dict(raw)
            row["identity"] = ShellIdentity(**row["identity"])
            row["interpreters"] = tuple(row["interpreters"])
            result.append(ShellProvider(**row))
        return tuple(result)

    async def run(
        self,
        command,
        *,
        provider=None,
        cwd=None,
        env=None,
        stdin=None,
        timeout_ms=30000,
        max_output_bytes=1048576,
        check=False,
        command_id=None,
    ):
        if self._session._stopped:
            raise MuError("SESSION_CLOSED")
        providers = await self.providers()
        eligible = [
            p for p in providers if p.state == "ready" and (provider is None or p.id == provider)
        ]
        if len(eligible) != 1:
            raise ShellError("SHELL_PROVIDER_UNAVAILABLE")
        if not 1 <= timeout_ms <= 120000 or not 0 <= max_output_bytes <= 1048576:
            raise MuError("INVALID_ARGUMENT", "timeout/output cap out of range")
        if stdin is not None and (not isinstance(stdin, bytes) or len(stdin) > 65536):
            raise MuError("INVALID_ARGUMENT", "stdin limit is 64 KiB")
        p = eligible[0]
        cid = command_id or uid()
        args = dict(
            op="shell",
            provider_id=p.id,
            command_id=cid,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
        )
        if isinstance(command, ShellScript):
            if command.interpreter not in (None, p.default_interpreter):
                unsupported("shell interpreter")
            args.update(
                script=command.source,
                interpreter=command.interpreter or p.default_interpreter,
            )
        elif isinstance(command, str):
            args.update(script=command, interpreter=p.default_interpreter)
        elif (
            isinstance(command, Sequence)
            and command
            and all(isinstance(x, str) and "\x00" not in x for x in command)
        ):
            args["argv"] = list(command)
        else:
            raise MuError("INVALID_ARGUMENT", "expected shell script or nonempty argv")
        if any("\x00" in x for x in [args.get("script", ""), cwd or ""]):
            raise MuError("INVALID_ARGUMENT", "NUL is not allowed")
        if cwd is not None:
            args["cwd"] = cwd
        if env is not None:
            args["env"] = dict(env)
        if stdin is not None:
            args["stdin"] = base64.b64encode(stdin).decode()
        try:
            data = await self._session._request(args)
        except MuError as e:
            raise ShellError(e.code, str(e), command_id=cid) from e
        r = ShellResult(
            command_id=cid,
            session_epoch=self._session.epoch,
            provider_id=p.id,
            identity=p.identity,
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
            r.check_returncode()
        return r
