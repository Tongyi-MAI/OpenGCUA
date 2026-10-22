"""Shared public errors and shell result helpers for both API versions."""

from .types import ShellResult


class MuError(Exception):
    def __init__(self, code, message="", *, receipt=None, stop_receipt=None, shell_result=None, details=None):
        super().__init__(
            message
            if message == code or message.startswith(code + ":")
            else f"{code}: {message}"
            if message
            else code
        )
        self.code = code
        # 保留 provider 的结构化诊断；不拼入异常文本，也不据此自动恢复或授权。
        self.details = details
        self.receipt = receipt
        self.stop_receipt = stop_receipt
        self.shell_result = shell_result


class ShellError(MuError):
    def __init__(self, code, message="", *, command_id="", shell_result=None):
        super().__init__(code, message, shell_result=shell_result)
        self.command_id = command_id


def _stdout(self, *, encoding="utf-8", errors="strict"):
    return self.stdout.decode(encoding, errors)


def _stderr(self, *, encoding="utf-8", errors="strict"):
    return self.stderr.decode(encoding, errors)


def _check(self):
    if self.state == "exited" and self.exit_code == 0 and self.cleanup == "complete":
        return
    code = (
        "SHELL_TIMEOUT"
        if self.state == "timed_out"
        else "SHELL_EXIT_NONZERO"
        if self.state == "exited" and self.exit_code
        else "SHELL_OUTCOME_UNKNOWN"
    )
    raise ShellError(code, command_id=self.command_id, shell_result=self)


ShellResult.stdout_text = _stdout
ShellResult.stderr_text = _stderr
ShellResult.check_returncode = _check
