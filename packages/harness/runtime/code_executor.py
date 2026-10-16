"""串行执行 Python 轮次，并管理仅在内存中保留对象的常驻会话。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import signal
import socket
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CTRL_PREFIX = "__MAC_AGENT_CTRL__ "
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_CONTROL_BYTES = 8 * 1024 * 1024
OUTPUT_LIMITS = {"stdout": 256 * 1024, "stderr": 64 * 1024}
BLOCK_PROGRESS_FIELDS = (
    "total_blocks", "started_blocks", "completed_blocks", "stopped_block", "stop_reason",
)
RESET_NOTICE = (
    "Python 会话已重置：此前的变量、imports、cwd 和活对象均已丢失，"
    "不会从磁盘恢复。必须重新观察当前界面并重建所需引用；"
    "此前 UI 操作可能已经生效，不能盲目重试 UI 操作。"
)


@dataclass
class CodeExecutionResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool
    control: dict[str, Any]
    session_reset: bool = False

    def presentation(self, limit: int = 12_000) -> dict:
        """有序展示计划；图片仅占位，由调用者按本轮截图序号原位绑定。"""
        parts: list[dict[str, Any]] = []

        def text(value: str) -> None:
            parts.append({"type": "text", "text": value})

        if self.session_reset:
            text(RESET_NOTICE)
        multi_block = self.control.get("total_blocks", 0) > 1
        if multi_block:
            total = self.control["total_blocks"]
            started = self.control.get("started_blocks", 0)
            completed = self.control.get("completed_blocks", 0)
            progress = f"代码块：已完成 {completed}/{total}；已开始 {started}/{total}；未开始 {total - started}"
            stopped = self.control.get("stopped_block")
            reason = self.control.get("stop_reason")
            if stopped is not None:
                progress += f"；停止于第 {stopped} 块（{reason or 'interrupted'}）"
            elif reason:
                progress += f"；整轮中止（{reason}）"
            text(progress)
        displays = [event for event in self.control.get("events", [])
                    if event.get("type") in {"display", "screenshot"}]
        if not displays:
            displays = [
                *({**event, "type": "display"} for event in self.control.get("displays", [])),
                *({**event, "type": "screenshot"} for event in self.control.get("screenshots", [])),
            ]
        # 新事件保持 runtime 编号；老事件独立成组，不与任何已知编号碰撞。
        reserved = {event["output_id"] for event in displays
                    if type(event.get("output_id")) is int and event["output_id"] > 0}
        numbered: list[tuple[int, dict[str, Any]]] = []
        legacy_id = 1
        for event in displays:
            output_id = event.get("output_id")
            if type(output_id) is not int or output_id <= 0:
                while legacy_id in reserved:
                    legacy_id += 1
                output_id = legacy_id
                reserved.add(output_id)
                legacy_id += 1
            numbered.append((output_id, event))
        counts: dict[int, int] = {}
        for output_id, _ in numbered:
            counts[output_id] = counts.get(output_id, 0) + 1
        children: dict[int, int] = {}
        current_id = None
        screenshot_index = 0
        for output_id, event in numbered:
            source = event.get("source")
            source = _presentation_excerpt(source, 1000, "...[source excerpt; truncated]") if isinstance(source, str) else None
            location = _presentation_excerpt(str(event.get("source_location") or "unavailable"), 512, "...[location truncated]")
            value_type = _display_type(event)
            root_type = _presentation_excerpt(str(event.get("root_type") or value_type), 128, "...[type truncated]")
            if current_id != output_id:
                origin = source if source is not None else "(unavailable)"
                if source is None and location != "unavailable":
                    origin += f"\nlocation: {location}"
                if multi_block:
                    origin += f"\nblock_index: {event.get('block_index') or '(unavailable)'}"
                text(f"## Output {output_id} · {root_type}\nsource: {origin}")
                current_id = output_id
            children[output_id] = children.get(output_id, 0) + 1
            label = event.get("label")
            label = _presentation_excerpt(str(label), 256, "...[path truncated]") if label is not None else None
            heading = f"### {output_id}.{children[output_id]} · {value_type} · {label or '$'}"
            if event.get("type") == "screenshot":
                parts.append({
                    "type": "image", "screenshot_index": screenshot_index,
                    "metadata": {**{key: event.get(key) for key in (
                        "width", "height", "coordinate_space", "target"
                    )}, "label": label},
                    "output_id": output_id, "source": source,
                    "source_location": location,
                    **({"block_index": event.get("block_index")} if multi_block else {}),
                    "heading": heading if label is not None or counts[output_id] > 1 else "",
                })
                screenshot_index += 1
            else:
                body = render_display(event, limit, include_label=False)
                text(heading + "\n" + body if label is not None or counts[output_id] > 1 else body)
        errors = [value for index, call in enumerate(self.control.get("tool_calls") or [], start=1)
                  if (value := render_tool_call(index, call, limit))]
        if errors:
            text("Automatic tool errors:\n" + "\n".join(errors))
        if self.stdout:
            text("stdout:\n" + compact_text(self.stdout, limit))
        if self.stderr:
            text("stderr:\n" + compact_text(self.stderr, limit))
        if not displays and not errors and not self.stdout and not self.stderr:
            text("(no output) 调用 emit(value) 或 print(value) 显式输出；无输出不代表任务成功。")
        return {
            "status": {"returncode": self.returncode, "timed_out": self.timed_out,
                       "session_reset": self.session_reset,
                       **({key: self.control.get(key) for key in BLOCK_PROGRESS_FIELDS} if multi_block else {})},
            "parts": parts,
        }

    def feedback(self, limit: int = 12_000) -> str:
        presentation = self.presentation(limit)
        status = presentation["status"]
        parts = [
            f"Previous execution: returncode={status['returncode']}; "
            f"timed_out={str(status['timed_out']).lower()}; "
            f"session_reset={str(status['session_reset']).lower()}"
        ]
        for part in presentation["parts"]:
            if part["type"] == "text":
                parts.append(part["text"])
            else:
                parts.append((part["heading"] + "\n" if part["heading"] else "") + "screenshot: " +
                             compact_text(_compact_json(part["metadata"]), limit))
        return "\n\n".join(parts)


def empty_control() -> dict[str, Any]:
    return {
        "events": [],
        "screenshot": False,
        "screenshots": [],
        "tool_calls": [],
        "displays": [],
        "terminate": None,
        "ask_user": None,
        "todo": [],
        "completed": False,
        "exit_code": None,
        "total_blocks": 0,
        "started_blocks": 0,
        "completed_blocks": 0,
        "stopped_block": None,
        "stop_reason": None,
    }


def compact_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    head = max(1, limit // 3)
    tail = max(1, limit - head)
    return value[:head] + "\n...[text compacted]...\n" + value[-tail:]


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _render_items(items: list, limit: int, indent: str) -> list[str]:
    """列表逐条一行并带原序号（与代码 enumerate/下标一致）；超长时按条目边界保留首尾，绝不拦腰截断单条 JSON。"""
    rendered = [compact_text(_compact_json(item), max(300, limit // 8)) for item in items]
    lined = [f"[{i}] {text}" for i, text in enumerate(rendered)]
    if sum(len(text) for text in lined) + len(lined) * (len(indent) + 2) <= limit:
        return [f"{indent}- {text}" for text in lined]
    head: list[str] = []
    used = 0
    for text in lined:
        if head and used + len(text) > limit // 2:
            break
        head.append(text)
        used += len(text)
    tail: list[str] = []
    used = 0
    for text in reversed(lined[len(head):]):
        if tail and used + len(text) > limit // 2:
            break
        tail.insert(0, text)
        used += len(text)
    omitted = len(lined) - len(head) - len(tail)
    lines = [f"{indent}- {text}" for text in head]
    if omitted > 0:
        lines.append(
            f"{indent}...[中间省略 {omitted} 条；本次调用的返回值含全部 {len(items)} 项，"
            f"赋值给变量后可在代码中过滤/索引/切片获取]..."
        )
    lines.extend(f"{indent}- {text}" for text in tail)
    return lines


def _render_result_value(value: Any, limit: int, indent: str = "  ") -> list[str]:
    """结果渲染：短值单行紧凑 JSON；长列表逐条一行；长 dict 逐键展开。"""
    compact = _compact_json(value)
    if isinstance(value, list) and len(value) > 1 and len(compact) > 240:
        return [f"{indent}result: [{len(value)} 项]"] + _render_items(value, limit, indent + "  ")
    if isinstance(value, dict) and len(compact) > 240:
        lines = [f"{indent}result:"]
        for key, item in value.items():
            item_compact = _compact_json(item)
            if isinstance(item, list) and len(item) > 1 and len(item_compact) > 240:
                lines.append(f"{indent}  {key}: [{len(item)} 项]")
                lines.extend(_render_items(item, limit, indent + "    "))
            else:
                lines.append(f"{indent}  {key}: {compact_text(item_compact, limit)}")
        return lines
    return [f"{indent}result: {compact_text(compact, limit)}"]


def _presentation_excerpt(text: str, limit: int, marker: str) -> str:
    return text if len(text) <= limit else text[:limit - len(marker)] + marker


def _display_type(event: dict[str, Any]) -> str:
    value_type = event.get("value_type")
    if not value_type:
        if event.get("type") == "screenshot":
            value_type = "Screenshot"
        else:
            value_type = {"ax": "AX", "text": "str", "bash": "BashResult"}.get(event.get("kind"))
            if not value_type:
                value_type = type(event["value"]).__name__ if "value" in event else "unknown"
    return _presentation_excerpt(str(value_type), 128, "...[type truncated]")


def _capability_equal(left: Any, right: Any) -> bool:
    """公共项必须递归同类型同值，不能把 True、1、1.0 合并。"""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return ({(type(k), k) for k in left} == {(type(k), k) for k in right}
                and all(_capability_equal(v, right[k]) for k, v in left.items()))
    if isinstance(left, list):
        return len(left) == len(right) and all(_capability_equal(a, b) for a, b in zip(left, right))
    return left == right


def _capability_common(items: list[dict]) -> dict:
    if len(items) < 2:
        return {}
    return {key: value for key, value in items[0].items()
            if all(key in item and _capability_equal(value, item[key]) for item in items[1:])}


def _capability_shape(value: Any) -> bool:
    return (type(value) is dict and {"state", "provider", "reason", "limits"} <= value.keys()
            and type(value["state"]) is str
            and value["state"] in ("ready", "needs_user_action", "unsupported")
            and type(value["provider"]) is str
            and (value["reason"] is None or type(value["reason"]) is str)
            and (value["limits"] is None or (type(value["limits"]) is dict
                 and all(type(key) is str for key in value["limits"]))))


def _validated_capability_views(value: Any, annotations: Any) -> dict | None:
    """只校验显式标记；未知、冲突或损坏标记使整个 event 回退旧渲染。"""
    if type(annotations) is not list or not annotations:
        return None
    views = {}
    for annotation in annotations:
        if type(annotation) is not dict or type(annotation.get("path")) is not list:
            return None
        path = annotation["path"]
        if any(type(part) not in (str, int) for part in path):
            return None
        node = value
        for part in path:
            if type(node) is dict and type(part) is str and part in node:
                node = node[part]
            elif type(node) is list and type(part) is int and 0 <= part < len(node):
                node = node[part]
            else:
                return None
        kind = annotation.get("kind")
        if kind == "overview":
            operations = annotation.get("operations")
            if (set(annotation) != {"path", "kind", "operations"}
                    or type(node) is not dict or not node or type(operations) is not dict
                    or node.keys() != operations.keys()
                    or any(type(k) is not str or type(operations[k]) is not str or not operations[k]
                           or not _capability_shape(v) for k, v in node.items())):
                return None
        elif kind == "detail":
            operation = annotation.get("operation")
            if (set(annotation) != {"path", "kind", "operation"} or not _capability_shape(node)
                    or not (operation is None or (type(operation) is str and operation))):
                return None
        else:
            return None
        path = tuple(path)
        if path in views:
            return None
        views[path] = annotation
    for path, view in views.items():
        for length in range(len(path)):
            parent = views.get(path[:length])
            if parent is not None:
                # 总览可伴随一致的直接子详情标记；只渲染总览，不重复子项。
                if (parent["kind"] != "overview" or view["kind"] != "detail" or len(path) != length + 1
                        or view["operation"] not in (None, parent["operations"].get(path[-1]))):
                    return None
    return views


_CAPABILITY_READ_HINT = (
    "单项详情：保存返回值，按原键索引后 emit；过长时字段级读取 state/provider/reason/limits。"
    "limits 的点分键是原字符串键，不是属性路径。"
)


def _render_capability_detail(value: dict, operation: str | None, limit: int) -> str:
    title = f"Capability 详情：{operation}" if operation else "Capability 详情（操作名未登记）"
    raw = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    complete = title + "（完整原值）\n" + raw
    if len(complete) <= limit:
        return complete
    notice = title + "（有省略，非完整原值）\n" + _CAPABILITY_READ_HINT
    lines = [notice]
    # 最小安全提示可以超过极小预算；原字段只能整体保留，不能拦腰截断。
    for key, item in value.items():
        field = _compact_json({key: item})
        if sum(map(len, lines)) + len(lines) + len(field) <= limit:
            lines.append(field)
    return "\n".join(lines)


def _capability_category(value: dict) -> str:
    limits = value.get("limits") or {}
    implementation, supported = limits.get("implementation"), limits.get("scope_supported")
    if implementation == "implemented" and supported is True:
        return "已实现且作用域支持（不代表权限就绪）"
    if implementation == "implemented" and supported is False:
        return "已实现但当前作用域不支持"
    if implementation in ("unsupported", "unimplemented"):
        return "未实现"
    return "实现/作用域未登记"


# 仅用于展示的已知解释性元数据；禁止按 metadata.* 等宽泛前缀丢弃新增字段。
_CAPABILITY_METADATA = {
    "metadata.name", "metadata.text", "metadata.missing", "metadata.last_verified_at", "metadata.dirty_if_unknown",
}
_CAPABILITY_CONTEXT = ("scope", "permissions", "readiness", "permission_enforcement")
_CAPABILITY_COORDINATES = ("coordinate_space", "latest_only", "same_scope", "max_age_ms", "bounds")
_CAPABILITY_POINT = (
    "explicit_image_same_turn", "bare_coordinates_require_prior_emit",
    "image_id_requires_prior_emit", "point_accepts_extra_image_or_y",
)
_CAPABILITY_NORMALIZED = ("accepts_normalized_point",)
_CAPABILITY_DELIVERY = (
    "delivery.auto", "delivery.background", "delivery.foreground", "delivery.foreground_fallback",
    "delivery.requires_explicit_foreground",
)
# [] 仅匹配数组下标；这些操作的已知参数在不支持组中可移至详情，未知键一律显示 extra。
_CAPABILITY_OPERATIONS = {
    "apps.list": ("列举应用", "app_identity runtime_instance pid_reuse_detection"),
    "apps.get": ("获取应用", "app_identity runtime_instance pid_reuse_detection"),
    "apps.launch": ("启动应用", "app_identity runtime_instance pid_reuse_detection"),
    "windows.list": ("列举窗口", "app_identity runtime_instance pid_reuse_detection"),
    "windows.get": ("获取窗口", "app_identity runtime_instance pid_reuse_detection"),
    "ui.ax.observe": ("AX 观察", "text_format continuation.[] character_budget diff default_timeout_ms"),
    "ui.ax.click": ("AX 点击", "variants.[].button variants.[].count variants.[].action variants.[].delivery.[] requires_advertised_action"),
    "ui.ax.set_text": ("设置 AX 文本", "requires_advertised_action writable_value"),
    "ui.ax.perform": ("执行 AX action", "actions.[] requires_advertised_action args"),
    "ui.vision.observe": ("截图", "coordinate_space default_timeout_ms desktop_scope window_fallback"),
    "ui.vision.click": ("图像点击", "buttons.[] counts.[] accepts_ax_node"),
    "ui.vision.scroll": ("图像滚动", "units.[] amount.type amount.min amount.max"),
    "ui.vision.drag": ("拖拽", "hold_ms same_image targets duration_ms.min duration_ms.max duration_ms.less_than_timeout_ms"),
    "ui.vision.move": ("移动指针", "steps.[] desktop_scope"),
    "ui.input.text": ("输入文本", "target_scopes.[] target_optional"),
    "ui.input.press_key": ("按键", "function_keys.min function_keys.max modifiers.[] key_aliases.Delete key_aliases.Backspace"),
}
_CAPABILITY_ACTIONS = {
    "ui.ax.click", "ui.ax.set_text", "ui.ax.perform", "ui.vision.click", "ui.vision.scroll",
    "ui.vision.drag", "ui.vision.move", "ui.input.text", "ui.input.press_key",
}
_CAPABILITY_VISUAL = {"ui.vision.click", "ui.vision.scroll", "ui.vision.drag", "ui.vision.move"}


def _capability_known_limit(operation: str, key: str) -> bool:
    patterns = _CAPABILITY_OPERATIONS.get(operation, ("", ""))[1].split()
    if operation in _CAPABILITY_ACTIONS:
        patterns.extend(_CAPABILITY_DELIVERY)
    if operation in _CAPABILITY_VISUAL:
        patterns.extend(_CAPABILITY_COORDINATES)
        patterns.extend(_CAPABILITY_NORMALIZED)
        if operation != "ui.vision.drag":
            patterns.extend(_CAPABILITY_POINT)
    return any(re.fullmatch(re.escape(pattern).replace(r"\[\]", r"(?:0|[1-9]\d*)"), key)
               for pattern in patterns)


def _capability_fields(fields: dict) -> str:
    return "; ".join(f"{key}={_compact_json(value)}" for key, value in fields.items())


def _capability_array(fields: dict, prefix: str, item_type: type) -> list | None:
    keys = [key for key in fields if key.startswith(prefix + ".")]
    if not keys or set(keys) != {f"{prefix}.{i}" for i in range(len(keys))}:
        return None
    values = [fields[f"{prefix}.{i}"] for i in range(len(keys))]
    return values if all(type(value) is item_type for value in values) else None


def _capability_limits(limits: dict, original: dict) -> str:
    """安全投影常见限制；字段缺失、异常值或目标键冲突时保留原键 JSON。"""
    fields = dict(limits)

    def available(target: str) -> bool:
        return not any(key == target or key.startswith(target + ".") for key in original)

    def project(target: str, keys: list[str], value: Any) -> None:
        for key in keys:
            fields.pop(key)
        fields[target] = value

    delivery_keys = ["delivery.background", "delivery.foreground"]
    if (all(type(fields.get(key)) is bool for key in delivery_keys) and available("delivery_allowed")
            and available("delivery.allowed")):
        project("delivery_allowed", delivery_keys,
                [key.split(".")[1] for key in delivery_keys if fields[key]])
    key = "delivery.requires_explicit_foreground"
    if type(fields.get(key)) is bool and available("requires_explicit_foreground"):
        project("requires_explicit_foreground", [key], fields[key])
    for prefix, item_type in (("continuation", str), ("actions", str), ("buttons", str), ("counts", int),
                              ("units", str), ("steps", int), ("target_scopes", str), ("modifiers", str)):
        array = _capability_array(fields, prefix, item_type)
        if array is not None and prefix not in original:
            project(prefix, [f"{prefix}.{i}" for i in range(len(array))], array)
    variant_keys = [key for key in original if key.startswith("variants.")]
    if variant_keys and "variants" not in original:
        indices = {key.split(".")[1] for key in variant_keys}
        variants = []
        if indices == {str(i) for i in range(len(indices))}:
            for i in range(len(indices)):
                prefix = f"variants.{i}."
                entry = {key[len(prefix):]: original[key] for key in variant_keys if key.startswith(prefix)}
                delivery = _capability_array(entry, "delivery", str)
                if (delivery is None or set(entry) != {"button", "count", "action", *(
                        f"delivery.{j}" for j in range(len(delivery)))}
                        or type(entry["button"]) is not str or type(entry["action"]) is not str
                        or type(entry["count"]) is not int):
                    break
                variants.append({key: entry[key] for key in ("button", "count", "action")}
                                | {"delivery": delivery})
        if len(variants) == len(indices) and all(key in fields for key in variant_keys):
            project("variants", variant_keys, variants)
    ranges = []
    for prefix in ("amount", "duration_ms", "function_keys"):
        low, high = fields.get(prefix + ".min"), fields.get(prefix + ".max")
        flag = prefix + ".less_than_timeout_ms"
        if (prefix in original or type(low) is not int or type(high) is not int or not 0 <= low <= high
                or (prefix == "duration_ms" and type(fields.get(flag)) is not bool)):
            continue
        fields.pop(prefix + ".min")
        fields.pop(prefix + ".max")
        suffix = ""
        if prefix == "duration_ms" and fields[flag] is True:
            fields.pop(flag)
            suffix = " 且小于 timeout_ms"
        ranges.append(f"{prefix}={low}..{high}{suffix}")
    return "; ".join(filter(None, [_capability_fields(fields), *ranges]))


def _render_capability_overview(value: dict, operations: dict, limit: int) -> str:
    """用途与关键限制摘要，不承担全字段重建；不探测权限、不改变原事件。"""
    names = [operations[key] if key == operations[key] else
             f"{operations[key]}（原键={_compact_json(key)}）" for key in value]
    lines = [f"Capability 总览：共 {len(value)} 项，操作名称全部覆盖；非全字段无损视图。",
             "已省略已知解释性元数据；不支持项的已知参数见详情，未知字段保留为 extra。",
             "needs_user_action 不等于权限缺失，已有授权不重复询问；静态能力表不能诊断当前窗口。",
             _CAPABILITY_READ_HINT]
    rows = []
    for key, name in zip(value, names):
        cap, operation = value[key], operations[key]
        limits = cap["limits"] or {}
        context = {"provider": cap["provider"], **{k: limits[k] for k in _CAPABILITY_CONTEXT if k in limits}}
        status = {k: limits[k] for k in ("implementation", "scope_supported") if k in limits}
        status.update(reason=cap["reason"])
        known, extra = {}, {}
        for k, v in limits.items():
            if (k in _CAPABILITY_METADATA or re.fullmatch(r"supported_scopes\.(?:0|[1-9]\d*)", k)
                    or k in _CAPABILITY_CONTEXT or k in ("implementation", "scope_supported")):
                continue
            (known if _capability_known_limit(operation, k) else extra)[k] = v
        extras = {}
        if cap["limits"] is None:
            extras["limits"] = None
        if extra:
            extras["extra_limits"] = extra
        extra_cap = {k: v for k, v in cap.items() if k not in ("state", "provider", "reason", "limits")}
        if extra_cap:
            extras["extra_cap"] = extra_cap
        category = _capability_category(cap)
        usable = (cap["state"] != "unsupported" and limits.get("implementation") == "implemented"
                  and limits.get("scope_supported") is True)
        rows.append(dict(name=name, operation=operation, state=cap["state"], context=context, status=status,
                         category=category, usable=usable, known=known, extras=extras, original=limits))
    common = _capability_common([row["context"] for row in rows])
    if common:
        lines.append("全表公共：" + _capability_fields(common))
    groups = []
    for row in sorted(rows, key=lambda row: (not row["usable"], row["operation"], row["name"])):
        context = {k: v for k, v in row["context"].items() if k not in common}
        group_key = (row["category"], row["state"], row["status"], context)
        group = next((members for key, members in groups if _capability_equal(list(key), list(group_key))), None)
        if group is None:
            group = []
            groups.append((group_key, group))
        group.append(row)
    for (category, state, status, context), members in groups:
        lines.append(f"{category} [{state}]（{len(members)} 项）：" + _capability_fields({**context, **status}))
        if not members[0]["usable"]:
            buckets = []
            for row in members:
                bucket = next((names for extra, names in buckets if _capability_equal(extra, row["extras"])), None)
                if bucket is None:
                    bucket = []
                    buckets.append((row["extras"], bucket))
                bucket.append(row["name"])
            for extra, bucket in buckets:
                lines.append("- " + "、".join(bucket) + ("; " + _capability_fields(extra) if extra else ""))
            continue
        actions = [row for row in members if any(k in row["known"] for k in _CAPABILITY_DELIVERY)]
        defaults = {}
        for key in ("delivery.auto", "delivery.foreground_fallback"):
            if len(actions) >= 2 and all(key in row["known"] for row in actions):
                values = [row["known"][key] for row in actions]
                chosen = max(values, key=lambda v: sum(_capability_equal(v, other) for other in values))
                if sum(_capability_equal(chosen, other) for other in values) >= 2:
                    defaults[key] = chosen
        if defaults:
            lines.append("本组动作默认（仅含 delivery 的操作，差异见 exception）：" + _capability_fields(defaults))
        visual = [row for row in members if row["operation"] in _CAPABILITY_VISUAL]
        coordinates = _capability_common([
            {k: v for k, v in row["known"].items() if k in _CAPABILITY_COORDINATES} for row in visual])
        if coordinates:
            lines.append("本组视觉动作公共（不含 observe）：" + _capability_fields(coordinates))
        for row in members:
            fields = dict(row["known"])
            exceptions = {}
            if any(k in fields for k in _CAPABILITY_DELIVERY):
                for key, default in defaults.items():
                    actual = fields.pop(key)
                    if not _capability_equal(actual, default):
                        exceptions[key] = actual
            if row["operation"] in _CAPABILITY_VISUAL:
                fields = {k: v for k, v in fields.items() if k not in coordinates}
            purpose = _CAPABILITY_OPERATIONS.get(row["operation"], ("", ""))[0]
            line = f"- {row['name']} [{state}]" + (f" {purpose}" if purpose else "")
            parts = [_capability_limits(fields, row["original"]), _capability_fields(row["extras"])]
            if exceptions:
                parts.append("exception=" + _compact_json(exceptions))
            if row["operation"] == "ui.ax.observe" and "character_budget" in fields:
                parts.append("full=True 不取消 character_budget")
            lines.append(line + ("; " + "; ".join(filter(None, parts)) if any(parts) else ""))
    complete = "\n".join(lines)
    if len(complete) <= limit:
        return complete
    # 预算不足时宁可不展示规则，也不留下缺失公共约束的半条操作。
    def notice(count: int) -> str:
        return (f"Capability 总览（有省略）：共 {len(names)} 项；名称覆盖 {count}/{len(names)}"
                f"（省略 {len(names) - count} 项名称）；关键规则未展示，执行前请读单项详情。\n"
                + _CAPABILITY_READ_HINT)
    kept = []
    for name in names:
        candidate = kept + ["- " + name]
        if len(notice(len(candidate))) + sum(map(len, candidate)) + len(candidate) > limit:
            break
        kept = candidate
    return "\n".join([notice(len(kept)), *kept])


def _render_capability_views(value: Any, annotations: Any, limit: int) -> str | None:
    try:
        views = _validated_capability_views(value, annotations)
        if views is None:
            return None
        ancestors = {path[:length] for path in views for length in range(len(path))}

        def visit(node: Any, path: tuple, indent: str) -> list[str]:
            if path in views:
                view = views[path]
                rendered = (_render_capability_overview(node, view["operations"], limit)
                            if view["kind"] == "overview" else
                            _render_capability_detail(node, view["operation"], limit))
                return [indent + line for line in rendered.splitlines()]
            if path not in ancestors:
                return _render_result_value(node, limit, indent)
            lines = [f"{indent}result:" + (f" [{len(node)} 项]" if isinstance(node, list) else "")]
            for key, child in (node.items() if isinstance(node, dict) else enumerate(node)):
                child_path = path + (key,)
                if child_path in views or child_path in ancestors:
                    lines.append(f"{indent}  {key if isinstance(node, dict) else f'[{key}]'}:")
                    lines.extend(visit(child, child_path, indent + "    "))
                elif isinstance(node, list):
                    lines.extend(_render_items([child], limit, indent + "  "))
                elif isinstance(child, list) and len(child) > 1 and len(_compact_json(child)) > 240:
                    lines.append(f"{indent}  {key}: [{len(child)} 项]")
                    lines.extend(_render_items(child, limit, indent + "    "))
                else:
                    lines.append(f"{indent}  {key}: {compact_text(_compact_json(child), limit)}")
            return lines

        return "\n".join(visit(value, (), ""))
    except (TypeError, ValueError, KeyError, IndexError, RecursionError):
        # annotation 是可选展示信息，不能使有效原始 event 的执行反馈失败。
        return None


def _validated_inventory_view(value: Any, annotation: Any) -> str | None:
    """只校验真实类型注解；形状不符或损坏标记返回 None 以回退通用渲染。

    注解由 runtime 仅对真实 AppInventory/WindowInventory 实例发出；这里再核对
    value 形状（apps/windows 为 list 且含 complete），二者任一不符即回退。
    """
    if type(annotation) is not dict or set(annotation) != {"kind"}:
        return None
    kind = annotation["kind"]
    if kind not in ("apps", "windows") or type(value) is not dict:
        return None
    items = value.get(kind)
    if type(items) is not list or "complete" not in value:
        return None
    return kind


def _inventory_app_row(app: dict) -> str:
    """运行项显示 pid，未运行项按需显示 path；只保留发现所需的 label/id。"""
    app_id = app.get("id")
    label = app.get("label") or app_id
    row = f"{label} ({app_id})"
    if app.get("running") is True:
        row += f" pid={app.get('pid')}"
    else:
        launch_path = app.get("launch_path")
        if launch_path:
            row += f" path={launch_path}"
    if app.get("launchable") is False:
        row += " launchable=false"
    return row


def _inventory_window_row(window: dict) -> str:
    """窗口逐条一行；title 非空才显示，bounds 为 dict 才显示尺寸与原点。"""
    row = f"{window.get('id')}"
    title = window.get("title")
    if title:
        row += f" title={title}"
    bounds = window.get("bounds")
    if type(bounds) is dict:
        row += (f" bounds={bounds.get('width')}x{bounds.get('height')}"
                f"@{bounds.get('x')},{bounds.get('y')}")
    return row


def _render_inventory_rows(rows: list[str], limit: int, indent: str, total: int) -> list[str]:
    """清单逐条一行；超预算按条目边界保留首尾，绝不拦腰截断单行。"""
    if sum(len(row) for row in rows) + len(rows) * (len(indent) + 2) <= limit:
        return [f"{indent}- {row}" for row in rows]
    head: list[str] = []
    used = 0
    for row in rows:
        if head and used + len(row) > limit // 2:
            break
        head.append(row)
        used += len(row)
    tail: list[str] = []
    used = 0
    for row in reversed(rows[len(head):]):
        if tail and used + len(row) > limit // 2:
            break
        tail.insert(0, row)
        used += len(row)
    omitted = len(rows) - len(head) - len(tail)
    lines = [f"{indent}- {row}" for row in head]
    if omitted > 0:
        lines.append(
            f"{indent}...[中间省略 {omitted} 条；本次调用的返回值含全部 {total} 项，"
            f"赋值给变量后可在代码中过滤/索引/切片获取]..."
        )
    lines.extend(f"{indent}- {row}" for row in tail)
    return lines


def _render_inventory_view(value: Any, annotation: Any, limit: int) -> str | None:
    """AppInventory/WindowInventory 的紧凑投影；任何异常回退通用渲染，绝不影响反馈。"""
    try:
        kind = _validated_inventory_view(value, annotation)
        if kind is None:
            return None
        items = value[kind]
        if kind == "apps":
            running = sum(1 for app in items if type(app) is dict and app.get("running") is True)
            header = (f"AppInventory: {len(items)} 项（running {running}）；"
                      "complete=false 表示清单可能不完整；变量保留全部字段，可在代码中过滤/索引。")
            rows = [_inventory_app_row(app) for app in items]
        else:
            header = (f"WindowInventory: {len(items)} 项；"
                      f"complete={_compact_json(value['complete'])}；变量保留全部字段。")
            rows = [_inventory_window_row(window) for window in items]
        return "\n".join([header, *_render_inventory_rows(rows, limit, "", len(items))])
    except (TypeError, ValueError, KeyError, IndexError, AttributeError, RecursionError):
        # annotation 是可选展示信息，不能使有效原始 event 的执行反馈失败。
        return None


def render_display(event: dict[str, Any], limit: int, *, include_label: bool = True) -> str:
    """AX 保持原样；只有 JSON 的有效类型标记可选择能力视图。"""
    label = str(event.get("label") or "") if include_label else ""
    heading = f"[{compact_text(label, limit)}]\n" if label else ""
    kind = event.get("kind")
    if event.get("type") == "screenshot":
        metadata = {key: event.get(key) for key in (
            "width", "height", "coordinate_space", "target"
        )}
        return heading + "screenshot: " + compact_text(_compact_json(metadata), limit)
    if kind == "ax":
        return heading + str(event.get("text", ""))
    if kind == "text":
        return heading + compact_text(str(event.get("text", "")), limit)
    value = event.get("value")
    if kind == "json" and "capability_views" in event:
        rendered = _render_capability_views(value, event["capability_views"], limit)
        if rendered is not None:
            return heading + rendered
    if kind == "json" and "inventory_view" in event:
        rendered = _render_inventory_view(value, event["inventory_view"], limit)
        if rendered is not None:
            return heading + rendered
    if kind == "bash" and isinstance(value, dict):
        summary = {key: value[key] for key in ("returncode", "ok", "timed_out") if key in value}
        lines = ["  result: " + _compact_json(summary)]
        for stream in ("stdout", "stderr"):
            lines.append(f"  {stream}:\n" + compact_text(str(value.get(stream) or ""), limit))
        return heading + "\n".join(lines)
    return heading + "\n".join(_render_result_value(value, limit))


def render_tool_call(index: int, call: dict[str, Any], limit: int) -> str:
    """成功仅留审计；失败只回报工具名与短原因，绝不回灌 arguments 或正文。"""
    reasons: list[str] = []
    failed = call.get("ok") is False or ("error" in call and "result" not in call)
    if failed and call.get("error"):
        reasons.append(str(call["error"]))

    def inspect(value: Any) -> None:
        nonlocal failed
        if isinstance(value, dict):
            bad_code = value.get("returncode") not in (None, 0)
            timed_out = bool(value.get("timed_out"))
            rejected = value.get("is_error") is True or value.get("ok") is False
            if bad_code or timed_out:
                failed = True
                reasons.append(f"returncode={value.get('returncode')}; timed_out={str(timed_out).lower()}")
            elif rejected:
                failed = True
                reason = next((value[key] for key in ("error", "message", "reason", "text")
                               if value.get(key)), None)
                if reason is not None:
                    reasons.append(str(reason))
            for key, item in value.items():
                if key not in {"arguments", "stdout", "stderr", "state", "images", "data_base64"}:
                    if isinstance(item, (dict, list)):
                        inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)

    inspect(call.get("result"))
    if not failed:
        return ""
    budget = max(1, min(limit, 500))
    tool = compact_text(str(call.get("tool") or "?"), 100)
    effect = compact_text(str(call.get("effect") or "error"), 80)
    detail = "; ".join(reasons) or "工具返回失败；请显式 emit 返回值查看详情。"
    return f"[#{index}] {tool} — ERROR ({effect})\n  error: {compact_text(detail, budget)}"


def _apply_event(control: dict[str, Any], event: dict[str, Any]) -> None:
    control["events"].append(event)
    event_type = event.get("type")
    if event_type == "screenshot":
        control["screenshot"] = True
        control["screenshots"].append(dict(event))
    elif event_type == "display":
        control["displays"].append(dict(event))
    elif event_type == "tool_call":
        control["tool_calls"].append(event)
    elif event_type == "terminate" and control["terminate"] is None:
        control["terminate"] = {
            "status": event.get("status", "failure"), "answer": event.get("answer"),
            **({"block_index": event["block_index"]} if "block_index" in event else {}),
        }
    elif event_type == "ask_user" and control["ask_user"] is None:
        control["ask_user"] = {
            "question": str(event.get("question") or ""),
            **({"block_index": event["block_index"]} if "block_index" in event else {}),
        }
    elif event_type == "todo":
        control["todo"].append(event)
    elif event_type == "code_block_start":
        control["total_blocks"] = event["total_blocks"]
        control["started_blocks"] = event["block_index"]
    elif event_type == "code_block_end":
        if event.get("status") == "completed":
            control["completed_blocks"] = event["block_index"]
        else:
            control["stopped_block"] = event["block_index"]
            control["stop_reason"] = event.get("stop_reason")
    elif event_type == "end":
        control["completed"] = True
        control["exit_code"] = event.get("exit_code")
        control.update({key: event[key] for key in BLOCK_PROGRESS_FIELDS if key in event})


def parse_control_stdout(stdout: str) -> tuple[str, dict[str, Any]]:
    """仅用于旧单步 CLI 输出；常驻协议绝不解析模型的 stdout。"""
    control = empty_control()
    kept: list[str] = []
    for line in stdout.splitlines():
        if not line.startswith(CTRL_PREFIX):
            kept.append(line)
            continue
        try:
            event = json.loads(
                base64.b64decode(line[len(CTRL_PREFIX):].strip()).decode("utf-8")
            )
        except Exception:
            kept.append("[mac-agent] malformed runtime control event was ignored")
            continue
        if isinstance(event, dict):
            _apply_event(control, event)
    return "\n".join(kept).strip(), control


def _kill_group(process: asyncio.subprocess.Process) -> None:
    # 即使组长已经退出，也要清理仍持有句柄的子进程。
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # 组长已退出，或沙箱/权限限制下无法 killpg；进程最终会被回收，不应让清理路径崩溃。
        pass


async def _finish_cancelled_cleanup(awaitable: Any) -> Any:
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # 已在处理取消；重复取消不能中断回收或丢失尚在启动的进程。
            continue
    return task.result()


class CodeExecutor:
    def __init__(
        self,
        *,
        runtime_path: Path,
        work_dir: Path,
        rpc_socket: Path,
        timeout: float,
        rpc_timeout: float,
        coordinate_range: float,
        pyautogui_pause: float,
        enable_bash: bool,
        persistent: bool = True,
        ui_api: str = "mai-ui",
    ) -> None:
        if ui_api not in {"mai-ui", "legacy-cua"}:
            raise ValueError("ui_api 必须是 mai-ui 或 legacy-cua")
        self.ui_api = ui_api
        self.runtime_path = runtime_path.resolve()
        self.work_dir = work_dir.resolve()
        self.rpc_socket = rpc_socket.resolve()
        self.timeout = timeout
        self.rpc_timeout = rpc_timeout
        self.coordinate_range = coordinate_range
        self.pyautogui_pause = pyautogui_pause
        self.enable_bash = enable_bash
        self.persistent = persistent
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._watch_task: asyncio.Task | None = None
        self._active_run: asyncio.Task | None = None
        self._closed = False
        self._reset_pending = False
        self._task_reset_pending = False
        self._output_reset_revision = 0
        self._output_reset_sent = 0
        self._rpc_generation = 0
        self._child_groups: set[int] = set()

    def set_rpc_generation(self, generation: int) -> None:
        """同步更新下一轮携带的 RPC 代次，不改变正在执行的请求上下文。"""
        self._rpc_generation = generation

    def reset_output_context(self) -> None:
        """摘要丢弃展示历史后，下一轮清除裸目标依据，不清 Python 活对象。"""
        self._output_reset_revision += 1

    def _config(self) -> dict[str, Any]:
        return {
            "ui_api": self.ui_api,
            "rpc_socket": str(self.rpc_socket),
            "rpc_timeout": self.rpc_timeout,
            "rpc_generation": self._rpc_generation,
            "coordinate_range": self.coordinate_range,
            "pyautogui_pause": self.pyautogui_pause,
            "enable_bash": self.enable_bash,
            "work_dir": str(self.work_dir),
        }

    async def _watch_worker(
        self, process: asyncio.subprocess.Process,
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        cancelled = False
        try:
            await process.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # 事件循环退出时同样回收 worker；不依赖调用者的垃圾回收时机。
            _kill_group(process)
            self._stop_child_groups()
            if self._process is process:
                self._reset_pending = True
            try:
                # 活动请求先读完已到达的错误输出；空闲或取消时直接关闭传输。
                while (
                    not cancelled and self._process is process
                    and self._active_run is not None and not reader.at_eof()
                ):
                    await asyncio.sleep(0.01)
            finally:
                writer.close()
                await process.wait()

    async def _start_worker(self) -> None:
        parent, child = socket.socketpair()
        parent.setblocking(False)
        try:
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-u", str(self.runtime_path), "--worker", str(child.fileno()),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                pass_fds=(child.fileno(),),
                start_new_session=True,
            ))
            try:
                self._process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                self._process = await _finish_cancelled_cleanup(spawning)
                raise
            self._reader, self._writer = await asyncio.open_connection(
                sock=parent, limit=MAX_FRAME_BYTES
            )
            self._watch_task = asyncio.create_task(
                self._watch_worker(self._process, self._reader, self._writer)
            )
        except BaseException:
            parent.close()
            await _finish_cancelled_cleanup(self._stop_worker())
            raise
        finally:
            child.close()

    def _stop_child_groups(self) -> None:
        for pid in self._child_groups:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self._child_groups.clear()

    async def _stop_worker(self) -> None:
        process, writer, watch = self._process, self._writer, self._watch_task
        self._process = self._reader = self._writer = self._watch_task = None
        if process is not None:
            _kill_group(process)
        self._stop_child_groups()
        if writer is not None:
            writer.close()
        if watch is not None:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        if process is not None:
            await process.wait()
            for path in self.work_dir.glob(f"bash_{process.pid}_*"):
                path.unlink(missing_ok=True)
        if writer is not None:
            try:
                await writer.wait_closed()
            except (OSError, RuntimeError):
                pass

    async def reset_task(self) -> None:
        """下一轮执行前清空 CUA 引用缓存，但不清空 Python 用户变量。"""
        async with self._lock:
            if self._closed:
                raise RuntimeError("执行器已经关闭")
            self._task_reset_pending = True

    async def close(self) -> None:
        """幂等关闭；取消进行中的轮次并硬清理整个 worker 进程组。"""
        self._closed = True
        active = self._active_run
        if active is not None and active is not asyncio.current_task():
            active.cancel()
        async with self._lock:
            await asyncio.shield(self._stop_worker())

    async def __aenter__(self) -> CodeExecutor:
        if self._closed:
            raise RuntimeError("执行器已经关闭")
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def run(self, code: str | list[str], todo_items: list[dict[str, Any]]) -> CodeExecutionResult:
        async with self._lock:
            if self._closed:
                raise RuntimeError("执行器已经关闭")
            if not isinstance(code, (str, list)):
                raise TypeError("code 必须是 str 或非空 list[str]")
            control = empty_control()
            control["total_blocks"] = len(code) if isinstance(code, list) else 1
            if self._reset_pending or (
                self._process is not None and self._process.returncode is not None
            ):
                await self._stop_worker()
                self._reset_pending = False
                # 此请求尚未执行；先让主循环获得丢失上下文的反馈，禁止自动重放 UI。
                control["stop_reason"] = "session_reset"
                return CodeExecutionResult(
                    "", "worker 在空闲时退出；本轮代码未执行。", -1, False,
                    control, session_reset=True,
                )
            self._active_run = asyncio.current_task()
            request_id = uuid.uuid4().hex
            output = {name: bytearray() for name in OUTPUT_LIMITS}
            timed_out = False
            reset = False
            returncode = -1
            error = ""
            try:
                async with asyncio.timeout(self.timeout):
                    if self._process is None:
                        await self._start_worker()
                    assert self._writer is not None and self._reader is not None
                    output_reset_revision = self._output_reset_revision
                    payload = {
                        "request_id": request_id,
                        "code": code,
                        "todo_items": todo_items,
                        "reset_task": self._task_reset_pending,
                        "reset_output_context": output_reset_revision != self._output_reset_sent,
                        "config": self._config(),
                    }
                    self._writer.write(json.dumps(payload, ensure_ascii=True).encode() + b"\n")
                    await self._writer.drain()
                    event_bytes = 0
                    while True:
                        raw = await self._reader.readline()
                        if not raw:
                            raise ConnectionError("worker 控制通道已关闭")
                        frame = json.loads(raw)
                        if not isinstance(frame, dict):
                            raise ValueError("worker 控制帧无效")
                        if frame.get("request_id") != request_id:
                            continue
                        kind = frame.get("kind")
                        if kind == "output":
                            stream = frame.get("stream")
                            if stream not in output:
                                raise ValueError("未知的输出流")
                            data = base64.b64decode(frame["data"], validate=True)
                            room = max(0, OUTPUT_LIMITS[stream] + 256 - len(output[stream]))
                            output[stream].extend(data[:room])
                        elif kind in {"event", "done"}:
                            event_bytes += len(raw)
                            if event_bytes > MAX_CONTROL_BYTES:
                                raise ValueError("本轮控制事件超过额度")
                            event = frame.get("event")
                            if not isinstance(event, dict):
                                raise ValueError("worker 控制事件无效")
                            if kind == "event":
                                # 只有专用 done 帧能够完成本轮，普通 end 事件也不能提前完成。
                                if event.get("type") == "_child_group":
                                    pid = int(event["pid"])
                                    if pid <= 1:
                                        raise ValueError("无效的 Bash 进程组")
                                    if event.get("active"):
                                        self._child_groups.add(pid)
                                    else:
                                        self._child_groups.discard(pid)
                                elif event.get("type") != "end":
                                    _apply_event(control, event)
                            else:
                                if event.get("type") != "end":
                                    raise ValueError("worker 缺少轮次结束事件")
                                _apply_event(control, event)
                                returncode = int(event.get("exit_code", 1))
                                if frame.get("fatal"):
                                    raise ConnectionError("worker 初始化或任务重置失败")
                                self._task_reset_pending = False
                                self._output_reset_sent = output_reset_revision
                                break
                        else:
                            raise ValueError("未知的 worker 控制帧")
            except asyncio.CancelledError:
                await _finish_cancelled_cleanup(self._stop_worker())
                self._reset_pending = True
                raise
            except (TimeoutError, OSError, ValueError, TypeError, KeyError) as exc:
                timed_out = isinstance(exc, TimeoutError)
                reset = True
                error = (
                    f"本轮代码超时（{self.timeout:g}s），worker 进程组已硬终止。"
                    if timed_out else f"worker 会话故障：{type(exc).__name__}: {exc}"
                )
                process = self._process
                returncode = process.returncode if process and process.returncode is not None else -1
                await asyncio.shield(self._stop_worker())
                self._reset_pending = False
                control["completed"] = False
                control["exit_code"] = None
                control["stop_reason"] = "timeout" if timed_out else "session_fault"
                if control["stopped_block"] is None and control["started_blocks"] > control["completed_blocks"]:
                    control["stopped_block"] = control["started_blocks"]
            finally:
                if not self.persistent:
                    await asyncio.shield(self._stop_worker())
                    self._reset_pending = False
                self._active_run = None
            stdout = output["stdout"].decode("utf-8", "replace").strip()
            stderr = output["stderr"].decode("utf-8", "replace").strip()
            if error:
                stderr = (stderr + "\n" if stderr else "") + error
            return CodeExecutionResult(
                stdout, stderr, returncode, timed_out, control, session_reset=reset
            )
