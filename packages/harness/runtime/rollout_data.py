from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import warnings
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# 本项目的精确请求记录格式，不代表任何外部 rollout 标准。
SCHEMA = "mac_agent_harness.request_record"
SFT_SCHEMA = "mac_agent_harness.code_sft"
VERSION = 1
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_LOG = logging.getLogger(__name__)
_ID = re.compile(r"[0-9a-f]{32}\Z")
_CREDENTIAL = re.compile(r"authorization|api[_-]?key|access[_-]?token|password|secret|headers", re.I)
_CREDENTIAL_TEXT = re.compile(
    r"\b(?:authorization|proxy-authorization)\s*['\"]?\s*[:=]\s*\S+|"
    r"\b(?:api[_-]?key|access[_-]?token)\s*['\"]?\s*[:=]\s*['\"]?[\w./+~=-]{8,}|"
    # Bearer/Basic 后的令牌必须含数字或 +/= 等凭据字符；普通英文短语（如 basic calculator）不拒存。
    r"\b(?:Bearer|Basic)\s+(?=[\w./+~=-]*[0-9+/=])[\w./+~=-]{8,}", re.I,
)
_ENCODER = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class RecordingError(ValueError):
    pass


class ExportError(ValueError):
    pass


def _check_tree(value: Any, secrets: tuple[str, ...], *, metadata: bool = False,
                limit: int = DEFAULT_MAX_BYTES) -> None:
    """拒绝凭据而非改写精确消息；只接受有限深度的 JSON 值。"""
    visited = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal visited
        visited += 1
        if depth > 80 or visited > 1_000_000:
            raise RecordingError("JSON 深度或节点数超过记录限额")
        if isinstance(item, str):
            if len(item) > limit:
                raise RecordingError("字符串超过记录限额")
            if any(secret in item for secret in secrets) or _CREDENTIAL_TEXT.search(item):
                raise RecordingError("记录包含鉴权凭据或鉴权赋值，拒绝写入")
        elif isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise RecordingError("JSON 对象键必须是字符串")
                if _CREDENTIAL.search(key):
                    raise RecordingError("记录不允许包含凭据或 header 字段")
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, depth + 1)
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise RecordingError("记录包含非 JSON 值")

    visit(value, 0)


def _chunks(value: Any, limit: int, secrets: tuple[str, ...] = ()):
    _check_tree(value, secrets, limit=limit)
    size = 0
    for text in _ENCODER.iterencode(value):
        chunk = text.encode("utf-8")
        size += len(chunk)
        if size > limit:
            raise RecordingError("记录超过字节限额")
        yield chunk


def _publish(path: Path, write) -> Any:
    """同目录临时文件写完后硬链接发布：不覆盖，失败不留下可读半文件。"""
    if path.exists():
        raise FileExistsError(f"拒绝覆盖已有文件：{path}")
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            result = write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        return result
    finally:
        os.unlink(temporary)


def _write_json(path: Path, value: Any, limit: int, secrets: tuple[str, ...] = ()) -> str:
    def write(handle):
        digest = hashlib.sha256()
        for chunk in _chunks(value, limit, secrets):
            handle.write(chunk)
            digest.update(chunk)
        return digest.hexdigest()
    return _publish(path, write)


def _envelope(kind: str, request_id: str, **fields: Any) -> dict:
    return dict(schema=SCHEMA, schema_version=VERSION, kind=kind, request_id=request_id, **fields)


def warn_recording(message: str, *args: Any) -> None:
    try:
        _LOG.warning(message, *args)
    except (Exception, asyncio.CancelledError):
        # 自定义日志 handler 自身故障时，尽力告警，仍不能中断模型调用。
        try:
            os.write(2, ((message % args) + "\n").encode("utf-8"))
        except (Exception, asyncio.CancelledError):
            pass


