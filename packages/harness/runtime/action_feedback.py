"""宿主的有界事实反馈规则；不新增模型 helper、不观察桌面、不决定业务成功。"""
from __future__ import annotations

import json
import math
import re

FEEDBACK_LIMIT = 64
_TOKEN = re.compile(r"[A-Za-z0-9_.:/-]{1,160}\Z")
# 原生文字可能含输入正文或不可信指令；只输出匹配到的固定诊断，完整文字仍留私有日志。
_DIAGNOSTICS = (
    ("axvalue is not settable", "原生报告 AXValue 不可设置：只说明 ax.set_text 写值路径不可用，"
                                "不代表控件不能输入，也不证明是静态文本"),
    ("same_pid_keyboard_ambiguity", "同进程多个窗口导致键盘目标歧义"),
    ("element_outside_target_window", "元素不在绑定的目标窗口内"),
    ("no longer frontmost", "目标应用或窗口已不在前台"),
    ("ax_window_unresolved", "原生 AX 窗口映射未解析"),
)
_LABELS = {
    "not_dispatched": "本次调用在派发前被拒绝，未执行",
    "not_applied": "SDK 回执明确报告未应用；不据此推断失败发生在派发前",
    "outcome_unknown": "派发结果未知，可能已有副作用；先查原 action_id 的状态并观察，勿直接重试",
    "execution_failed": "调用报告失败；仅依据回执，不能确定业务结果",
    "suspected_noop": "SDK 报告疑似无效操作；尚不能确定原因或断言完全未执行",
    "partial": "SDK 报告部分效果；业务结果仍未核验",
    "receipt_unverified": "调用已结束；技术回执不证明期望的界面变化或业务成功",
    "observation_unavailable": "观察不完整或不可用；不能据此证明目标不存在或唯一",
}
_PRIORITY = {"outcome_unknown": 0, "execution_failed": 1, "not_dispatched": 1,
             "not_applied": 1, "suspected_noop": 1, "partial": 1,
             "observation_unavailable": 1, "receipt_unverified": 2}
# SDK 本地前置校验的固定英文消息 → 可读原因；只在已证明未进入 session 请求时匹配，只输出标签。
_PRECHECKS = (
    ("expected an ax node from this ui scope", "目标须是同一 ui.ax.observe() 返回的 AXNode 或其 index"),
    ("belongs to a different ui scope", "节点/截图/点属于另一个 UI 作用域；在同一 win.ui 上重新观察"),
    ("already contains its image basis", "PointRef 已含截图依据，不能再传 y 或 image"),
    ("a node target takes no y coordinate", "节点目标不接受 y 坐标"),
    ("emit one image in a previous cell", "没有唯一已交付截图：先在前一 cell emit 一张截图，或显式传 image"),
    ("image id is not in the delivered view", "image ID 不在已交付视图中；重新观察并 emit"),
    ("node has no bounds", "节点没有 bounds，无法映射坐标；改用 image.point()"),
    ("image carries no window geometry", "截图缺少窗口几何，不能由节点换算坐标；改用 image.point()"),
    ("outside the image", "坐标落在截图外（目标可能已滚出视图）；滚动后重新观察"),
    ("provide numeric coordinates", "坐标须为数字"),
    ("unknown delivery policy", "delivery 只能是 auto/background/foreground"),
    ("background input is unavailable", "该平台不支持后台投递"),
    ("text must be a string", "text 须为 str"),
    ("provide a key and modifier names", "press_key 需要非空 key，modifiers 须为字符串列表"),
    ("timeout_ms must be an integer in 1..120000", "timeout_ms 须为 1..120000 的整数"),
    ("use an advertised node action", "perform 只接受该节点 actions 列出的动作，且不支持 args"),
    ("viewport scrolling supports an amount", "AX viewport 滚动的 amount 须在 (0, 1]"),
    ("selection requires a readable code-point range", "select_text 需要可读文本长度内的整数范围"),
    ("unknown direction", "direction 只能是 up/down/left/right"),
    ("two points in the same capture", "两个点须来自同一张截图，且 duration_ms 有界"),
    ("same captured image", "两个点须来自同一张截图"),
    ("duration must", "duration_ms 须在 50..5000 且小于 timeout"),
    ("timeout must allow the target focus wait", "带 target 的输入 timeout 须大于 500ms"),
    ("bind a window before changing its frame", "set_frame 需要已绑定窗口的句柄"),
    ("bind an app and supply a nonempty menu path", "invoke_menu 需要已绑定 App 和非空字符串路径"),
    ("unknown android system button", "Android 系统按钮名无效"),
    ("use an absolute path on the target device", "文件路径须为目标设备上的绝对路径"),
    ("argv must be a nonempty sequence of strings", "argv 须为非空字符串列表"),
    ("write accepts up to 1 mib of bytes", "files.write 的 data 须为≤ 1 MiB 的 bytes，overwrite 须为 bool"),
    ("stdin must be bytes", "stdin 须为 bytes"),
    ("requires a whole-device attachment", "该服务只能在整机 device 上调用，不能在 app/window 句柄上调用"),
)
_CONNECTION_HINTS = {
    "scope_fenced": "SDK 报告目标作用域因未完成操作暂时阻塞；等待原操作结束后重新观察，"
                    "不代表整个设备断线；勿直接重试有副作用动作",
    "endpoint_offline": "SDK 报告目标端点已断线；由宿主恢复连接后重新获取句柄并观察，"
                        "勿直接重试结果未知动作",
}
_CODE_HINTS = {
    "DEVICE_OFFLINE": "SDK 报告连接或作用域不可用；原因未明确，不能据此断言永久断线或动作未执行",
    "STALE_ATTACHMENT": "句柄所属 attachment 已失效；重新获取 app/window",
    "SESSION_CLOSED": "SDK 会话已关闭",
    "UNSUPPORTED_CAPABILITY": "当前绑定目标未声明该能力 ready；用 await win.capabilities() 查看",
}
# 仅当调用方证明 TypeError 来自参数绑定（协程尚未创建）时使用；协程内部的 TypeError 不套此解释。
_SIGNATURE_HINT = "Python 调用签名不匹配（参数名、个数或位置/关键字）"
_PHASES = {"python_precheck", "host_gate"}
_UNKNOWN_CODES = {"ACTION_OUTCOME_UNKNOWN", "SHELL_OUTCOME_UNKNOWN"}
_NATIVE_LABELS = frozenset(item[1] for item in _DIAGNOSTICS)
_PRECHECK_LABELS = frozenset(item[1] for item in _PRECHECKS)
_HINT_LABELS = frozenset((*_CODE_HINTS.values(), *_CONNECTION_HINTS.values(), _SIGNATURE_HINT))
_ALL_LABELS = _NATIVE_LABELS | _PRECHECK_LABELS | _HINT_LABELS
_CAPABILITY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\Z")
_HELP = re.compile(r"[A-Z][A-Za-z]{1,20}\.[a-z_]{1,32}\Z")
_LIST_FIELDS = ("diagnostics", "evidence_kinds", "error_codes")
# 分类权威字段只来自同一条胜出事实，绝不把两条回执拼成新的 execution/effect/stage 组合。
_AUTHORITY = ("evaluation", "summary", "reported_stage", "execution", "effect", "phase")


