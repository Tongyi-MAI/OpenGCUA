"""公开 mutation 的派发前事实反馈与工具手册：真实 mai_ui Runtime + 离线 fake adapter + 真实 worker。

不连驱动、不碰 GUI/TCC；adapter 记录的就是 native 真正发出的 wire，没有记录即未派发。
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re

import pytest

from action_feedback import action_feedback, remember_feedback, render_feedback
from action_journal import ActionJournal
from code_executor import empty_control
from test_action_journal_executor import calls, executor, stages


WORKER = r'''
import asyncio, base64, json, sys
from pathlib import Path
import code_runtime as rt
import mai_ui

APP = "app:42:1"
NODE = dict(id="field", parent_id=None, app_id=APP, window_id="w1", class_name="AXTextField",
            automation_id=None, role="AXTextField", name="City", text="", hint=None, text_status="readable",
            bounds=dict(x=10.0, y=20.0, width=30.0, height=40.0), enabled=None, visible=None, focused=None,
            editable=True, password=False, actions=[dict(id="macos.ax.press")],
            editable_source="role_hint", value_settable=None, value_settable_source="unknown",
            actions_source="native")
SHELL = dict(id="local", state="ready", default_interpreter="/bin/sh", interpreters=["/bin/sh"],
             kind="host_process", default_cwd="/", argv=True, streaming=False, stdin=True, pty=False,
             cancellation="process_group", identity=dict(platform="macos", principal="fake", uid=1, gid=1))
# 故意不声明 ui.set_text：用来验证 SDK 本地 _require 拒绝。
CAPS = ["device.list_apps", "ui.observe.tree", "ui.click.primary", "ui.input_text", "ui.press_key",
        "ui.perform", "shell.run", "files.write", "files.mkdir"]
DESCRIPTION = dict(protocol_major=1, platform="macos", provider="fake.bridge", shell_providers=[SHELL],
                   capabilities={name: dict(state="ready") for name in CAPS})
ERRORS = {"reject": "INPUT_REFUSED", "unsupported": "UNSUPPORTED_CAPABILITY", "unknown": "TIMEOUT"}

class Adapter:
    platform, provider = "macos", "fake.bridge"
    async def start(self): pass
    async def close(self): pass
    async def request(self, message):
        op = message["op"]
        if op == "describe":
            return dict(result=DESCRIPTION)
        if op == "list_apps":
            return dict(result=dict(apps=[dict(id=APP, label="Fake", launchable=True)], complete=True))
        if op == "observe":
            return dict(result=dict(width=800, height=600, captured_at=0, revision="r1", truncated=False,
                                    source="fake", nodes=[dict(NODE)]))
        with open(Path(rt._CONFIG["work_dir"]) / "native_calls.jsonl", "a") as stream:
            stream.write(json.dumps(message) + "\n")
        if message.get("text") in ERRORS:
            return dict(error=dict(code=ERRORS[message["text"]], message="adapter says no"))
        if op == "shell":
            return dict(result=dict(state="exited", exit_code=0, stdout=base64.b64encode(b"ok").decode(),
                                    stderr="", stdout_truncated=False, stderr_truncated=False,
                                    duration_ms=1, cleanup="complete"))
        return dict(result=dict(ok=True))

def bootstrap(loop):
    async def open_session():
        runtime = mai_ui.Runtime()
        info = await runtime.add_adapter(Adapter(), name="fake", resource_id="fake")
        rt._SDK_KEEPALIVE = runtime._endpoints["fake"].pump
        mu = runtime.session()
        return mu, await mu.register(info, name="fake")
    mu, device = loop.run_until_complete(open_session())
    rt._MAI_SESSION = mu
    rt._instrument_sdk_session(mu)
    return dict(mu=mu, device=device, APP=APP, json=json, asyncio=asyncio)
rt._bootstrap = bootstrap
rt._close_sdk_session = lambda **kwargs: asyncio.sleep(0)
raise SystemExit(rt.main(sys.argv))
'''

BIND = "app = await device.apps.get(APP)\nax = await app.ui.ax.observe()\nnode = ax.tree.nodes[0]\n"

PRECHECK_CELL = BIND + '''
errors = []
for call in (lambda: app.ui.input.text(1),
             lambda: app.ui.ax.set_text(node, "SECRET-VALUE"),
             lambda: app.ui.ax.perform(node, "macos.ax.raise"),
             lambda: device.files.write("relative.txt", b"SECRET-BYTES")):
    try:
        await call()
    except Exception as exc:
        errors.append([type(exc).__name__, exc.code, str(exc)])
try:
    app.ui.input.text("x", bogus=1)
except TypeError as exc:
    errors.append(["TypeError", None, "sync"])
print(json.dumps(errors))
print(mu.help("Input.text"))
'''


def _run(tmp_path, cell, journal=None):
    async def scenario():
        instance = executor(tmp_path, journal, source=WORKER)
        try:
            return await instance.run(cell, [])
        finally:
            await instance.close()
    return asyncio.run(scenario())


def _rows(result, **match):
    return [row for row in result.control["action_feedback"]
            if all(row.get(key) == value for key, value in match.items())]


def _sdk_tools(result):
    return [event for event in result.control["tool_calls"] if event["tool"].startswith("mai_ui.")]


@pytest.mark.parametrize("journaled", [False, True])
def test_caught_python_precheck_is_reported_once_without_dispatch(tmp_path, journaled):
    journal = ActionJournal(tmp_path / "j", "host") if journaled else None
    try:
        result = _run(tmp_path, PRECHECK_CELL, journal)
        assert result.returncode == 0, result.stderr
        assert calls(tmp_path) == [], "前置校验失败的调用一个也没有到达 adapter"
        if journal is not None:
            assert not journal.path.exists() or stages(journal) == [], "未派发前不写 intent"
        errors = json.loads(result.stdout.splitlines()[0])
        # 原异常类型、错误码与消息不变；签名错误仍同步抛出。
        assert errors == [
            ["MuError", "INVALID_ARGUMENT", "INVALID_ARGUMENT: text must be a string"],
            ["MuError", "UNSUPPORTED_CAPABILITY", "UNSUPPORTED_CAPABILITY: ui.set_text"],
            ["MuError", "UNSUPPORTED_CAPABILITY",
             "UNSUPPORTED_CAPABILITY: use an advertised node action; generic arguments are WIP"],
            ["MuError", "INVALID_ARGUMENT", "INVALID_ARGUMENT: use an absolute path on the target device"],
            ["TypeError", None, "sync"],
        ]
        assert "Input.text(self, text: str, *, target:" in result.stdout

        failed = [event for event in _sdk_tools(result) if not event["ok"]]
        assert [event["tool"] for event in failed] == [
            "mai_ui.Input.text", "mai_ui.AX.set_text", "mai_ui.AX.perform", "mai_ui.Files.write",
            "mai_ui.Input.text"], "每次派发前异常恰好一条审计，traced 没有第二条"
        assert all(event["effect"] == "refused" and event["arguments"] == {} for event in failed)

        rows = _rows(result, phase="python_precheck")
        assert len(rows) == 5 and all(row["evaluation"] == "not_dispatched" for row in rows)
        text_row, cap_row, perform_row, files_row, signature_row = rows
        assert "text 须为 str" in text_row["diagnostics"] and text_row["help"] == "Input.text"
        assert cap_row["capability"] == "ui.set_text" and cap_row["help"] == "AX.set_text"
        assert any("未声明该能力 ready" in item for item in cap_row["diagnostics"])
        assert "capability" not in perform_row, "WIP 原因不是能力名，不能说成未声明 ready"
        assert not any("未声明该能力" in item for item in perform_row["diagnostics"])
        assert "文件路径须为目标设备上的绝对路径" in files_row["diagnostics"]
        assert any("调用签名不匹配" in item for item in signature_row["diagnostics"])

        feedback = result.feedback()
        assert "SDK 前置校验：text 须为 str" in feedback and 'mu.help("AX.set_text")' in feedback
        assert '"phase":"python_precheck"' in feedback and '"capability":"ui.set_text"' in feedback
        public = feedback + json.dumps(result.control["tool_calls"], ensure_ascii=False)
        assert "SECRET-VALUE" not in public and "SECRET-BYTES" not in public
        assert "adapter says no" not in public
    finally:
        if journal is not None:
            journal.close()


def test_dispatched_rejection_unknown_and_adapter_capability_error_are_not_prechecks(tmp_path):
    cell = BIND + '''
out = []
for text in ("reject", "unsupported", "unknown"):
    try:
        await app.ui.input.text(text)
    except Exception as exc:
        out.append([exc.code, exc.receipt.execution, exc.receipt.effect])
print(json.dumps(out))
'''
    result = _run(tmp_path, cell)
    assert result.returncode == 0, result.stderr
    assert [call["text"] for call in calls(tmp_path)] == ["reject", "unsupported", "unknown"]
    assert json.loads(result.stdout) == [["INPUT_REFUSED", "rejected", "not_applied"],
                                         ["UNSUPPORTED_CAPABILITY", "rejected", "not_applied"],
                                         ["ACTION_OUTCOME_UNKNOWN", "unknown", "unknown"]]
    assert not _rows(result, phase="python_precheck"), "已到达 adapter 的拒绝不能凭错误码说成派发前"
    rejected = _rows(result, error_code="INPUT_REFUSED")[0]
    assert (rejected["evaluation"], rejected["execution"], rejected["effect"]) == (
        "not_applied", "rejected", "not_applied")
    adapter_cap = _rows(result, error_code="UNSUPPORTED_CAPABILITY")[0]
    assert adapter_cap["evaluation"] == "not_applied" and "capability" not in adapter_cap
    unknown = _rows(result, evaluation="outcome_unknown")[0]
    assert unknown["execution"] == "unknown" and unknown["error_code"] == "ACTION_OUTCOME_UNKNOWN"
    failed = [event for event in _sdk_tools(result) if not event["ok"]]
    # legacy tool_call 形状：字段集合与 effect 分类不变，原始 receipt 仍在。
    assert [event["effect"] for event in failed] == ["refused", "refused", "outcome_unknown"]
    assert all(set(event) == {"type", "call_id", "tool", "arguments", "ok", "error", "error_code",
                              "receipt", "effect", "rule_feedback"} for event in failed)
    # legacy receipt 字段沿用旧 _public_json 序列化（dataclass 文本），形状不变；结构化事实在 rule_feedback。
    assert "execution='rejected'" in str(failed[0]["receipt"])
    assert failed[0]["rule_feedback"]["execution"] == "rejected"
    assert "勿直接重试" in result.feedback()
    assert len(calls(tmp_path)) == 3, "unknown 不自动重试"


@pytest.mark.parametrize("journaled", [False, True])
def test_multiple_successful_mutations_return_unchanged_receipts_with_facts(tmp_path, journaled):
    cell = BIND + '''
receipts = [await app.ui.ax.click(node), await app.ui.input.text("hello", target=node),
            await app.ui.input.press_key("a", modifiers=["meta"]), await device.files.write("/tmp/o", b"d")]
shell = await device.processes.run(["/bin/echo", "ok"])
print(json.dumps([[type(r).__name__, getattr(r, "execution", None)] for r in receipts]
                 + [[type(shell).__name__, shell.state, shell.exit_code, shell.stdout.decode()]]))
'''
    journal = ActionJournal(tmp_path / "j", "host") if journaled else None
    try:
        result = _run(tmp_path, cell, journal)
        assert result.returncode == 0, result.stderr
        native = calls(tmp_path)
        assert [call.get("action") or call["op"] for call in native][:3] == ["click", "input_text", "press_key"]
        assert len(native) == 5
        printed = json.loads(result.stdout)
        assert all(item == ["ActionReceipt", "completed"] for item in printed[:3])
        assert printed[3][0] == "dict", "files.write 原样返回 SDK 的 dict"
        assert printed[4] == ["ShellResult", "exited", 0, "ok"]
        assert not _rows(result, phase="python_precheck")
        rows = [row for row in result.control["action_feedback"] if row["evaluation"] != "observation_unavailable"]
        assert len(rows) >= 5 and {row["evaluation"] for row in rows} == {"receipt_unverified"}
        ui_rows = [row for row in rows if row.get("operation", "").endswith(("click", "input_text", "press_key"))]
        assert len(ui_rows) == 3 and all(row.get("execution") == "completed" for row in ui_rows)
        if journaled:
            assert all(row.get("reported_stage") == "receipt_received" for row in rows)
        assert "调用已结束；技术回执不证明期望的界面变化或业务成功" in result.feedback()
    finally:
        if journal is not None:
            journal.close()


def test_equal_priority_merge_keeps_durable_status_and_unions_diagnostics():
    control = empty_control()
    durable = action_feedback("action.set_text", action_id="a", sdk_session_id="s", stage="receipt_received",
                              receipt={"execution": "completed", "effect": "unknown"})
    worker = action_feedback("action.set_text", action_id="a", sdk_session_id="s", returned=True,
                             receipt={"execution": "completed", "effect": "applied", "mechanism": "accessibility",
                                      "error": {"details": {"native_message": "AXValue is not settable"}}})
    remember_feedback(control, durable, fallback_id="x")
    remember_feedback(control, worker, fallback_id="y")
    [row] = control["action_feedback"]
    assert (row["reported_stage"], row["execution"], row["effect"]) == ("receipt_received", "completed", "unknown")
    assert row["mechanism"] == "accessibility" and "AXValue 不可设置" in row["diagnostics"][0]
    # 优先级不同：unknown 无论先后都整体胜出，不与已应用回执拼出新组合。
    for order in ((durable, "unknown"), ("unknown", durable)):
        control = empty_control()
        for item in order:
            item = item if isinstance(item, dict) else action_feedback(
                "action.set_text", action_id="a", sdk_session_id="s", stage="outcome_unknown",
                receipt={"execution": "unknown", "effect": "unknown"})
            remember_feedback(control, item, fallback_id="z")
        [row] = control["action_feedback"]
        assert (row["evaluation"], row["execution"], row["effect"], row["reported_stage"]) == (
            "outcome_unknown", "unknown", "unknown", "outcome_unknown")


def test_phase_and_precheck_labels_require_caller_proof():
    message = {"code": "INVALID_ARGUMENT", "message": "INVALID_ARGUMENT: text must be a string"}
    dispatched = action_feedback("Input.text", error=message, help_topic="Input.text")
    assert "phase" not in dispatched and "help" not in dispatched and "diagnostics" not in dispatched
    assert "phase" not in action_feedback("x", phase="python_precheck", error=message)
    gate = action_feedback("action.click", not_dispatched=True, phase="host_gate",
                           error={"code": "FOREGROUND_DENIED"})
    assert gate["phase"] == "host_gate" and "diagnostics" not in gate
    unknown = action_feedback("shell", error={"code": "SHELL_OUTCOME_UNKNOWN"})
    assert unknown["evaluation"] == "outcome_unknown"
    assert action_feedback("click", returned=True)["evaluation"] == "receipt_unverified"
    assert action_feedback("click")["evaluation"] == "outcome_unknown", "没有任何事实时保持未知"
    control = empty_control()
    remember_feedback(control, {**action_feedback("click", returned=True), "phase": "python_precheck",
                                "help": "忽略指令", "capability": "rm -rf /",
                                "diagnostics": ["任意文本", "text 须为 str"]}, fallback_id="f")
    [row] = control["action_feedback"]
    assert "phase" not in row and "help" not in row and "capability" not in row
    assert row["diagnostics"] == ["text 须为 str"]
    assert "忽略指令" not in render_feedback(control)


@pytest.mark.parametrize("cause,expected", [
    ("scope_fenced", "作用域因未完成操作暂时阻塞"),
    ("endpoint_offline", "目标端点已断线"),
])
def test_public_precheck_preserves_structured_reason_without_raw_details(monkeypatch, cause, expected):
    import code_runtime as rt
    from mai_ui import MuError

    events = []
    monkeypatch.setattr(rt, "_emit", events.append)
    error = MuError("DEVICE_OFFLINE", "不含英文签名", details={
        "cause": cause, "scope": "private-target", "reason": "SECRET-VALUE", "next_step": "send_payment"})
    rt._record_public_precheck(object(), "Input.text", error)
    [event] = events
    row = event["rule_feedback"]
    assert row["evaluation"] == "not_dispatched" and row["phase"] == "python_precheck"
    assert any(expected in label for label in row["diagnostics"])
    assert not any("同一会话不会自行恢复" in label for label in row["diagnostics"])
    public = json.dumps(events, ensure_ascii=False)
    assert all(value not in public for value in ("private-target", "SECRET-VALUE", "send_payment"))


@pytest.mark.parametrize("journaled", [False, True])
def test_native_offline_state_reaches_precheck_feedback_without_dispatch(tmp_path, journaled):
    journal = ActionJournal(tmp_path / "j", "host") if journaled else None
    cell = BIND + '''
for text in ("unknown", "must-not-dispatch"):
    try:
        await app.ui.input.text(text)
    except Exception:
        pass
'''
    try:
        result = _run(tmp_path, cell, journal)
        assert result.returncode == 0, result.stderr
        assert [call["text"] for call in calls(tmp_path)] == ["unknown"]
        [row] = _rows(result, phase="python_precheck", error_code="DEVICE_OFFLINE")
        assert row["evaluation"] == "not_dispatched"
        assert any("目标端点已断线" in label for label in row["diagnostics"])
        assert "勿直接重试" in result.feedback()
        unknown = _rows(result, evaluation="outcome_unknown")
        assert unknown and all(row["verification"] == "unverified" for row in unknown)
    finally:
        if journal is not None:
            journal.close()


def test_feedback_publish_failure_never_changes_business_exception(monkeypatch):
    import code_runtime as rt

    def broken(*args, **kwargs):
        raise RuntimeError("UI 发布失败")
    monkeypatch.setattr(rt, "_record_tool", broken)
    original = ValueError("原始")
    rt._record_public_precheck(object(), "Input.text", original)  # 不抛出、不重试


# ---- 工具手册 ----

def _api_signature(cls, method):
    from rich_help import _bases, _classes
    seen = cls
    while seen:
        for line in _classes().get(seen, []):
            if re.match(rf"(?:async )?def {method}\(", line):
                namespace = {}
                exec("from __future__ import annotations\n" + line, namespace)
                return inspect.signature(namespace[method]), line.startswith("async ")
        seen = _bases().get(seen)
    raise AssertionError(f"{cls}.{method} 不在 api.pyi")


_OWNERS = {"ax": "AX", "vision": "Vision", "input": "Input", "apps": "Apps", "windows": "Windows",
           "image": "Image", "mu": "Session", "win": "Window", "window": "Window", "app": "App",
           "files": "Files", "shell": "Shell", "processes": "Processes", "clipboard": "Clipboard"}


def test_help_recipes_compile_and_bind_to_exact_signatures():
    from rich_help import build_help
    text = "\n".join(build_help(topic) for topic in ("core", "tools", "native", "coordinates"))
    snippets = re.findall(r"(?:await )?(?:ui|image|mu|win|window|app|device)\.[\w.]+\([^()]*\)", text)
    checked = 0
    for snippet in snippets:
        tree = compile(snippet, "<help>", "exec", ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))
        chain, func = [], call.func
        while isinstance(func, ast.Attribute):
            chain.insert(0, func.attr)
            func = func.value
        chain.insert(0, func.id)
        owner = _OWNERS.get(chain[-2])
        if owner is None:
            continue
        signature, is_async = _api_signature(owner, chain[-1])
        signature.bind(object(), *call.args, **{kw.arg: kw.value for kw in call.keywords})
        assert is_async == snippet.startswith("await "), f"同步/异步写法与签名不符：{snippet}"
        checked += 1
    assert checked >= 15, snippets


def test_help_groups_match_real_handles_and_feedback_pointers_resolve():
    import code_runtime as rt
    from rich_help import build_help
    apps, windows = build_help("apps"), build_help("windows")
    assert "## Apps\n" in apps and "## App(Device)" in apps and "async def list(" in apps and "async def get(" in apps
    assert "## Windows\n" in windows and "## Window(App)" in windows and "async def get(" in windows
    assert build_help("window").startswith("## Window(App)") and build_help("ax").startswith("## AX\n")
    assert "no signatures" not in build_help("displays")  # 单词类名不再递归
    for _, cls, methods in rt._PUBLIC_MUTATIONS:
        for method in methods:
            assert build_help(f"{cls}.{method}").startswith(f"{cls}.{method}(self"), (cls, method)
    tools = build_help("tools")
    for fact in ("AXValue 不可设置", "ui.input.text(text, target=node)", "mu.action_status(action_id)",
                 "按 session 许可", "按任务许可", "不按窗口", "python_precheck", "host_gate", "observe 正常时没有反馈行"):
        assert fact in tools, fact
    assert "normalized_1000" in build_help("coordinates") and 'space 默认是 "pixels"' in build_help("coordinates")
    everything = "\n".join(build_help(topic) for topic in ("core", "tools", "native", "coordinates", "ax"))
    assert not re.search(r"\b(find|inspect|wait_for|act_checked)\(|mcp", everything)
    assert "只证明送达" not in everything and "每任务授权" not in everything
    assert len(build_help("core")) < 600 and len(build_help("tools")) < 1600