def recording_call(target: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    """日志旁路不得改变模型请求的返回值、重试或取消语义。"""
    if target is None or getattr(target, "broken", False):
        return None
    try:
        return getattr(target, method)(*args, **kwargs)
    except (Exception, asyncio.CancelledError) as exc:
        target.broken = True
        # 异常正文可能带 URL、header 或密钥，只记录异常类型。
        warn_recording("请求记录失败，已隔离；该记录不得导出（%s/%s）", method, type(exc).__name__)
        return None


def _code_content_supported(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    # 未知块、provider envelope 和内部调用仅保留在原始记录，不充当公共代码标签。
    return isinstance(content, list) and bool(content) and all(
        isinstance(block, dict) and set(block) == {"type", "text"}
        and block["type"] == "text" and isinstance(block["text"], str)
        for block in content
    ) and any(block["text"].strip() for block in content)


class ResponseCapture:
    """只捕获 assistant 协议字段，不复制 provider trace；限制新增内存。"""

    def __init__(self, limit: int, secrets: tuple[str, ...]):
        self.limit, self.secrets = limit, secrets
        self.size = 0
        self.frames: list[dict] = []
        self.body_key: str | None = None
        self.finish_reason: Any = None
        self.broken = False

    def capture(self, choice: dict, body_key: str) -> None:
        if choice.get("finish_reason") is not None:
            self.finish_reason = choice["finish_reason"]
        body = choice.get(body_key)
        if not isinstance(body, dict):
            raise RecordingError("响应正文缺失")
        fields = {key: body[key] for key in (
            "role", "content", "reasoning_content", "reasoning", "tool_calls", "function_call", "refusal",
        ) if key in body}
        # 留出聚合值与 JSON 容器的空间；不截断并冒充完整响应。
        self.size += sum(len(chunk) for chunk in _chunks(fields, self.limit, self.secrets))
        if self.size > self.limit // 4 or len(self.frames) >= 100_000:
            raise RecordingError("响应捕获超过记录限额")
        self.frames.append(copy.deepcopy(fields))
        self.body_key = body_key

    def result(self) -> dict:
        if self.broken or not self.frames:
            raise RecordingError("没有完整的原始响应捕获")
        if self.body_key == "message":
            body = self.frames[0]
        else:
            body = {"role": "assistant"}
            for field in ("content", "reasoning_content", "reasoning"):
                if not any(field in frame for frame in self.frames):
                    continue
                parts = [frame[field] for frame in self.frames if frame.get(field) is not None]
                if all(isinstance(part, str) for part in parts):
                    body[field] = "".join(parts)
                elif all(isinstance(part, list) for part in parts):
                    body[field] = [block for part in parts for block in part]
                else:
                    raise RecordingError("混合类型 delta 无法无损聚合")
        assistant = {key: value for key, value in body.items()
                     if key not in ("reasoning_content", "reasoning")}
        assistant.setdefault("role", "assistant")
        return {
            "assistant": assistant,
            "reasoning_fields": {key: body[key] for key in ("reasoning_content", "reasoning") if key in body},
            "raw_deltas": self.frames if self.body_key == "delta" else None,
            "finish_reason": self.finish_reason,
            "supported_code_output": _code_content_supported(assistant.get("content"))
            and all(frame.get("role", "assistant") == "assistant" for frame in self.frames)
            and not any(
                frame.get(key) for frame in self.frames for key in ("tool_calls", "function_call", "refusal")
            ),
        }


class RolloutRecorder:
    def __init__(self, run_dir: str | Path, *, metadata: dict | None = None,
                 max_record_bytes: int = DEFAULT_MAX_BYTES, secrets: tuple[str, ...] = ()):
        if not isinstance(max_record_bytes, int) or max_record_bytes < 1024:
            raise RecordingError("max_record_bytes 至少为 1024")
        self.root = Path(run_dir) / "model_requests"
        self.max_bytes = max_record_bytes
        self.secrets = tuple(secret for secret in secrets if secret)
        self.metadata = copy.deepcopy(metadata if metadata is not None else {})
        if not isinstance(self.metadata, dict):
            raise RecordingError("metadata 必须为对象")
        _check_tree(self.metadata, self.secrets, metadata=True, limit=self.max_bytes)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.broken = False

    def begin(self, request_id: str, purpose: str, metadata: dict | None = None) -> RequestRecord:
        if not _ID.fullmatch(request_id):
            raise RecordingError("非法 request_id")
        if metadata is not None and not isinstance(metadata, dict):
            raise RecordingError("metadata 必须为对象")
        combined = {**self.metadata, **(metadata or {})}
        _check_tree(combined, self.secrets, metadata=True, limit=self.max_bytes)
        directory = self.root / request_id
        directory.mkdir(mode=0o700, exist_ok=False)
        return RequestRecord(self, directory, request_id, purpose, combined)


class RequestRecord:
    def __init__(self, owner: RolloutRecorder, directory: Path, request_id: str, purpose: str, metadata: dict):
        self.owner, self.directory = owner, directory
        self.request_id, self.purpose, self.metadata = request_id, purpose, metadata
        self.request_hash: str | None = None
        self.result_hash: str | None = None
        self.final_attempt_hash: str | None = None
        self.attempt_count = 0
        self.request_dispatched = False
        self.final_attempt: int | None = None
        self.broken = False
        self.status = "pending"
        self.supported = False
        self.images_self_contained = False
        self.validation: bool | None = None

    def write(self, name: str, kind: str, **fields: Any) -> str:
        return _write_json(self.directory / name, _envelope(kind, self.request_id, **fields),
                           self.owner.max_bytes, self.owner.secrets)

    def save_request(self, payload: dict) -> None:
        self.request_hash = self.write(
            "request.json", "request", purpose=self.purpose,
            created_at=datetime.now(timezone.utc).isoformat(), metadata=self.metadata,
            exact=True, capture_stage="prepared_payload", request=payload,
        )
        self.images_self_contained = _images_exact(payload["messages"])
        # 图片仅存在独立 request 文件中，不在结果、事件或内存索引再次复制。
        self.metadata = {}

    def new_capture(self) -> ResponseCapture:
        return ResponseCapture(self.owner.max_bytes, self.owner.secrets)

    def save_attempt(self, stats: dict, capture: ResponseCapture | None, *, dispatched: bool = False) -> None:
        if capture is not None and capture.broken:
            raise RecordingError("响应捕获不完整")
        raw = capture.result() if capture is not None and capture.frames else None
        dispatched = dispatched is True
        if stats["status"] == "success" and (raw is None or not dispatched):
            raise RecordingError("成功返回没有原始响应或真实传输入口记录")
        number = stats["attempt"]
        digest = self.write(
            f"attempt_{number:04d}.json", "attempt", attempt=number,
            attempt_id=stats["attempt_id"], status=stats["status"], raw=raw, dispatched=dispatched,
            usage=stats["usage"], finish_reason=raw["finish_reason"] if raw else stats["finish_reason"],
            http_status=stats["http_status"],
            # 不保存可能回显凭据的网络异常正文。
            error={"type": stats["error"].split(":", 1)[0]} if stats["error"] else None,
        )
        self.attempt_count += 1
        self.request_dispatched = self.request_dispatched or dispatched
        self.final_attempt, self.final_attempt_hash = number, digest
        self.supported = bool(raw and raw["supported_code_output"])

    def finish(self, status: str, error_type: str | None = None) -> None:
        if self.request_hash is None:
            raise RecordingError("缺少精确请求")
        self.status = status
        self.result_hash = self.write(
            "result.json", "result", request_sha256=self.request_hash,
            recording_complete=True, request_dispatched=self.request_dispatched,
            status=status, error={"type": error_type} if error_type else None,
            attempt_count=self.attempt_count, final_attempt=self.final_attempt,
            final_attempt_sha256=self.final_attempt_hash, trainable=False,
            trainability_reason="pending_code_validation" if status == "success" and self.purpose == "action"
            else "non_action" if self.purpose != "action" else "request_failed",
        )

    def validate(self, accepted: bool) -> None:
        if type(accepted) is not bool:
            raise RecordingError("代码校验必须显式使用布尔值")
        if self.validation is not None:
            if self.validation != accepted:
                # 禁止互相矛盾的标记继续作为可训练样本；不覆盖先前的事实。
                self.write("invalid.json", "invalid", reason="conflicting_validation")
                raise RecordingError("重复校验结果冲突")
            return
        if self.result_hash is None:
            raise RecordingError("缺少完整逻辑请求结果")
        self.write(
            "validation.json", "validation", result_sha256=self.result_hash,
            accepted=accepted, scope="code_protocol_only",
            trainable=accepted and self.status == "success" and self.purpose == "action"
            and self.supported and self.images_self_contained,
            trainability_reason="non_action" if self.purpose != "action" else
            "request_failed" if self.status != "success" else "code_rejected" if not accepted else
            "unsupported_output" if not self.supported else "images_not_self_contained"
            if not self.images_self_contained else "code_protocol_validated",
        )
        self.validation = accepted


def _read_json(path: Path, limit: int, kind: str, request_id: str) -> tuple[dict, str]:
    if path.is_symlink():
        raise RecordingError("不读取符号链接记录")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise RecordingError("记录超过读取限额")
    value = json.loads(data)
    _check_tree(value, (), limit=limit)
    if not isinstance(value, dict) or any(value.get(key) != expected for key, expected in (
        ("schema", SCHEMA), ("schema_version", VERSION), ("kind", kind), ("request_id", request_id),
    )):
        raise RecordingError("记录 schema/version/kind/request_id 不匹配")
    return value, hashlib.sha256(data).hexdigest()


def _images_exact(messages: list) -> bool:
    """只接受自包含图片，不从原图路径或网络补图。"""
    for message in messages:
        if (not isinstance(message, dict)
                or message.get("role") not in ("system", "developer", "user", "assistant", "tool", "function")):
            return False
        blocks = message.get("content")
        if isinstance(blocks, str):
            continue
        if not isinstance(blocks, list):
            return False
        for block in blocks:
            if not isinstance(block, dict):
                return False
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                continue
            # 未知模态可能隐藏外部图片引用：原始请求仍保留，但不猜测可训练性。
            if block.get("type") != "image_url":
                return False
            image = block.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if not isinstance(url, str) or not url.startswith("data:image/"):
                return False
            head, separator, encoded = url.partition(",")
            if not separator or not head.endswith(";base64") or not encoded:
                return False
            # 分块解码到临时文件；校验真实图片及 MIME，不展开整张像素图。
            if len(encoded) % 4 or "=" in encoded[:-2]:
                return False
            from PIL import Image
            try:
                with tempfile.TemporaryFile() as image_file:
                    for offset in range(0, len(encoded), 65536):
                        image_file.write(base64.b64decode(encoded[offset:offset + 65536], validate=True))
                    image_file.seek(0)
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", Image.DecompressionBombWarning)
                        with Image.open(image_file) as decoded:
                            if Image.MIME.get(decoded.format) != head[5:-7]:
                                return False
                            decoded.verify()
            except (ValueError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                return False
    return True


def _sample(directory: Path, limit: int) -> tuple[dict | None, str | None]:
    request_id = directory.name
    request, request_hash = _read_json(directory / "request.json", limit, "request", request_id)
    if request.get("exact") is not True:
        return None, "inexact_request"
    if request.get("purpose") not in ("action", "compaction"):
        return None, "invalid_record"
    if request["purpose"] != "action":
        return None, "non_action"
    if (directory / "invalid.json").exists():
        return None, "invalidated"
    result, result_hash = _read_json(directory / "result.json", limit, "result", request_id)
    if result.get("recording_complete") is not True or result.get("request_sha256") != request_hash:
        return None, "incomplete_record"
    if result.get("status") != "success":
        return None, "request_failed"
    if result.get("request_dispatched") is not True:
        return None, "missing_dispatch"
    number = result.get("final_attempt")
    if type(number) is not int or not 1 <= number <= 100_000 or result.get("attempt_count") != number:
        return None, "incomplete_record"
    # 检查所有 HTTP 尝试均已落盘；每次只读取一条，不累计整条轨迹。
    attempt = None
    for index in range(1, number + 1):
        attempt, digest = _read_json(directory / f"attempt_{index:04d}.json", limit, "attempt", request_id)
        if attempt.get("attempt") != index:
            return None, "incomplete_record"
    if (digest != result.get("final_attempt_sha256") or attempt.get("status") != "success"
            or attempt.get("dispatched") is not True):
        return None, "incomplete_record"
    validation, _ = _read_json(directory / "validation.json", limit, "validation", request_id)
    if (validation.get("result_sha256") != result_hash
            or validation.get("scope") != "code_protocol_only"):
        return None, "incomplete_record"
    if validation.get("accepted") is not True or validation.get("trainable") is not True:
        reason = validation.get("trainability_reason")
        return None, reason if reason in ("images_not_self_contained", "unsupported_output") else "not_trainable"
    raw = attempt.get("raw")
    if not isinstance(raw, dict) or raw.get("supported_code_output") is not True:
        return None, "unsupported_output"
    assistant = raw.get("assistant")
    if (not isinstance(assistant, dict) or assistant.get("role") != "assistant"
            or set(assistant) - {"role", "content", "tool_calls", "function_call", "refusal"}
            or not _code_content_supported(assistant.get("content"))
            or any(assistant.get(key) for key in ("tool_calls", "function_call", "refusal"))):
        return None, "unsupported_output"
    if attempt.get("finish_reason") not in (None, "stop"):
        return None, "bad_finish"
    payload = request.get("request")
    if (not isinstance(payload, dict) or not isinstance(payload.get("model"), str)
            or type(payload.get("max_tokens")) is not int or not isinstance(request.get("metadata"), dict)):
        return None, "invalid_record"
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages or not _images_exact(messages):
        return None, "images_not_self_contained"
    # 保留原始协议、消息块和顺序；不加入独立 reasoning，不拆动作 transition。
    return {
        "messages": [*messages, assistant],
        "metadata": {
            "source": request.get("metadata", {}), "request_id": request_id,
            "purpose": request["purpose"], "model": payload.get("model"),
            "max_tokens": payload.get("max_tokens"), "created_at": request.get("created_at"),
            "schema": SFT_SCHEMA, "schema_version": VERSION, "request_schema": SCHEMA,
            "image_transport": "inline_data_url", "transport_status": result["status"],
            "request_sha256": request_hash, "usage": attempt.get("usage", {}),
            "finish_reason": attempt.get("finish_reason"), "validation_scope": "code_protocol_only",
        },
    }, None


def export_rollout(run_dirs: list[str | Path], output: str | Path, *,
                   max_record_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """只导出精确记录。去重输入目录，逐请求读取；目标已存在时显式拒绝。"""
    if type(max_record_bytes) is not int or max_record_bytes < 1024:
        raise ExportError("max_record_bytes 必须是至少为 1024 的整数")
    roots = []
    for item in run_dirs:
        path = Path(item).resolve()
        root = path if path.name == "model_requests" else path / "model_requests"
        if not root.is_dir():
            raise ExportError(f"缺少精确 request records，拒绝从旧 traj 重建：{path}")
        if root not in roots:
            roots.append(root)
    if not roots:
        raise ExportError("至少提供一个记录目录")
    output = Path(output)
    stats: dict[str, Any] = {"scanned": 0, "exported": 0, "skipped": 0, "skip_reasons": {}}
    reasons: Counter = Counter()
    # UUID 去重状态放临时 SQLite，不让样本数决定 Python 内存占用。
    import sqlite3
    with tempfile.TemporaryDirectory(prefix=".rollout-export-", dir=output.parent) as temporary:
        with closing(sqlite3.connect(str(Path(temporary) / "seen.sqlite"))) as seen:
            seen.execute("CREATE TABLE seen (request_id TEXT PRIMARY KEY)")

            def write(handle):
                for root in roots:
                    with os.scandir(root) as entries:
                        for entry in entries:
                            if not entry.is_dir(follow_symlinks=False) or not _ID.fullmatch(entry.name):
                                continue
                            stats["scanned"] += 1
                            try:
                                sample, reason = _sample(Path(entry.path), max_record_bytes)
                            except FileNotFoundError as exc:
                                sample = None
                                reason = "missing_validation" if exc.filename and str(exc.filename).endswith("validation.json") else "incomplete_record"
                            except (ValueError, TypeError, KeyError, OSError, RecursionError):
                                sample, reason = None, "invalid_record"
                            if sample is not None:
                                if seen.execute("SELECT 1 FROM seen WHERE request_id=?", (entry.name,)).fetchone():
                                    sample, reason = None, "duplicate_request"
                            if sample is None:
                                reasons[reason or "invalid_record"] += 1
                                stats["skipped"] += 1
                                continue
                            offset = handle.tell()
                            try:
                                for chunk in _chunks(sample, max_record_bytes * 2):
                                    handle.write(chunk)
                                handle.write(b"\n")
                            except (ValueError, TypeError, RecursionError):
                                handle.seek(offset)
                                handle.truncate()
                                reasons["serialization_error"] += 1
                                stats["skipped"] += 1
                                continue
                            seen.execute("INSERT INTO seen VALUES (?)", (entry.name,))
                            stats["exported"] += 1
                if not stats["scanned"]:
                    raise ExportError("没有精确 request records，拒绝从旧 traj 重建")
                stats["skip_reasons"] = dict(reasons)
            _publish(output, write)
    return stats