def _token(value):
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _connection_hint(*errors):
    # 只认 SDK 错误码和精确 cause；不回显自由文本 scope/reason/next_step，不触发恢复或重试。
    for error in errors:
        error = _dict(error)
        cause = _dict(error.get("details")).get("cause")
        if error.get("code") == "DEVICE_OFFLINE" and isinstance(cause, str):
            hint = _CONNECTION_HINTS.get(cause)
            if hint:
                return hint
    return None


def _diagnostics(*errors, table=_DIAGNOSTICS):
    found = []
    for error in errors:
        error = _dict(error)
        details = _dict(error.get("details"))
        texts = [error.get("message"), details.get("native_message")]
        for value in texts:
            if not isinstance(value, str):
                continue
            lower = value[:4096].lower()
            for signature, label in table:
                if signature in lower and label not in found:
                    found.append(label)
    return found


def action_feedback(operation, *, action_id=None, sdk_session_id=None, stage=None,
                    receipt=None, raw_result=None, error=None, not_dispatched=False,
                    returned=False, phase=None, help_topic=None):
    """只读 SDK 的结构化事实，不从异常名称、页面不变或 role 推断动作效果。

    returned=True 只表示 SDK 调用正常返回；phase 只由调用方在可证明的派发前阶段提供。
    """
    receipt, result, error = _dict(receipt), _dict(raw_result), _dict(error)
    facts = {**result, **receipt}
    execution, effect = _token(facts.get("execution")), _token(facts.get("effect"))
    native_error = _dict(facts.get("error"))
    code = _token(error.get("code")) or _token(native_error.get("code"))
    if not_dispatched or stage in {"intent_recorded", "cancelled_before_dispatch"}:
        evaluation = "not_dispatched"
    elif (stage == "outcome_unknown" or execution == "unknown"
          or (execution is None and code in _UNKNOWN_CODES)):
        evaluation = "outcome_unknown"
    elif effect == "not_applied":
        evaluation = "not_applied"
    elif execution in {"rejected", "failed", "cancelled_before_dispatch"} or code:
        evaluation = "execution_failed"
    elif effect == "suspected_noop":
        evaluation = "suspected_noop"
    elif effect == "partial":
        evaluation = "partial"
    elif execution == "completed" or stage == "receipt_received" or returned:
        evaluation = "receipt_unverified"
    else:
        evaluation = "outcome_unknown"
    row = {"rule_version": 1, "operation": _token(operation) or "sdk.action",
           "evaluation": evaluation, "summary": _LABELS[evaluation],
           "verification": "unverified", "condition": "not_checked"}
    for key, value in (("action_id", action_id or facts.get("action_id")),
                       ("sdk_session_id", sdk_session_id or facts.get("session_id")),
                       ("reported_stage", stage), ("execution", execution), ("effect", effect),
                       ("error_code", code), ("native_error_code", native_error.get("code")),
                       ("mechanism", facts.get("mechanism")),
                       ("observation_id", facts.get("based_on"))):
        if (value := _token(value)) is not None:
            row[key] = value
    duration = facts.get("duration_ms")
    if type(duration) in (int, float) and 0 <= duration <= 86400000 and math.isfinite(duration):
        row["duration_ms"] = duration
    if evaluation == "not_dispatched" and phase in _PHASES:
        row["phase"] = phase
    diagnostics = _diagnostics(native_error, error)
    connection_hint = _connection_hint(error, native_error)
    if connection_hint:
        diagnostics.append(connection_hint)
    if row.get("phase") == "python_precheck":
        # 只有可证明未进入 SDK session 请求的本地校验异常才匹配 SDK 固定消息与错误码提示。
        diagnostics += [label for label in _diagnostics(error, table=_PRECHECKS) if label not in diagnostics]
        message = error.get("message")
        tail = message.split(": ", 1)[-1] if code == "UNSUPPORTED_CAPABILITY" and isinstance(message, str) else None
        if tail and _CAPABILITY.fullmatch(tail):
            row["capability"] = tail
        # UNSUPPORTED_CAPABILITY 也用于 WIP/整机限制等固定原因；只有消息就是能力名时才提示“未声明 ready”。
        hint = (_SIGNATURE_HINT if error.get("signature_mismatch") is True
                else None if connection_hint or (code == "UNSUPPORTED_CAPABILITY" and "capability" not in row)
                else _CODE_HINTS.get(code))
        if hint and hint not in diagnostics:
            diagnostics.append(hint)
        if isinstance(help_topic, str) and _HELP.fullmatch(help_topic):
            row["help"] = help_topic
    if diagnostics:
        row["diagnostics"] = diagnostics[:8]
    # 只列证据类型，不传播 observed/正文/target token；这些类型本身也不升级 verification。
    evidence = facts.get("evidence")
    if isinstance(evidence, list):
        known = {"exact_window_activation", "native_observation", "device_receipt"}
        kinds = sorted({item["kind"] for item in evidence[:64]
                        if isinstance(item, dict) and isinstance(item.get("kind"), str)
                        and item["kind"] in known})
        if kinds:
            row["evidence_kinds"] = kinds
    return row


