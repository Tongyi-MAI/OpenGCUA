from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading

import pytest

import memory_store
from memory_runtime import MemoryService, resolve_memory_root
from memory_store import MemoryStore, MemoryStoreError


def propose(store, title="钉钉周报流程", content="发送周报前先让用户确认收件人列表", kind="workflow", app="钉钉", **kw):
    return store.propose(title, content, kind, app, **kw)


def code_of(operation):
    with pytest.raises(MemoryStoreError) as raised:
        operation()
    return raised.value.code


def test_default_root_shared_by_cli_and_app_and_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setenv("GENERALIST_DATA_DIR", str(tmp_path / "data"))
    assert resolve_memory_root() == tmp_path / "data" / "memory"
    assert resolve_memory_root(tmp_path / "custom") == tmp_path / "custom"
    monkeypatch.delenv("GENERALIST_DATA_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert resolve_memory_root() == tmp_path / "home/Library/Application Support/Generalist/memory"


def test_lazy_reads_never_create_directory_or_database(tmp_path):
    root = tmp_path / "data" / "memory"
    service = MemoryService(root)
    catalog = service.catalog_fields()
    assert catalog["entries"] == [] and catalog["generation"] == 0 and catalog["available"]
    assert catalog["policy"] == "candidate_review" and catalog["root"] == str(root)
    assert service.recall_block("钉钉 周报", {}, []) == ""
    assert "暂无记忆" in service.handle_command("list")
    assert not (tmp_path / "data").exists()


def test_candidate_never_recalled_until_approved_and_update_requires_review(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    created = propose(store)
    assert created["status"] == "candidate" and created["revision"] == 1
    entry = store.get(created["memory_id"])
    assert entry["validation"] == "pending_user_review" and entry["source"]["origin"] == "model"
    assert store.recall("钉钉 周报") == []
    assert code_of(lambda: store.approve(created["memory_id"], 2)) == "MEMORY_REVISION_CONFLICT"
    approved = store.approve(created["memory_id"], 1)
    assert approved["revision"] == 2 and approved["generation"] == created["generation"] + 1
    recalled = store.recall("钉钉 周报怎么发")
    assert [item["id"] for item in recalled] == [created["memory_id"]]
    assert recalled[0]["validation"] == "user_confirmed"
    # 修订已批准条目一律退回候选，重审前不再召回。
    changed = store.update(created["memory_id"], 2, content="发送周报前先让用户确认收件人和抄送")
    assert changed["status"] == "candidate" and changed["revision"] == 3
    assert store.get(created["memory_id"])["source"]["origin"] == "user_edit"
    assert store.recall("钉钉 周报") == []
    assert code_of(lambda: store.approve(created["memory_id"], 2)) == "MEMORY_REVISION_CONFLICT"
    store.approve(created["memory_id"], 3)
    assert "抄送" in store.recall("钉钉 周报")[0]["content"]
    assert code_of(lambda: store.reject(created["memory_id"], 4)) == "MEMORY_INVALID_STATE"
    assert code_of(lambda: store.approve("m_missing", 1)) == "MEMORY_NOT_FOUND"
    for bad in (0, True, "1", None):
        assert code_of(lambda: store.approve(created["memory_id"], bad)) == "MEMORY_INVALID"


def test_concurrent_approve_with_same_revision_has_single_winner(tmp_path):
    created = propose(MemoryStore(tmp_path / "memory"))
    barrier, outcomes = threading.Barrier(4), []

    def approve():
        store = MemoryStore(tmp_path / "memory")
        barrier.wait()
        try:
            outcomes.append(store.approve(created["memory_id"], 1)["status"])
        except MemoryStoreError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=approve) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["MEMORY_REVISION_CONFLICT"] * 3 + ["approved"]
    assert MemoryStore(tmp_path / "memory").get(created["memory_id"])["revision"] == 2


def test_size_limits_and_candidate_approved_caps(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory")
    assert code_of(lambda: propose(store, title="长" * 121)) == "MEMORY_TOO_LARGE"
    assert code_of(lambda: propose(store, content="长" * 2001)) == "MEMORY_TOO_LARGE"
    assert code_of(lambda: propose(store, kind="secretive")) == "MEMORY_INVALID"
    assert code_of(lambda: propose(store, title="  ")) == "MEMORY_INVALID"
    assert propose(store, title="长" * 120, content="短" * 2000)["status"] == "candidate"
    service = MemoryService(tmp_path / "service")
    too_big = service.propose({"title": "t", "content": "c", "padding": "x" * 9000})
    assert too_big == {"ok": False, "status": "rejected", "memory_id": None, "revision": None,
                       "error_code": "MEMORY_TOO_LARGE"}
    assert not (tmp_path / "service").exists()
    monkeypatch.setattr(memory_store, "MAX_CANDIDATES", 2)
    second = propose(store, content="第二条流程说明")
    assert code_of(lambda: propose(store, content="第三条流程说明")) == "MEMORY_CANDIDATE_LIMIT"
    monkeypatch.setattr(memory_store, "MAX_APPROVED", 1)
    store.approve(second["memory_id"], 1)
    first = next(entry for entry in store.catalog()["entries"] if entry["status"] == "candidate")
    assert code_of(lambda: store.approve(first["id"], 1)) == "MEMORY_APPROVED_LIMIT"
    assert (memory_store.MAX_TITLE, memory_store.MAX_CONTENT, memory_store.MAX_REQUEST_BYTES) == (120, 2000, 8192)


def test_global_preferences_and_app_specific_workflows_have_different_scope(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    preference = propose(store, title="回复格式", content="回复采用简短段落", kind="preference", app=None)
    workflow = propose(store, title="保存文件", content="保存文件后核对结果", kind="workflow", app="钉钉")
    assert store.recall("保存文件") == []
    store.approve(preference["memory_id"], 1)
    store.approve(workflow["memory_id"], 1)
    assert [row["id"] for row in store.recall("打开计算器")] == [preference["memory_id"]]
    assert [row["id"] for row in store.recall("保存文件")] == [preference["memory_id"]]
    assert {row["id"] for row in store.recall("钉钉保存文件")} == {preference["memory_id"], workflow["memory_id"]}
    rows, current = store.recall("", shown={preference["memory_id"]: 2, workflow["memory_id"]: 2})
    assert [row["id"] for row in rows] == [preference["memory_id"]]
    assert set(current) == {preference["memory_id"], workflow["memory_id"]}


@pytest.mark.parametrize("app,query,matched", [
    ("Google Chrome", "Google Docs 保存文件", False),
    ("Microsoft Excel", "Microsoft Word 保存文件", False),
    ("com.apple.mail", "com.apple.Safari 保存文件", False),
    ("com.apple.mail", "com.apple.mailhelper 保存文件", False),
    ("com.apple.mail", "org.example.com.apple.mail 保存文件", False),
    ("飞书会议", "飞书保存文件", False),
    ("X", "保存文件", False),
    ("To Do", "保存文件", False),
    ("To In", "保存文件", False),
    ("C++", "保存文件", False),
    ("Chrome", "Chromebook 保存文件", False),
    ("Google Chrome", "Chrome 保存文件", False),
    ("Google Chrome", "Google Chrome.beta 保存文件", False),
    ("Google Chrome", "在 GOOGLE   chrome 保存文件", True),
    ("Google Chrome", "保存文件，请使用 Google Chrome.", True),
    ("To In", "在 to in 保存文件", True),
    ("C++", "在 C++ 保存文件", True),
    ("com.apple.mail", "在 COM.APPLE.MAIL 保存文件", True),
    ("X", "在 X 保存文件", True),
    ("To Do", "在 to do 保存文件", True),
    ("飞书会议", "在飞书会议保存文件", True),
])
def test_recall_app_scope_requires_complete_name_not_shared_tokens(tmp_path, app, query, matched):
    store = MemoryStore(tmp_path / "memory")
    created = propose(store, title="保存文件", content="保存文件后核对结果", app=app)
    store.approve(created["memory_id"], 1)
    rows = store.recall(query)
    assert [row["id"] for row in rows] == ([created["memory_id"]] if matched else [])
    # 请求未涉及该应用不等于记忆失效；历史版本仍有效，不生成撤销提示。
    shown, revoked = {created["memory_id"]: 2}, []
    block = MemoryService(store.directory).recall_block(query, shown, revoked)
    assert ("保存文件后核对结果" in block) == matched
    assert shown == {created["memory_id"]: 2} and revoked == []


def test_paged_catalog_reaches_approved_items_beyond_first_candidates(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    for index in range(70):
        result = propose(store, title=f"流程 {index}", content=f"第 {index} 条本地流程说明")
        if index >= 65:
            store.approve(result["memory_id"], 1)
    first = store.catalog()
    second = store.catalog(offset=64)
    assert len(first["entries"]) == 64 and first["next_offset"] == 64 and first["offset"] == 0
    assert len(second["entries"]) == 6 and second["next_offset"] is None and second["offset"] == 64
    assert second["generation"] == first["generation"]
    assert len({entry["id"] for entry in first["entries"] + second["entries"]}) == 70
    assert sum(entry["status"] == "approved" for entry in second["entries"]) == 5
    assert "/memory list 64" in MemoryService(tmp_path / "memory").handle_command("list")
    for offset in (-1, True, 385):
        assert code_of(lambda: store.catalog(offset=offset)) == "MEMORY_INVALID"


def test_permissions_and_symlink_roots_fail_closed(tmp_path):
    root = tmp_path / "data" / "memory"
    service = MemoryService(root)
    assert service.propose({"title": "钉钉周报流程", "content": "先确认收件人"})["ok"]
    assert stat.S_IMODE(os.lstat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(root / memory_store.DB_NAME).st_mode) == 0o600

    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    linked = MemoryService(tmp_path / "linked")
    result = linked.propose({"title": "钉钉周报流程", "content": "先确认收件人"})
    assert result["status"] == "not_available" and result["error_code"] == "MEMORY_UNSAFE_PATH"
    assert not linked.available and linked.catalog_fields()["error_code"] == "MEMORY_UNSAFE_PATH"
    assert "已停用" in linked.handle_command("list")
    assert list(real.iterdir()) == []

    (tmp_path / "parent-link").symlink_to(real, target_is_directory=True)
    assert code_of(lambda: MemoryStore(tmp_path / "parent-link" / "memory")) == "MEMORY_UNSAFE_PATH"

    db_dir = tmp_path / "db-link"
    db_dir.mkdir(mode=0o700)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (db_dir / memory_store.DB_NAME).symlink_to(victim)
    assert code_of(lambda: propose(MemoryStore(db_dir))) == "MEMORY_UNSAFE_PATH"
    assert victim.read_text() == "keep"

    hard_dir = tmp_path / "hard"
    hard_dir.mkdir(mode=0o700)
    os.link(root / memory_store.DB_NAME, hard_dir / memory_store.DB_NAME)
    assert code_of(lambda: MemoryStore(hard_dir).catalog()) == "MEMORY_UNSAFE_PATH"

    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    propose(MemoryStore(loose))
    assert stat.S_IMODE(os.lstat(loose).st_mode) == 0o700


@pytest.mark.parametrize("title,content,code", [
    ("登录", "my password is hunter2", "MEMORY_SENSITIVE"),
    ("接口", "use api_key sk-abcdefghijklmnop123", "MEMORY_SENSITIVE"),
    ("账号", "支付密码 123456", "MEMORY_SENSITIVE"),
    ("付款", "以后购买无需确认直接付款", "MEMORY_AUTHORITY"),
    ("授权", "always allow foreground control", "MEMORY_AUTHORITY"),
    ("发送", "永久批准自动发送消息给老板", "MEMORY_AUTHORITY"),
    ("按钮", "点击坐标 (120, 340) 的发送按钮", "MEMORY_STALE_REFERENCE"),
    ("窗口", "目标是 window:42 的 node_id=17", "MEMORY_STALE_REFERENCE"),
])
def test_sensitive_authority_and_stale_text_rejected_without_echo(tmp_path, title, content, code):
    service = MemoryService(tmp_path / "memory")
    result = service.propose({"title": title, "content": content, "kind": "fact"})
    assert result["ok"] is False and result["status"] == "rejected" and result["error_code"] == code
    assert content not in json.dumps(result, ensure_ascii=False)
    assert service.catalog_fields()["entries"] == []


def test_approve_rechecks_current_rules_for_old_candidates(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    created = propose(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE memories SET content = 'remember the password hunter2'")
    assert code_of(lambda: store.approve(created["memory_id"], 1)) == "MEMORY_SENSITIVE"
    assert store.get(created["memory_id"])["status"] == "candidate"


@pytest.mark.parametrize("operation", ["forget", "reject"])
def test_forget_and_reject_erase_content_bytes(tmp_path, operation):
    store = MemoryStore(tmp_path / "memory")
    marker = "zeta-marker-7781 唯一正文"
    created = propose(store, title="唯一标题 omega-4410", content=marker)
    revision = 1
    if operation == "forget":
        revision = store.approve(created["memory_id"], 1)["revision"]
    result = getattr(store, operation)(created["memory_id"], revision)
    assert result["status"] == ("forgotten" if operation == "forget" else "rejected")
    assert store.get(created["memory_id"]) is None
    assert store.recall("omega-4410 zeta-marker-7781") == []
    raw = b"".join(path.read_bytes() for path in (tmp_path / "memory").iterdir())
    assert "zeta-marker-7781".encode() not in raw and "omega-4410".encode() not in raw


def test_duplicates_only_dedupe_candidates_and_never_overwrite_approved(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    first = propose(store)
    again = propose(store, title="另一个标题", content="  发送周报前先让用户确认收件人列表 ")
    assert again["status"] == "duplicate" and again["memory_id"] == first["memory_id"]
    assert again["generation"] == first["generation"]
    store.approve(first["memory_id"], 1)
    assert propose(store)["status"] == "duplicate_approved"
    same_title = propose(store, content="另一种做法：先发草稿给自己")
    assert same_title["status"] == "candidate" and same_title["memory_id"] != first["memory_id"]
    assert store.get(first["memory_id"])["content"] == "发送周报前先让用户确认收件人列表"
    clash = lambda: store.update(same_title["memory_id"], 1, content="发送周报前先让用户确认收件人列表")
    assert code_of(clash) == "MEMORY_DUPLICATE"


@pytest.mark.parametrize("other_app", ["飞书", None])
def test_duplicate_content_does_not_reuse_approval_from_another_app(tmp_path, other_app):
    store = MemoryStore(tmp_path / "memory")
    first = propose(store)
    store.approve(first["memory_id"], 1)
    # 重启服务也必须保留作用域；沿用已有 schema/hash，不迁移用户数据。
    fresh = MemoryStore(tmp_path / "memory")
    second = propose(fresh, app=other_app)
    assert second["status"] == "candidate" and second["memory_id"] != first["memory_id"]
    assert fresh.get(first["memory_id"])["status"] == "approved"
    assert fresh.get(second["memory_id"])["status"] == "candidate"
    assert fresh.recall("飞书 周报") == []
    duplicate = propose(fresh, app=other_app)
    assert duplicate["status"] == "duplicate" and duplicate["memory_id"] == second["memory_id"]
    with sqlite3.connect(fresh.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == memory_store.SCHEMA_VERSION


def test_duplicate_scope_normalizes_case_and_whitespace(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    first = propose(store, app="Google Chrome")
    again = propose(store, app="  google   CHROME  ")
    assert again["status"] == "duplicate" and again["memory_id"] == first["memory_id"]
    assert again["generation"] == first["generation"]


def test_update_duplicate_detection_uses_destination_scope(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    first = propose(store, app="钉钉")
    store.approve(first["memory_id"], 1)
    second = propose(store, content="发送周报前先确认附件", app="飞书")
    # 相同正文在不同应用可独立存在，修改后的条目仍要审核。
    changed = store.update(second["memory_id"], 1, content=store.get(first["memory_id"])["content"])
    assert changed["status"] == "candidate" and changed["revision"] == 2
    assert store.recall("飞书 周报") == []
    assert code_of(lambda: store.update(second["memory_id"], 2, app="钉钉")) == "MEMORY_DUPLICATE"
    assert store.get(second["memory_id"])["app"] == "飞书"
    assert store.get(second["memory_id"])["revision"] == 2
    assert store.get(first["memory_id"])["status"] == "approved"


@pytest.mark.parametrize("failed_pragma", ["PRAGMA secure_delete=ON", "PRAGMA foreign_keys=ON"])
def test_connection_setup_failure_is_bounded_closes_handle_and_allows_recovery(tmp_path, monkeypatch, failed_pragma):
    service = MemoryService(tmp_path / "memory")
    created = service.propose({"title": "钉钉周报流程", "content": "先核对附件", "app": "钉钉"})
    memory_id = created["memory_id"]
    assert service.mutate("approve", memory_id, 1)[0]["ok"]
    shown, revoked = {}, []
    assert "先核对附件" in service.recall_block("钉钉 周报", shown, revoked)
    original_connect = sqlite3.connect
    opened, closed = [], []

    class SetupFailure(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement == failed_pragma:
                raise sqlite3.OperationalError("private-database-diagnostic")
            return super().execute(statement, *args, **kwargs)

        def close(self):
            closed.append(self)
            return super().close()

    def connect(*args, **kwargs):
        connection = original_connect(*args, factory=SetupFailure, **kwargs)
        opened.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(memory_store.sqlite3, "connect", connect)
        # 连接已成功但 PRAGMA 初始化失败：读、候选、用户审核均须走相同有界错误边界。
        block = service.recall_block("钉钉 周报", shown, revoked)
        catalog = service.catalog_fields()
        proposal = service.propose({"title": "周报草稿", "content": "先核对标题"})
        mutation, _ = service.mutate("forget", memory_id, 2)
        assert code_of(lambda: MemoryStore(service.root).get(memory_id)) == "MEMORY_UNAVAILABLE"
        assert "MEMORY_UNAVAILABLE" in block and "无法核验" in block
        assert "先核对附件" not in block and "已被用户删除或修改" not in block
        assert shown == {memory_id: 2} and revoked == []
        assert not catalog["available"] and catalog["error_code"] == "MEMORY_UNAVAILABLE"
        assert proposal["status"] == "not_available" and proposal["error_code"] == "MEMORY_UNAVAILABLE"
        assert not mutation["ok"] and mutation["error_code"] == "MEMORY_UNAVAILABLE"
        assert "private-database-diagnostic" not in json.dumps([block, catalog, proposal, mutation])
        assert opened and closed == opened
        for connection in opened:
            with pytest.raises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    # 不自动修复或批准；初始化恢复可用后重读原版本，失败的遗忘没有落盘。
    assert service.available and service.error_code is None
    assert "先核对附件" in service.recall_block("钉钉 周报", shown, revoked)
    assert shown == {memory_id: 2} and revoked == []
    assert MemoryStore(service.root).get(memory_id)["revision"] == 2
    assert service.catalog_fields()["candidate_count"] == 0


def test_corrupt_database_is_unavailable_without_rewriting_or_disclosing_bytes(tmp_path):
    root = tmp_path / "memory"
    root.mkdir(mode=0o700)
    path = root / memory_store.DB_NAME
    corrupted = b"not-a-sqlite-database private-fixture-body" * 8
    path.write_bytes(corrupted)
    path.chmod(0o600)
    service = MemoryService(root)
    shown, revoked = {"m_previous": 2}, []
    block = service.recall_block("钉钉 周报", shown, revoked)
    catalog = service.catalog_fields()
    proposal = service.propose({"title": "周报流程", "content": "先核对附件"})
    assert "MEMORY_UNAVAILABLE" in block and "无法核验" in block
    assert shown == {"m_previous": 2} and revoked == []
    assert not catalog["available"] and catalog["error_code"] == "MEMORY_UNAVAILABLE"
    assert proposal["error_code"] == "MEMORY_UNAVAILABLE" and not proposal["ok"]
    assert "private-fixture-body" not in json.dumps([block, catalog, proposal])
    assert path.read_bytes() == corrupted and service.available


def test_schema_version_unsupported_disables_service(tmp_path):
    root = tmp_path / "memory"
    service = MemoryService(root)
    service.propose({"title": "钉钉周报流程", "content": "先确认收件人"})
    with sqlite3.connect(root / memory_store.DB_NAME) as connection:
        connection.execute("PRAGMA user_version=99")
    fresh = MemoryService(root)
    assert fresh.catalog_fields()["error_code"] == "MEMORY_SCHEMA_UNSUPPORTED"
    assert not fresh.available
    assert fresh.propose({"title": "a", "content": "b"})["status"] == "not_available"


def test_catalog_bounded_with_explicit_omitted(tmp_path, monkeypatch):
    assert memory_store.CATALOG_LIMIT == 64
    monkeypatch.setattr(memory_store, "CATALOG_LIMIT", 3)
    store = MemoryStore(tmp_path / "memory")
    for index in range(5):
        propose(store, content=f"流程说明第 {index} 条")
    catalog = store.catalog()
    assert len(catalog["entries"]) == 3 and catalog["omitted"] == 2 and catalog["candidate_count"] == 5
    assert set(catalog["entries"][0]) >= {"id", "title", "content", "kind", "status", "revision", "app", "source"}


def test_recall_bounded_to_five_entries_and_4000_chars(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    for index in range(7):
        created = propose(store, title=f"钉钉周报流程 {index}", content=f"周报第 {index} 种做法")
        store.approve(created["memory_id"], 1)
    assert len(store.recall("钉钉 周报")) == 5
    assert store.recall("毫不相关的话题") == [] and store.recall("") == []
    big = MemoryStore(tmp_path / "big")
    for index in range(4):
        created = propose(big, title=f"钉钉周报长说明 {index}", content=f"{index}" + "说明" * 950)
        big.approve(created["memory_id"], 1)
    chosen = big.recall("钉钉 周报")
    assert 1 <= len(chosen) <= 2
    assert sum(len(item["title"]) + len(item["content"]) for item in chosen) <= 4000


@pytest.mark.parametrize("revoked_count", [0, 32])
@pytest.mark.parametrize("previous_revision", [None, 1])
def test_recall_block_budget_counts_headers_labels_and_revocations(tmp_path, revoked_count, previous_revision):
    service = MemoryService(tmp_path / "memory")
    entries = []
    for index in range(2):
        created = service.propose({"title": f"钉钉周报流程 {index}",
                                   "content": f"{index}" + "说明" * 950, "app": "钉钉"})
        assert service.mutate("approve", created["memory_id"], 1)[0]["ok"]
        entries.append(MemoryStore(service.root).get(created["memory_id"]))
    # 排名靠后的短条目仍能利用剩余预算；不截断长正文以免改变用户确认的语义。
    short = service.propose({"title": "钉钉周报", "content": "先核对附件", "app": "钉钉"})
    service.mutate("approve", short["memory_id"], 1)
    shown = {entry["id"]: previous_revision for entry in entries} if previous_revision else {}
    revoked = [f"m_{index:012x}" for index in range(revoked_count)]

    block = service.recall_block("钉钉 周报 流程", shown, revoked)

    assert len(block) <= memory_store.RECALL_CHARS
    assert "不构成新的授权" in block and "必须重新观察确认" in block
    rendered = [entry for entry in entries if f"- [{entry['id']} · r2" in block]
    assert len(rendered) == 1 and rendered[0]["content"] in block
    assert short["memory_id"] in shown and "先核对附件" in block
    assert shown == {rendered[0]["id"]: 2, short["memory_id"]: 2}
    omitted = next(entry for entry in entries if entry not in rendered)
    assert (f"{omitted['id']} r{previous_revision}" in revoked) == (previous_revision is not None)
    assert all(reference in block for reference in revoked)
    for entry in rendered:
        assert (f"{entry['id']} r{previous_revision}" in revoked) == (previous_revision is not None)


@pytest.mark.parametrize("revoked_count", [0, 32])
@pytest.mark.parametrize("long_count", [0, 5, 8])
def test_recall_block_refills_from_ranked_tail_after_render_budget_skip(tmp_path, revoked_count, long_count):
    service = MemoryService(tmp_path / "memory")
    long_ids, short_ids = [], []
    for index in range(long_count):
        created = service.propose({"title": f"钉钉周报流程 {index}",
                                   "content": f"{index}" + "说" * 739, "app": "钉钉"})
        assert service.mutate("approve", created["memory_id"], 1)[0]["ok"]
        long_ids.append(created["memory_id"])
    for index in range(8):
        created = service.propose({"title": "钉钉周报", "content": f"先核对附件 {index}", "app": "钉钉"})
        assert service.mutate("approve", created["memory_id"], 1)[0]["ok"]
        short_ids.append(created["memory_id"])
    candidate = service.propose({"title": "钉钉周报流程", "content": "候选正文不应召回", "app": "钉钉"})
    other = service.propose({"title": "钉钉周报流程", "content": "其它应用正文不应召回", "app": "飞书"})
    assert service.mutate("approve", other["memory_id"], 1)[0]["ok"]
    if long_count:
        # 粗估预算下前五项均为长条目；最终渲染后其中至少一项容纳不下。
        initial = MemoryStore(service.root).recall("钉钉 周报 流程")
        assert len(initial) == 5 and all(entry["id"] in long_ids for entry in initial)
    shown, revoked = {}, [f"m_{index:012x} r2" for index in range(revoked_count)]

    block = service.recall_block("钉钉 周报 流程", shown, revoked)

    assert len(block) <= memory_store.RECALL_CHARS
    assert block.count("- [m_") == memory_store.RECALL_LIMIT == len(shown)
    assert any(memory_id in shown for memory_id in short_ids)
    assert all(f"- [{memory_id} · r{revision}" in block for memory_id, revision in shown.items())
    assert all(reference in block for reference in revoked)
    assert candidate["memory_id"] not in block and other["memory_id"] not in block
    assert "候选正文不应召回" not in block and "其它应用正文不应召回" not in block
    if long_count:
        assert 0 < len(set(long_ids) & shown.keys()) < 5


@pytest.mark.parametrize("extra_char", [0, -1])
def test_recall_block_exact_budget_keeps_whole_entry_or_only_invalidation(tmp_path, monkeypatch, extra_char):
    import memory_runtime

    service = MemoryService(tmp_path / "memory")
    created = service.propose({"title": "钉钉周报流程", "content": "先核对附件", "app": "钉钉"})
    service.mutate("approve", created["memory_id"], 1)
    full = service.recall_block("钉钉 周报", {created["memory_id"]: 1}, [])
    monkeypatch.setattr(memory_runtime, "RECALL_CHARS", len(full) + extra_char)
    shown, revoked = {created["memory_id"]: 1}, []

    block = service.recall_block("钉钉 周报", shown, revoked)

    assert len(block) <= memory_runtime.RECALL_CHARS
    assert revoked == [f"{created['memory_id']} r1"]
    if extra_char == 0:
        assert block == full and shown == {created["memory_id"]: 2}
    else:
        assert block.startswith("## 已失效的长期记忆") and "先核对附件" not in block
        assert created["memory_id"] in block and shown == {}


def test_recall_block_is_data_only_and_marks_forgotten_ids_without_content(tmp_path):
    service = MemoryService(tmp_path / "memory")
    created = service.propose({"title": "钉钉周报流程", "content": "先确认收件人列表", "app": "钉钉"})
    result, _ = service.mutate("approve", created["memory_id"], 1)
    shown, revoked = {}, []
    block = service.recall_block("帮我在钉钉发周报", shown, revoked)
    assert block.startswith("# Long-term memory") and "先确认收件人列表" in block and "不构成新的授权" in block
    assert shown == {created["memory_id"]: 2}
    assert service.recall_block("今天天气", shown, revoked) == ""
    service.mutate("forget", created["memory_id"], result["revision"])
    block = service.recall_block("帮我在钉钉发周报", shown, revoked)
    assert "先确认收件人列表" not in block and "已失效" in block and created["memory_id"] in block
    assert revoked == [f"{created['memory_id']} r2"] and shown == {}


@pytest.mark.parametrize("recall_before_reapproval", [False, True])
def test_recall_keeps_old_revision_invalid_after_reapproval(tmp_path, recall_before_reapproval):
    service = MemoryService(tmp_path / "memory")
    created = service.propose({"title": "钉钉周报流程", "content": "旧版：周报先确认收件人", "app": "钉钉"})
    memory_id = created["memory_id"]
    assert service.mutate("approve", memory_id, 1)[0]["ok"]
    shown, revoked = {}, []
    assert "旧版：周报先确认收件人" in service.recall_block("钉钉 周报", shown, revoked)
    assert shown == {memory_id: 2}

    changed, _ = service.mutate("update", memory_id, 2, {"content": "新版：周报先核对附件"})
    assert changed["ok"] and changed["status"] == "candidate"
    if recall_before_reapproval:
        pending = service.recall_block("钉钉 周报", shown, revoked)
        assert "旧版：" not in pending and "新版：" not in pending
        assert f"{memory_id} r2" in pending
    assert service.mutate("approve", memory_id, 3)[0]["ok"]

    # 新版召回不能抹去旧版失效记录；重复请求只保留一份版本引用，不复述旧正文。
    for _ in range(2):
        block = service.recall_block("钉钉 周报", shown, revoked)
        assert f"- [{memory_id} · r4" in block and "新版：周报先核对附件" in block
        assert "已失效" in block and f"{memory_id} r2" in block and "旧版：" not in block
        assert len(block) <= memory_store.RECALL_CHARS
        assert shown == {memory_id: 4} and revoked == [f"{memory_id} r2"]

    assert service.mutate("update", memory_id, 4, {"content": "第三版：周报先检查格式"})[0]["ok"]
    assert service.mutate("approve", memory_id, 5)[0]["ok"]
    block = service.recall_block("钉钉 周报", shown, revoked)
    assert "第三版：周报先检查格式" in block and "新版：" not in block
    assert f"{memory_id} r2" in block and f"{memory_id} r4" in block
    assert shown == {memory_id: 6} and revoked == [f"{memory_id} r2", f"{memory_id} r4"]

    assert service.mutate("forget", memory_id, 6)[0]["ok"]
    block = service.recall_block("钉钉 周报", shown, revoked)
    assert "第三版：" not in block and f"{memory_id} r6" in block
    assert shown == {} and revoked == [f"{memory_id} r2", f"{memory_id} r4", f"{memory_id} r6"]


@pytest.mark.parametrize("error_code", [
    "MEMORY_UNAVAILABLE", "MEMORY_UNSAFE_PATH", "MEMORY_SCHEMA_UNSUPPORTED",
])
def test_recall_failure_does_not_claim_user_revocation_or_discard_versions(tmp_path, monkeypatch, error_code):
    service = MemoryService(tmp_path / "memory")
    created = service.propose({"title": "钉钉周报流程", "content": "先核对附件", "app": "钉钉"})
    memory_id = created["memory_id"]
    assert service.mutate("approve", memory_id, 1)[0]["ok"]
    shown, revoked = {}, ["m_old r2"]
    assert "先核对附件" in service.recall_block("钉钉 周报", shown, revoked)

    def fail(*_args, **_kwargs):
        raise MemoryStoreError(error_code)

    with monkeypatch.context() as patch:
        patch.setattr(MemoryStore, "recall", fail)
        for _ in range(2):
            block = service.recall_block("钉钉 周报", shown, revoked)
            assert block.startswith("# Long-term memory") and error_code in block
            assert "无法核验" in block and "不得作为依据" in block
            assert "先核对附件" not in block and memory_id not in block
            assert "已被用户删除或修改" not in block and "## 已失效" not in block
            assert len(block) <= memory_store.RECALL_CHARS
            assert shown == {memory_id: 2} and revoked == ["m_old r2"]
    if error_code == "MEMORY_UNAVAILABLE":
        assert service.available
        block = service.recall_block("钉钉 周报", shown, revoked)
        assert "先核对附件" in block and "无法核验" not in block
        assert shown == {memory_id: 2} and revoked == ["m_old r2"]
    else:
        assert not service.available
        assert error_code in service.recall_block("钉钉 周报", shown, revoked)
        assert MemoryStore(service.root).get(memory_id)["revision"] == 2


def test_locked_recall_retains_tracking_and_detects_forget_after_recovery(tmp_path, monkeypatch):
    service = MemoryService(tmp_path / "memory")
    created = service.propose({"title": "钉钉周报流程", "content": "先核对附件", "app": "钉钉"})
    memory_id = created["memory_id"]
    assert service.mutate("approve", memory_id, 1)[0]["ok"]
    shown, revoked = {}, []
    assert "先核对附件" in service.recall_block("钉钉 周报", shown, revoked)
    monkeypatch.setattr(memory_store, "_BUSY_TIMEOUT", 0.01)
    connection = sqlite3.connect(service.root / memory_store.DB_NAME)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        block = service.recall_block("钉钉 周报", shown, revoked)
        assert "MEMORY_UNAVAILABLE" in block and "不得作为依据" in block
        assert shown == {memory_id: 2} and revoked == [] and service.available
    finally:
        connection.rollback()
        connection.close()
    assert service.mutate("forget", memory_id, 2)[0]["ok"]
    block = service.recall_block("钉钉 周报", shown, revoked)
    assert "已失效" in block and f"{memory_id} r2" in block and "先核对附件" not in block
    assert shown == {} and revoked == [f"{memory_id} r2"]


def test_mutate_returns_same_generation_catalog_and_bounded_errors(tmp_path):
    service = MemoryService(tmp_path / "memory")
    created = service.propose({"title": "钉钉周报流程", "content": "先确认收件人"})
    result, catalog = service.mutate("approve", created["memory_id"], 1)
    assert result["ok"] and result["generation"] == catalog["generation"] == created["generation"] + 1
    assert catalog["approved_count"] == 1 and catalog["source_command_id"] is None
    stale, catalog = service.mutate("approve", created["memory_id"], 1)
    assert stale["ok"] is False and stale["error_code"] == "MEMORY_REVISION_CONFLICT"
    assert stale["generation"] == catalog["generation"] == result["generation"]
    secret, _ = service.mutate("update", created["memory_id"], 2, {"content": "token abc"})
    assert secret["error_code"] == "MEMORY_SENSITIVE" and "abc" not in json.dumps(secret)
    assert service.mutate("update", created["memory_id"], 2, {})[0]["error_code"] == "MEMORY_INVALID"


def test_text_commands_parse_explicitly(tmp_path):
    service = MemoryService(tmp_path / "memory")
    created = service.propose({"title": "钉钉周报流程", "content": "先确认收件人", "app": "钉钉"})
    memory_id = created["memory_id"]
    listing = service.handle_command("")
    assert "[候选] " + memory_id + " r1" in listing and "先确认收件人" in listing and "候选不是已核验事实" in listing
    assert "标题：钉钉周报流程" in service.handle_command(f"show {memory_id}")
    assert "revision 必须是整数" in service.handle_command(f"approve {memory_id} x")
    assert service.handle_command("approve") .startswith("用法")
    assert service.handle_command("delete everything").startswith("用法")
    assert "MEMORY_REVISION_CONFLICT" in service.handle_command(f"approve {memory_id} 9")
    assert service.handle_command(f"approve {memory_id} 1").startswith("已批准")
    assert "kind 必须是" in service.handle_command(f"update {memory_id} 2 kind=secret")
    assert service.handle_command(f"update {memory_id} 2 title=钉钉周报发送流程").startswith("已修改并退回候选")
    entry = MemoryStore(tmp_path / "memory").get(memory_id)
    assert entry["title"] == "钉钉周报发送流程" and entry["status"] == "candidate" and entry["revision"] == 3
    service.handle_command(f"update {memory_id} 3 app=")
    service.handle_command(f"update {memory_id} 4 先确认收件人 再确认附件")
    entry = MemoryStore(tmp_path / "memory").get(memory_id)
    assert entry["app"] is None and entry["content"] == "先确认收件人 再确认附件"
    assert service.handle_command(f"reject {memory_id} 5").startswith("已拒绝")
    assert MemoryStore(tmp_path / "memory").get(memory_id) is None
