"""有界本地长期记忆：模型只能提出候选，用户确认后才参与检索。

这是同一 macOS 用户下的私有数据文件，不是 OS 安全沙箱：同 uid 进程仍可读写。
短期记忆继续由 ContextManager/TODO/compaction/journal/resume 负责，这里不保存
请求原文、截图或聊天全文。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DB_NAME = "memory.sqlite3"
POLICY = "candidate_review"
KINDS = ("preference", "workflow", "fact")
MAX_CANDIDATES = 128
MAX_APPROVED = 256
MAX_TITLE = 120
MAX_CONTENT = 2000
MAX_APP = 120
MAX_REQUEST_BYTES = 8192
CATALOG_LIMIT = 64
RECALL_LIMIT = 5
RECALL_CHARS = 4000
_BUSY_TIMEOUT = 2.0
_UNSET = object()

ERROR_CODES = frozenset({
    "MEMORY_SENSITIVE", "MEMORY_AUTHORITY", "MEMORY_STALE_REFERENCE", "MEMORY_INVALID",
    "MEMORY_TOO_LARGE", "MEMORY_CANDIDATE_LIMIT", "MEMORY_APPROVED_LIMIT",
    "MEMORY_REVISION_CONFLICT", "MEMORY_NOT_FOUND", "MEMORY_INVALID_STATE",
    "MEMORY_DUPLICATE", "MEMORY_UNSAFE_PATH", "MEMORY_SCHEMA_UNSUPPORTED", "MEMORY_UNAVAILABLE",
})

# 只拦明显的凭证/授权承诺/易失定位；拒绝时只返回错误码，不回显正文。
_SECRET = re.compile(
    r"(password|passwd|passcode|pass\s*word|api[\s_-]?key|secret|token|bearer|cookie|"
    r"credential|private[\s_-]?key|session[\s_-]?id|otp\b|2fa|mfa|"
    r"密码|口令|密钥|私钥|令牌|凭证|凭据|验证码|动态码|支付密码|"
    r"-----BEGIN|\bsk-[A-Za-z0-9_-]{12,}|\bAKIA[0-9A-Z]{12,}|\bgh[pousr]_[A-Za-z0-9]{12,}|"
    r"\bxox[abprs]-[A-Za-z0-9-]{8,}|\beyJ[A-Za-z0-9_-]{16,}\.)",
    re.IGNORECASE,
)
_AUTHORITY = re.compile(
    r"(永久(批准|授权|允许|同意)|总是(允许|批准|同意|授权)|一律(允许|批准|同意)|"
    r"自动(批准|授权|同意|付款|支付|购买|下单|发送|发布|删除|转账)|"
    r"无需(确认|授权|询问|审批)|不用(确认|授权|询问|再问)|不必(确认|询问)|免(确认|授权|审批)|"
    r"直接(付款|支付|购买|下单|转账)|授予.{0,12}权限|前台(接管|授权)|"
    r"always\s+(allow|approve|grant|confirm)|auto[\s-]?(approve|confirm|purchase|pay|send)|"
    r"permanent(ly)?\s+(approv|allow|grant|authori)|without\s+(asking|confirmation|approval)|"
    r"grant\s+(access|permission))",
    re.IGNORECASE,
)
_STALE = re.compile(
    r"(window:\d+|\bnode[\s_-]?(id)?\s*[:#=]\s*\S+|\bax[\s_-]?id\b|\bpid\s*[:=]?\s*\d+|"
    r"\bhandle\s*[:=]\s*\S+|句柄|节点\s*(id|ID|编号)|坐标|\(\s*\d{2,5}\s*,\s*\d{2,5}\s*\)|"
    r"\bx\s*=\s*\d+\s*[,，]\s*y\s*=\s*\d+|normalized_1000)",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9][a-z0-9_.+-]*")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_STOP = frozenset({"the", "and", "for", "with", "to", "of", "in", "on", "a", "an", "is", "it"})


class MemoryStoreError(Exception):
    """有界错误：只暴露错误码，不携带记忆正文。"""

    def __init__(self, code: str):
        super().__init__(code if code in ERROR_CODES else "MEMORY_UNAVAILABLE")
        self.code = str(self)


def tokens(text: str) -> set[str]:
    """用于词法检索：ASCII 单词 + CJK 二元组。"""
    lowered = (text or "").casefold()
    found = {word for word in _WORD.findall(lowered) if len(word) >= 2 and word not in _STOP}
    for run in _CJK.findall(lowered):
        found.update(run[index:index + 2] for index in range(len(run) - 1))
    return found


def _clean_line(value: Any, limit: int, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise MemoryStoreError("MEMORY_INVALID")
    cleaned = " ".join(value.split())
    if not cleaned:
        if required:
            raise MemoryStoreError("MEMORY_INVALID")
        return None
    if len(cleaned) > limit:
        raise MemoryStoreError("MEMORY_TOO_LARGE")
    return cleaned


def normalize_fields(title: Any, content: Any, kind: Any, app: Any) -> dict[str, Any]:
    title = _clean_line(title, MAX_TITLE, required=True)
    if not isinstance(content, str):
        raise MemoryStoreError("MEMORY_INVALID")
    content = "\n".join(line.rstrip() for line in content.strip().splitlines())
    if not content:
        raise MemoryStoreError("MEMORY_INVALID")
    if len(content) > MAX_CONTENT:
        raise MemoryStoreError("MEMORY_TOO_LARGE")
    if kind not in KINDS:
        raise MemoryStoreError("MEMORY_INVALID")
    app = _clean_line(app, MAX_APP, required=False)
    screen = "\n".join(part for part in (title, content, app or "") if part)
    if _SECRET.search(screen):
        raise MemoryStoreError("MEMORY_SENSITIVE")
    if _AUTHORITY.search(screen):
        raise MemoryStoreError("MEMORY_AUTHORITY")
    if _STALE.search(screen):
        raise MemoryStoreError("MEMORY_STALE_REFERENCE")
    return {"title": title, "content": content, "kind": kind, "app": app}


def _content_hash(kind: str, content: str) -> str:
    return hashlib.sha256((kind + "\n" + " ".join(content.casefold().split())).encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _bounded_int(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value < 2 ** 31 else None


def _bounded_text(value: Any, limit: int = 100) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


class MemoryStore:
    """SQLite 存储；每次操作独立连接，写操作用 BEGIN IMMEDIATE 做 revision CAS。"""

    def __init__(self, directory: Path):
        self.directory = Path(directory).expanduser().absolute()
        self.path = self.directory / DB_NAME
        self._check_directory(create=False)

    # -- 文件安全 -------------------------------------------------------------
    def _check_directory(self, *, create: bool) -> bool:
        parent = self.directory.parent
        try:
            parent_stat = os.lstat(parent)
        except FileNotFoundError:
            if not create:
                return False
            os.makedirs(parent, mode=0o700, exist_ok=True)
            parent_stat = os.lstat(parent)
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise MemoryStoreError("MEMORY_UNSAFE_PATH")
        try:
            info = os.lstat(self.directory)
        except FileNotFoundError:
            if not create:
                return False
            os.mkdir(self.directory, 0o700)
            info = os.lstat(self.directory)
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()):
            raise MemoryStoreError("MEMORY_UNSAFE_PATH")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(self.directory, 0o700, follow_symlinks=False)
        return True

    def _check_file(self, *, create: bool) -> bool:
        flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | (os.O_CREAT if create else 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise MemoryStoreError("MEMORY_UNSAFE_PATH") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise MemoryStoreError("MEMORY_UNSAFE_PATH")
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        return True

    def _connect(self, *, create: bool) -> sqlite3.Connection | None:
        try:
            if not self._check_directory(create=create) or not self._check_file(create=create):
                return None
            connection = sqlite3.connect(str(self.path), timeout=_BUSY_TIMEOUT, isolation_level=None)
        except MemoryStoreError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise MemoryStoreError("MEMORY_UNAVAILABLE") from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _schema(self, connection: sqlite3.Connection, *, write: bool) -> bool:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return True
        if version != 0:
            raise MemoryStoreError("MEMORY_SCHEMA_UNSUPPORTED")
        if not write:
            return False
        # 不能用 executescript：它会先隐式 COMMIT，破坏外层 BEGIN IMMEDIATE 事务。
        for statement in ("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                status TEXT NOT NULL CHECK (status IN ('candidate', 'approved')),
                kind TEXT NOT NULL CHECK (kind IN ('preference', 'workflow', 'fact')),
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                app TEXT,
                content_hash TEXT NOT NULL,
                source_session TEXT,
                source_turn INTEGER,
                source_step INTEGER,
                source_origin TEXT NOT NULL,
                validation TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS memories_status ON memories(status, updated_at)",
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)",
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('generation', 0)",
            f"PRAGMA user_version={SCHEMA_VERSION}",
        ):
            connection.execute(statement)
        return True

    def _write(self, operation):
        connection = self._connect(create=True)
        if connection is None:
            raise MemoryStoreError("MEMORY_UNAVAILABLE")
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._schema(connection, write=True)
                result = operation(connection)
                if not result.pop("unchanged", False):
                    connection.execute("UPDATE meta SET value = value + 1 WHERE key = 'generation'")
                result["generation"] = self._generation(connection)
                result["catalog"] = self._catalog(connection, CATALOG_LIMIT)
                connection.execute("COMMIT")
                return result
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        except sqlite3.Error as exc:
            raise MemoryStoreError("MEMORY_UNAVAILABLE") from exc
        finally:
            connection.close()

    def _read(self, operation, empty):
        connection = self._connect(create=False)
        if connection is None:
            return empty
        try:
            connection.execute("BEGIN")
            try:
                if not self._schema(connection, write=False):
                    return empty
                return operation(connection)
            finally:
                connection.execute("COMMIT")
        except sqlite3.Error as exc:
            raise MemoryStoreError("MEMORY_UNAVAILABLE") from exc
        finally:
            connection.close()

    # -- 读取 ---------------------------------------------------------------
    @staticmethod
    def _generation(connection) -> int:
        row = connection.execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def entry(row) -> dict[str, Any]:
        return {
            "id": row["id"], "revision": row["revision"], "status": row["status"],
            "kind": row["kind"], "title": row["title"], "content": row["content"], "app": row["app"],
            "source": {"session_id": row["source_session"], "turn": row["source_turn"],
                       "step": row["source_step"], "origin": row["source_origin"]},
            "validation": row["validation"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def _counts(self, connection) -> dict[str, int]:
        counts = {"candidate": 0, "approved": 0}
        for status, count in connection.execute("SELECT status, COUNT(*) FROM memories GROUP BY status"):
            counts[status] = int(count)
        return counts

    def _catalog(self, connection, limit: int, offset: int = 0) -> dict[str, Any]:
        counts = self._counts(connection)
        rows = connection.execute(
            "SELECT * FROM memories ORDER BY status = 'approved', updated_at DESC, id LIMIT ? OFFSET ?",
            (max(0, min(limit, CATALOG_LIMIT)), offset),
        ).fetchall()
        entries = [self.entry(row) for row in rows]
        return {
            "entries": entries, "generation": self._generation(connection),
            "candidate_count": counts["candidate"], "approved_count": counts["approved"],
            "omitted": counts["candidate"] + counts["approved"] - len(entries),
            "offset": offset,
            "next_offset": offset + len(entries) if entries and offset + len(entries)
                           < counts["candidate"] + counts["approved"] else None,
            "policy": POLICY,
        }

    def catalog(self, limit: int = CATALOG_LIMIT, *, offset: int = 0) -> dict[str, Any]:
        if type(offset) is not int or not 0 <= offset <= MAX_CANDIDATES + MAX_APPROVED:
            raise MemoryStoreError("MEMORY_INVALID")
        empty = {"entries": [], "generation": 0, "candidate_count": 0, "approved_count": 0,
                 "omitted": 0, "offset": offset, "next_offset": None, "policy": POLICY}
        return self._read(lambda connection: self._catalog(connection, limit, offset), empty)

    def get(self, memory_id: str) -> dict[str, Any] | None:
        def operation(connection):
            row = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            return self.entry(row) if row else None
        return self._read(operation, None)

    def approved_revisions(self, ids) -> dict[str, int]:
        ids = [value for value in ids if isinstance(value, str)][:256]
        if not ids:
            return {}

        def operation(connection):
            marks = ",".join("?" for _ in ids)
            return {row["id"]: row["revision"] for row in connection.execute(
                f"SELECT id, revision FROM memories WHERE status = 'approved' AND id IN ({marks})", ids)}
        return self._read(operation, {})

    def recall(self, query: str, *, limit: int = RECALL_LIMIT, max_chars: int = RECALL_CHARS,
               shown: dict[str, int] | None = None):
        """只检索已批准记忆；未匹配返回空列表。"""
        wanted = tokens(query)
        def operation(connection):
            scored = []
            current = {}
            for row in connection.execute("SELECT * FROM memories WHERE status = 'approved'"):
                if shown is not None and row["id"] in shown:
                    current[row["id"]] = row["revision"]
                app_tokens = tokens(row["app"] or "")
                if app_tokens and not wanted & app_tokens:
                    continue  # 应用专属经验不因“保存”等通用词进入其它应用的请求。
                strong = tokens(row["title"]) | app_tokens
                score = 2 * len(wanted & strong) + len(wanted & tokens(row["content"]))
                if row["kind"] == "preference" and not row["app"]:
                    score = max(score, 2)  # 用户已确认的全局偏好无需每轮重复命中关键词。
                if score >= 2:
                    scored.append((score, row["updated_at"], row))
            scored.sort(key=lambda item: (-item[0], -item[1], item[2]["id"]))
            chosen, used = [], 0
            for _, _, row in scored:
                size = len(row["title"]) + len(row["content"]) + len(row["app"] or "") + 48
                if len(chosen) >= limit or used + size > max_chars:
                    continue
                chosen.append(self.entry(row))
                used += size
            return (chosen, current) if shown is not None else chosen
        return self._read(operation, ([], {}) if shown is not None else [])

    # -- 写入 ---------------------------------------------------------------
    def propose(self, title: Any, content: Any, kind: Any = "workflow", app: Any = None,
                *, source: dict[str, Any] | None = None, origin: str = "model") -> dict[str, Any]:
        fields = normalize_fields(title, content, kind, app)
        source = source or {}
        digest = _content_hash(fields["kind"], fields["content"])

        def operation(connection):
            for row in connection.execute(
                "SELECT id, revision, status FROM memories WHERE content_hash = ?", (digest,)
            ):
                status = "duplicate" if row["status"] == "candidate" else "duplicate_approved"
                return {"status": status, "memory_id": row["id"], "revision": row["revision"],
                        "unchanged": True}
            if self._counts(connection)["candidate"] >= MAX_CANDIDATES:
                raise MemoryStoreError("MEMORY_CANDIDATE_LIMIT")
            memory_id = "m_" + uuid.uuid4().hex[:12]
            now = _now_ms()
            connection.execute(
                "INSERT INTO memories (id, revision, status, kind, title, content, app, content_hash, "
                "source_session, source_turn, source_step, source_origin, validation, created_at, updated_at) "
                "VALUES (?, 1, 'candidate', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_user_review', ?, ?)",
                (memory_id, fields["kind"], fields["title"], fields["content"], fields["app"], digest,
                 _bounded_text(source.get("session_id")), _bounded_int(source.get("turn")),
                 _bounded_int(source.get("step")), origin, now, now),
            )
            return {"status": "candidate", "memory_id": memory_id, "revision": 1}
        return self._write(operation)

    def _locked_row(self, connection, memory_id: Any, revision: Any):
        if not isinstance(memory_id, str) or not memory_id or len(memory_id) > 64:
            raise MemoryStoreError("MEMORY_INVALID")
        if type(revision) is not int or revision < 1:
            raise MemoryStoreError("MEMORY_INVALID")
        row = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise MemoryStoreError("MEMORY_NOT_FOUND")
        if row["revision"] != revision:
            raise MemoryStoreError("MEMORY_REVISION_CONFLICT")
        return row

    def approve(self, memory_id: Any, revision: Any) -> dict[str, Any]:
        def operation(connection):
            row = self._locked_row(connection, memory_id, revision)
            if row["status"] != "candidate":
                raise MemoryStoreError("MEMORY_INVALID_STATE")
            # 批准时按当前规则复检，旧候选不能绕过新的拒绝条件。
            normalize_fields(row["title"], row["content"], row["kind"], row["app"])
            if self._counts(connection)["approved"] >= MAX_APPROVED:
                raise MemoryStoreError("MEMORY_APPROVED_LIMIT")
            connection.execute(
                "UPDATE memories SET status = 'approved', revision = revision + 1, "
                "validation = 'user_confirmed', updated_at = ? WHERE id = ?", (_now_ms(), memory_id))
            return {"status": "approved", "memory_id": memory_id, "revision": revision + 1}
        return self._write(operation)

    def update(self, memory_id: Any, revision: Any, *, title: Any = _UNSET, content: Any = _UNSET,
               kind: Any = _UNSET, app: Any = _UNSET) -> dict[str, Any]:
        """修订后一律回到候选，必须重新审核。"""
        def operation(connection):
            row = self._locked_row(connection, memory_id, revision)
            fields = normalize_fields(
                row["title"] if title is _UNSET else title,
                row["content"] if content is _UNSET else content,
                row["kind"] if kind is _UNSET else kind,
                row["app"] if app is _UNSET else app,
            )
            digest = _content_hash(fields["kind"], fields["content"])
            clash = connection.execute(
                "SELECT 1 FROM memories WHERE content_hash = ? AND id != ?", (digest, memory_id)
            ).fetchone()
            if clash is not None:
                raise MemoryStoreError("MEMORY_DUPLICATE")
            if row["status"] == "approved" and self._counts(connection)["candidate"] >= MAX_CANDIDATES:
                raise MemoryStoreError("MEMORY_CANDIDATE_LIMIT")
            connection.execute(
                "UPDATE memories SET status = 'candidate', revision = revision + 1, kind = ?, title = ?, "
                "content = ?, app = ?, content_hash = ?, source_origin = 'user_edit', "
                "validation = 'pending_user_review', updated_at = ? WHERE id = ?",
                (fields["kind"], fields["title"], fields["content"], fields["app"], digest,
                 _now_ms(), memory_id),
            )
            return {"status": "candidate", "memory_id": memory_id, "revision": revision + 1}
        return self._write(operation)

    def reject(self, memory_id: Any, revision: Any) -> dict[str, Any]:
        def operation(connection):
            row = self._locked_row(connection, memory_id, revision)
            if row["status"] != "candidate":
                raise MemoryStoreError("MEMORY_INVALID_STATE")
            connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return {"status": "rejected", "memory_id": memory_id, "revision": revision}
        return self._write(operation)

    def forget(self, memory_id: Any, revision: Any) -> dict[str, Any]:
        """删除行；secure_delete 会覆写释放页，不保留历史正文。"""
        def operation(connection):
            self._locked_row(connection, memory_id, revision)
            connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return {"status": "forgotten", "memory_id": memory_id, "revision": revision}
        return self._write(operation)