def observation_feedback(operation, result):
    """只依据原始观察的完整性字段；不会因空树就自动转视觉或报告设备离线。"""
    result = _dict(result)
    components = result.get("components")
    components = components if isinstance(components, list) else []
    incomplete = [item for item in components if isinstance(item, dict)
                  and (item.get("status") not in (None, "ok") or item.get("ax_complete") is False)]
    if result.get("truncated") is not True and not incomplete and result.get("complete") is not False:
        return None
    row = {"rule_version": 1, "operation": _token(operation) or "sdk.observe",
           "evaluation": "observation_unavailable", "summary": _LABELS["observation_unavailable"],
           "verification": "unverified", "condition": "not_checked",
           "incomplete_components": len(incomplete), "truncated": result.get("truncated") is True}
    codes = sorted({code for item in incomplete if (code := _token(item.get("error")))})
    if codes:
        row["error_codes"] = codes[:8]
    diagnostics = _diagnostics(*({"message": item.get("message") or item.get("reason")}
                                for item in incomplete[:64]))
    if diagnostics:
        row["diagnostics"] = diagnostics
    return row


def _merge(previous, current):
    """同一动作的多条事实合并：优先级高者整体提供分类权威字段，另一条只补元数据与诊断。"""
    new_rank, old_rank = _PRIORITY[current["evaluation"]], _PRIORITY[previous["evaluation"]]
    if new_rank != old_rank:
        winner, other = (current, previous) if new_rank < old_rank else (previous, current)
    elif "reported_stage" in previous and "reported_stage" not in current:
        # 同优先级时宿主 durable status（带 reported_stage）是权威，后到的 worker 诊断不能把它冲掉。
        winner, other = previous, current
    else:
        winner, other = current, previous
    merged = {key: value for key, value in other.items() if key not in _AUTHORITY}
    merged.update(winner)
    for key in _LIST_FIELDS:
        values = [item for source in (winner, other) for item in source.get(key) or ()]
        if values:
            merged[key] = list(dict.fromkeys(values))[:8]
    return merged


