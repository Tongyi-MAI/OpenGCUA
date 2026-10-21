"""宿主的有界事实反馈规则；不新增模型 helper、不观察桌面、不决定业务成功。"""
from __future__ import annotations

import json
import math
import re

FEEDBACK_LIMIT = 64
_TOKEN = re.compile(r"[A-Za-z0-9_.:/-]{1,160}\Z")
# 原生文字可能含输入正文或不可信指令；只输出匹配到的固定诊断，完整文字仍留私有日志。
_DIAGNOSTICS = (
    ("axvalue is not settable", "AXValue 不可直接写入；可读文本不等于输入控件"),
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


def _token(value):
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _diagnostics(*errors):
    found = []
    for error in errors:
        error = _dict(error)
        details = _dict(error.get("details"))
        texts = [error.get("message"), details.get("native_message")]
        for value in texts:
            if not isinstance(value, str):
                continue
            lower = value[:4096].lower()
            for signature, label in _DIAGNOSTICS:
                if signature in lower and label not in found:
                    found.append(label)
    return found


def action_feedback(operation, *, action_id=None, sdk_session_id=None, stage=None,
                    receipt=None, raw_result=None, error=None, not_dispatched=False):
    """只读 SDK 的结构化事实，不从异常名称、页面不变或 role 推断动作效果。"""
    receipt, result, error = _dict(receipt), _dict(raw_result), _dict(error)
    facts = {**result, **receipt}
    execution, effect = _token(facts.get("execution")), _token(facts.get("effect"))
    native_error = _dict(facts.get("error"))
    code = _token(error.get("code")) or _token(native_error.get("code"))
    if not_dispatched or stage in {"intent_recorded", "cancelled_before_dispatch"}:
        evaluation = "not_dispatched"
    elif stage == "outcome_unknown" or execution == "unknown":
        evaluation = "outcome_unknown"
    elif effect == "not_applied":
        evaluation = "not_applied"
    elif execution in {"rejected", "failed", "cancelled_before_dispatch"} or code:
        evaluation = "execution_failed"
    elif effect == "suspected_noop":
        evaluation = "suspected_noop"
    elif effect == "partial":
        evaluation = "partial"
    elif execution == "completed" or stage == "receipt_received":
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
    diagnostics = _diagnostics(native_error, error)
    if diagnostics:
        row["diagnostics"] = diagnostics
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


def remember_feedback(control, row, *, fallback_id):
    """每个 cell 只保留最多 64 条；同动作的 audit/receipt 去重，优先保留问题事实。"""
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
    labels = {item[1] for item in _DIAGNOSTICS}
    if isinstance(row.get("diagnostics"), list):
        safe["diagnostics"] = [item for item in row["diagnostics"][:8] if isinstance(item, str) and item in labels]
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
            # 宿主 durable status 优先于 worker audit 的较少信息；不让较弱事实覆写 unknown/拒绝。
            if _PRIORITY[row["evaluation"]] <= _PRIORITY[previous["evaluation"]]:
                rows[index] = stored
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
                                      "error_code", "native_error_code", "mechanism", "duration_ms", "observation_id")
                if (value := row.get(key)) is not None}
        line = json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + " — " + _LABELS[row["evaluation"]]
        for label in row.get("diagnostics", []):
            if label in {item[1] for item in _DIAGNOSTICS}:
                line += "；原生诊断：" + label
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