def remember_feedback(control, row, *, fallback_id):
    """每个 cell 只保留最多 64 条；同动作的 audit/receipt 合并去重，优先保留问题事实。"""
    if (not isinstance(row, dict) or type(row.get("rule_version")) is not int or row["rule_version"] != 1
            or not isinstance(row.get("evaluation"), str) or row["evaluation"] not in _LABELS):
        return
    safe = {key: value for key in ("operation", "action_id", "sdk_session_id", "reported_stage", "execution",
                                   "effect", "error_code", "native_error_code", "mechanism", "observation_id")
            if (value := _token(row.get(key))) is not None}
    safe.update(rule_version=1, evaluation=row["evaluation"], summary=_LABELS[row["evaluation"]],
                verification="unverified", condition="not_checked")
    duration = row.get("duration_ms")
    if type(duration) in (int, float) and 0 <= duration <= 86400000 and math.isfinite(duration):
        safe["duration_ms"] = duration
    if row.get("phase") in _PHASES and row["evaluation"] == "not_dispatched":
        safe["phase"] = row["phase"]
    for key, pattern in (("capability", _CAPABILITY), ("help", _HELP)):
        if isinstance(row.get(key), str) and pattern.fullmatch(row[key]):
            safe[key] = row[key]
    if isinstance(row.get("diagnostics"), list):
        safe["diagnostics"] = [item for item in row["diagnostics"][:8] if isinstance(item, str) and item in _ALL_LABELS]
    if isinstance(row.get("evidence_kinds"), list):
        safe["evidence_kinds"] = [item for item in row["evidence_kinds"][:8] if isinstance(item, str)
                                  and item in {"exact_window_activation", "native_observation", "device_receipt"}]
    if isinstance(row.get("error_codes"), list):
        safe["error_codes"] = [code for item in row["error_codes"][:8] if (code := _token(item))]
    count = row.get("incomplete_components")
    if type(count) is int and count >= 0:
        safe.update(incomplete_components=count, truncated=row.get("truncated") is True)
    rows = control.setdefault("action_feedback", [])
    identity = (safe.get("sdk_session_id"), safe.get("action_id") or _token(fallback_id))
    stored = {**safe, "feedback_id": str(identity)}
    for index, previous in enumerate(rows):
        if previous.get("feedback_id") == stored["feedback_id"]:
            # 不让较弱事实覆写 unknown/拒绝；同级时保留 durable status，诊断取并集。
            rows[index] = _merge(previous, stored)
            return
    rows.append(stored)
    if len(rows) > FEEDBACK_LIMIT:
        worst = max(range(len(rows)), key=lambda index: (_PRIORITY[rows[index]["evaluation"]], -index))
        rows.pop(worst)
        control["action_feedback_omitted"] = control.get("action_feedback_omitted", 0) + 1


def render_feedback(control, limit=6000):
    rows = control.get("action_feedback") or []
    if not rows:
        return ""
    budget = max(200, min(limit, 6000))
    lines = ["[自动动作事实反馈：宿主规则，仅陈述回执/观察；condition=not_checked，不代表业务核验]"]
    omitted = control.get("action_feedback_omitted", 0)
    used = len(lines[0])
    for row in sorted(rows, key=lambda item: _PRIORITY.get(item.get("evaluation"), 3)):
        # 这里只渲染规则允许的字段，不渲染原始 arguments/request/结果或自由文本诊断。
        safe = {key: value for key in ("operation", "action_id", "reported_stage", "execution", "effect",
                                      "error_code", "native_error_code", "phase", "capability", "mechanism",
                                      "duration_ms", "observation_id")
                if (value := row.get(key)) is not None}
        line = json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + " — " + _LABELS[row["evaluation"]]
        for label in row.get("diagnostics", []):
            if label in _NATIVE_LABELS:
                line += "；原生诊断：" + label
            elif label in _PRECHECK_LABELS:
                line += "；SDK 前置校验：" + label
            elif label in _HINT_LABELS:
                line += "；提示：" + label
        if row.get("help"):
            line += f'；精确签名：mu.help("{row["help"]}")'
        if row.get("evidence_kinds"):
            line += "；证据仅为 " + ",".join(row["evidence_kinds"])
        if row["evaluation"] == "observation_unavailable":
            line += f"；不完整窗口数={row.get('incomplete_components', 0)}；truncated={row.get('truncated', False)}"
            if row.get("error_codes"):
                line += "；观察错误=" + ",".join(row["error_codes"])
        if used + len(line) + 1 > budget - 60:
            omitted += 1
            continue
        lines.append(line)
        used += len(line) + 1
    if omitted:
        lines.append(f"另有 {omitted} 条事实未列出；未列出不代表已成功。")
    return "\n".join(lines)
