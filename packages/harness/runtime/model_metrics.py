"""纯离线模型计量：保留未知 usage、按请求快照校准输入、汇总实际 HTTP 尝试。"""

from __future__ import annotations

import copy
import math
from typing import Any


__all__ = [
    "normalize_usage", "estimate_messages_tokens", "TokenEstimator", "aggregate_attempts",
    "compact_step_metrics", "expand_step_metrics",
]

_SCHEMA_VERSION = 1
_ESTIMATOR_RULE = "ascii4-ceil_nonascii1_message4_global2_fixed-image_usage-delta-v1"
_PURPOSES = ("action", "compaction")
_TOKEN_KINDS = ("input", "output", "cache", "reasoning")
_STATE_KEYS = frozenset({
    "schema_version", "estimator_rule", "scope", "image_token_estimate", "anchors",
})
_ANCHOR_KEYS = frozenset({"input_tokens", "estimated_tokens", "attempt_id"})


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _first_count(*values: Any) -> int | None:
    for value in values:
        valid = _count(value)
        if valid is not None:
            return valid
    return None


def _detail(usage: dict, key: str, field: str) -> Any:
    details = usage.get(key)
    return details.get(field) if isinstance(details, dict) else None


def normalize_usage(usage: dict | None) -> dict:
    """返回五个独立计数字段；绝不修改原始 usage，也不扣除缓存或另加推理量。

    别名按 OpenAI、兼容接口、已规范字段的顺序取首个合法整数；合法零优先。
    total 优先保留服务端合法值，仅在输入和输出都已知时推导缺失或非法值。
    """
    raw = usage if isinstance(usage, dict) else {}
    input_tokens = _first_count(raw.get("prompt_tokens"), raw.get("input_tokens"))
    output_tokens = _first_count(raw.get("completion_tokens"), raw.get("output_tokens"))
    cache_tokens = _first_count(
        _detail(raw, "prompt_tokens_details", "cached_tokens"),
        _detail(raw, "input_tokens_details", "cached_tokens"),
        raw.get("prompt_cache_hit_tokens"), raw.get("cache_tokens"),
    )
    reasoning_tokens = _first_count(
        _detail(raw, "completion_tokens_details", "reasoning_tokens"),
        _detail(raw, "output_tokens_details", "reasoning_tokens"),
        raw.get("reasoning_tokens"),
    )
    total_tokens = _count(raw.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_tokens": cache_tokens, "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def _require_integer(value: Any, name: str, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} 必须是至少为 {minimum} 的整数")


def _text_tokens(text: str) -> int:
    ascii_count = sum(ord(char) < 128 for char in text)
    return (ascii_count + 3) // 4 + len(text) - ascii_count


def estimate_messages_tokens(messages: list[dict], image_token_estimate: int = 1600) -> int:
    """迁移原字符 fallback：ASCII/4 向上取整，非 ASCII 每字符一个。

    每消息加 4，整个序列加 2。图片只读类型判别，不遍历 URL/base64，不执行 I/O。
    """
    _require_integer(image_token_estimate, "image_token_estimate", 1)

    def content_tokens(content: Any) -> int:
        if isinstance(content, str):
            return _text_tokens(content)
        if isinstance(content, (list, tuple)):
            return sum(content_tokens(part) for part in content)
        if isinstance(content, dict):
            if content.get("type") in ("image_ref", "image_url", "image", "input_image"):
                return image_token_estimate
            if "image_ref" in content or "image_url" in content:
                return image_token_estimate
            if content.get("type") == "text":
                return content_tokens(content.get("text", ""))
            return sum(content_tokens(value) for value in content.values())
        return 0

    def replay_tokens(message: dict) -> int:
        output = message.get("response_output")
        # The assistant text is already counted from `content`; count only the
        # opaque reasoning state that the Responses request additionally replays.
        response_tokens = 0
        if isinstance(output, list):
            response_tokens = sum(
                _text_tokens(item["encrypted_content"])
                for item in output if isinstance(item, dict)
                and item.get("type") == "reasoning"
                and isinstance(item.get("encrypted_content"), str)
            )
        native = message.get("anthropic_content")
        anthropic_tokens = 0
        if isinstance(native, list):
            anthropic_tokens = sum(
                _text_tokens(value)
                for block in native if isinstance(block, dict)
                and block.get("type") in ("thinking", "redacted_thinking")
                for value in (block.get("thinking"), block.get("signature"), block.get("data"))
                if isinstance(value, str)
            )
        return response_tokens + anthropic_tokens

    return 2 + sum(4 + content_tokens(message.get("content")) + replay_tokens(message)
                   for message in messages)


def _require_purpose(purpose: Any) -> None:
    if type(purpose) is not str or purpose not in _PURPOSES:
        raise ValueError("purpose 必须是 action 或 compaction")


def _valid_attempt_id(value: Any) -> bool:
    return value is None or type(value) in (str, int)


class TokenEstimator:
    """每个用途只保存最近准确输入量、原请求估值和尝试 ID，不保存消息正文。

    scope 由调用方绑定模型、端点及图像配置；实例配置固定，避免旧锚点跨配置复用。
    """

    def __init__(self, *, image_token_estimate: int = 1600, scope: str = "") -> None:
        _require_integer(image_token_estimate, "image_token_estimate", 1)
        if type(scope) is not str:
            raise ValueError("scope 必须是字符串")
        self._image_token_estimate = image_token_estimate
        self._scope = scope
        self._anchors: dict[str, dict] = {}

    @property
    def image_token_estimate(self) -> int:
        return self._image_token_estimate

    @property
    def scope(self) -> str:
        return self._scope

    def estimate(
        self, messages: list[dict], *, purpose: str = "action", reserve_tokens: int = 0,
        context_limit: int = 200000,
    ) -> dict:
        """输入预测为 anchor.input + E(新请求) - E(原请求)，预留只加在总预算上。

        纯读取，不移动锚点；异常非正预测回退字符估值，保留差值和 reason 供审计。
        """
        _require_purpose(purpose)
        _require_integer(reserve_tokens, "reserve_tokens", 0)
        _require_integer(context_limit, "context_limit", 1)
        heuristic = estimate_messages_tokens(messages, self.image_token_estimate)
        anchor = self._anchors.get(purpose)
        input_tokens, delta = heuristic, None
        source, reason = "heuristic", "no_anchor"
        if anchor is not None:
            delta = heuristic - anchor["estimated_tokens"]
            prediction = anchor["input_tokens"] + delta
            if prediction < 0 or (prediction == 0 and bool(messages)):
                reason = "non_positive_usage_delta"
            else:
                input_tokens = prediction
                source, reason = "usage_delta", None
        total = input_tokens + reserve_tokens
        return {
            "input_tokens": input_tokens, "reserve_tokens": reserve_tokens,
            "total_tokens": total, "context_limit": context_limit,
            "over_limit": total > context_limit, "source": source,
            "heuristic_input_tokens": heuristic,
            "anchor_input_tokens": anchor["input_tokens"] if anchor is not None else None,
            "anchor_estimated_tokens": anchor["estimated_tokens"] if anchor is not None else None,
            "delta_tokens": delta,
            "anchor_attempt_id": anchor["attempt_id"] if anchor is not None else None,
            "reason": reason,
        }

    def observe(
        self, messages: list[dict], usage: dict | None, *, purpose: str = "action",
        attempt_id: str | int | None = None,
    ) -> None:
        """调用方须传实际发出的原请求快照；usage 缺失不清空最近有效锚点。"""
        _require_purpose(purpose)
        input_tokens = normalize_usage(usage)["input_tokens"]
        if input_tokens is None:
            return
        if not _valid_attempt_id(attempt_id):
            raise ValueError("attempt_id 必须是字符串、整数或 None")
        estimated = estimate_messages_tokens(messages, self.image_token_estimate)
        self._anchors[purpose] = {
            "input_tokens": input_tokens, "estimated_tokens": estimated, "attempt_id": attempt_id,
        }

    def state_dict(self) -> dict:
        """返回可独立修改、可 JSON 落盘的纯数值锚点快照。"""
        return {
            "schema_version": _SCHEMA_VERSION, "estimator_rule": _ESTIMATOR_RULE,
            "scope": self.scope, "image_token_estimate": self.image_token_estimate,
            "anchors": {purpose: dict(anchor) for purpose, anchor in self._anchors.items()},
        }

    def load_state(self, raw: Any) -> bool:
        """严格核对 schema、规则、作用域、配置与全部锚点，成功后一次性替换。"""
        if not isinstance(raw, dict) or set(raw) != _STATE_KEYS:
            return False
        if type(raw["schema_version"]) is not int or raw["schema_version"] != _SCHEMA_VERSION:
            return False
        if type(raw["estimator_rule"]) is not str or raw["estimator_rule"] != _ESTIMATOR_RULE:
            return False
        if type(raw["scope"]) is not str or raw["scope"] != self.scope:
            return False
        if (type(raw["image_token_estimate"]) is not int
                or raw["image_token_estimate"] != self.image_token_estimate):
            return False
        anchors = raw["anchors"]
        if not isinstance(anchors, dict):
            return False
        restored = {}
        for purpose, anchor in anchors.items():
            if type(purpose) is not str or purpose not in _PURPOSES:
                return False
            if not isinstance(anchor, dict) or set(anchor) != _ANCHOR_KEYS:
                return False
            if _count(anchor["input_tokens"]) is None:
                return False
            estimated = _count(anchor["estimated_tokens"])
            if estimated is None or estimated < 2:
                return False
            if not _valid_attempt_id(anchor["attempt_id"]):
                return False
            restored[purpose] = dict(anchor)
        self._anchors = restored
        return True


def _identity(value: Any) -> tuple | None:
    # 不把缺失 ID 合并，也不让布尔与整数 ID 在 Python 字典中误碰撞。
    if type(value) in (str, int) and value != "":
        return type(value), value
    return None


def _duration(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if seconds >= 0 and math.isfinite(seconds) else None


def _aggregate(attempts: list[dict]) -> dict:
    result = {
        "model_response_time_seconds": 0.0, "missing_duration_count": 0,
        "model_attempt_count": len(attempts), "model_call_count": 0,
        "model_failed_attempt_count": 0, "transport_retry_count": 0,
    }
    for kind in _TOKEN_KINDS:
        result[f"known_{kind}_tokens"] = 0
        result[f"unknown_{kind}_usage_count"] = 0
    call_ids = set()
    for attempt in attempts:
        duration = _duration(attempt.get("elapsed_seconds"))
        if duration is None:
            result["missing_duration_count"] += 1
        else:
            result["model_response_time_seconds"] += duration
        call_id = _identity(attempt.get("call_id"))
        if call_id is not None:
            call_ids.add(call_id)
        if attempt.get("status") != "success":
            result["model_failed_attempt_count"] += 1
        retry_number = attempt.get("attempt")
        if type(retry_number) is int and retry_number > 1:
            result["transport_retry_count"] += 1
        usage = normalize_usage(attempt.get("usage"))
        for kind in _TOKEN_KINDS:
            tokens = usage[f"{kind}_tokens"]
            if tokens is None:
                result[f"unknown_{kind}_usage_count"] += 1
            else:
                result[f"known_{kind}_tokens"] += tokens
    result["model_call_count"] = len(call_ids)
    for kind in _TOKEN_KINDS:
        result[f"{kind}_tokens"] = (
            result[f"known_{kind}_tokens"]
            if attempts and result[f"unknown_{kind}_usage_count"] == 0 else None
        )
    return result


def aggregate_attempts(attempts: list[dict]) -> dict:
    """按 attempt_id 只使用最后一个完整快照，绝不累加同一流的累计 usage 帧。

    无 ID 的记录逐项保留；缺失 call_id 不虚构调用身份。只加 elapsed_seconds，
    不另加首 token 等待、退避或人工时间。缺省 purpose 兼容为 action；未知用途仅进总计。
    """
    if not isinstance(attempts, list) or any(not isinstance(item, dict) for item in attempts):
        raise ValueError("attempts 必须是字典列表")
    unique: list[dict] = []
    positions = {}
    for attempt in attempts:
        attempt_id = _identity(attempt.get("attempt_id"))
        if attempt_id is not None and attempt_id in positions:
            unique[positions[attempt_id]] = attempt
        else:
            if attempt_id is not None:
                positions[attempt_id] = len(unique)
            unique.append(attempt)
    result = _aggregate(unique)
    result["by_purpose"] = {
        purpose: _aggregate([item for item in unique if item.get("purpose", "action") == purpose])
        for purpose in _PURPOSES
    }
    return result


# v2 仅是磁盘事实格式；运行态与显式展开视图继续使用完整的 v1 字段。
_STEP_COUNT_KEYS = (
    "parse_retry_count", "parse_failure_count", "manual_retry_count",
    "compaction_retry_count", "compaction_failure_count",
    "compaction_validation_failure_count", "compaction_context_retry_count",
    "compaction_count", "context_retry_count",
)
_RETRY_KEYS = (
    "transport_retry_count", "parse_retry_count", "manual_retry_count",
    "compaction_retry_count", "compaction_context_retry_count", "context_retry_count",
)
_ZERO_TIME_KEYS = ("compaction_time_seconds", "user_wait_time_seconds")
_DERIVED_METRIC_KEYS = frozenset(aggregate_attempts([])) | {"retry_count"}


def _referenced_attempt(attempts: list[dict], attempt_id: Any) -> dict | None:
    identity = _identity(attempt_id)
    if identity is None:
        return None
    return next((item for item in reversed(attempts)
                 if _identity(item.get("attempt_id")) == identity), None)


def compact_step_metrics(metrics: dict) -> dict:
    """将完整指标变为独立的 v2 事实快照；不修改调用方，不推断解析是否成功。"""
    version = metrics.get("schema_version", 1)
    if version not in (1, 2):
        raise ValueError(f"不支持的 step metrics schema：{version!r}")
    source = expand_step_metrics(metrics) if version == 2 else copy.deepcopy(metrics)
    packed = {key: value for key, value in source.items() if key not in _DERIVED_METRIC_KEYS}
    packed["schema_version"] = 2
    attempts = packed.setdefault("attempts", [])
    for attempt in attempts:
        attempt.pop("normalized_usage", None)
    for key in (*_STEP_COUNT_KEYS, *_ZERO_TIME_KEYS):
        if packed.get(key) == 0:
            packed.pop(key)

    accepted_id = packed.get("final_action_attempt_id")
    accepted = _referenced_attempt(attempts, accepted_id)
    final_usage = packed.get("final_action_usage")
    if (accepted is not None and accepted.get("purpose", "action") == "action"
            and accepted.get("status") == "success"
            and final_usage == normalize_usage(accepted.get("usage"))):
        packed.pop("final_action_usage", None)
    elif accepted_id is not None:
        # 不以错误或缺失的关联覆盖旧轨迹/兼容客户端提供的最终用量。
        packed.pop("final_action_attempt_id", None)
    if final_usage == normalize_usage(None):
        packed.pop("final_action_usage", None)
    if packed.get("final_action_attempt_id") is None:
        packed.pop("final_action_attempt_id", None)

    budget = packed.get("context_budget")
    if isinstance(budget, dict):
        for attempt in reversed(attempts):
            attempt_id = attempt.get("attempt_id")
            latest = _referenced_attempt(attempts, attempt_id)
            if latest is attempt and budget == attempt.get("input_estimate"):
                packed["context_budget_attempt_id"] = attempt_id
                packed.pop("context_budget")
                break
    return packed


def expand_step_metrics(metrics: dict) -> dict:
    """读取 v1/v2，返回完整兼容视图；未知用量仍为 None，绝不猜最终有效请求。"""
    version = metrics.get("schema_version", 1)
    if version not in (1, 2):
        raise ValueError(f"不支持的 step metrics schema：{version!r}")
    expanded = copy.deepcopy(metrics)
    if version == 1:
        return expanded
    expanded["schema_version"] = 1
    attempts = expanded.setdefault("attempts", [])
    for attempt in attempts:
        attempt["normalized_usage"] = normalize_usage(attempt.get("usage"))
    expanded.update(aggregate_attempts(attempts))
    for key in _STEP_COUNT_KEYS:
        expanded.setdefault(key, 0)
    for key in _ZERO_TIME_KEYS:
        expanded.setdefault(key, 0.0)
    expanded["retry_count"] = sum(expanded[key] for key in _RETRY_KEYS)
    accepted_id = expanded.get("final_action_attempt_id")
    if accepted_id is not None:
        accepted = _referenced_attempt(attempts, accepted_id)
        if (accepted is None or accepted.get("purpose", "action") != "action"
                or accepted.get("status") != "success"):
            raise ValueError("最终动作指标引用无效")
        expanded.setdefault("final_action_usage", normalize_usage(accepted.get("usage")))
    else:
        expanded.setdefault("final_action_usage", normalize_usage(None))
    if "context_budget_attempt_id" in expanded:
        budget_id = expanded.pop("context_budget_attempt_id")
        attempt = _referenced_attempt(attempts, budget_id)
        if attempt is None or not isinstance(attempt.get("input_estimate"), dict):
            raise ValueError("上下文预算指标引用无效")
        expanded["context_budget"] = copy.deepcopy(attempt["input_estimate"])
    return expanded


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="只读展开 traj 中的逐步指标，兼容 v1/v2")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--step", type=int, help="只输出指定步骤；省略则输出全部已有指标")
    args = parser.parse_args()
    trajectory = json.loads((args.run_dir / "traj.json").read_text(encoding="utf-8"))
    rows = [{"step": record["step"], "task_index": record.get("task_index", 0),
             "metrics": expand_step_metrics(record["metrics"])}
            for record in trajectory.get("steps", []) if isinstance(record.get("metrics"), dict)
            and (args.step is None or record["step"] == args.step)]
    if args.step is not None and not rows:
        parser.error(f"步骤 {args.step} 没有可读取的指标")
    print(json.dumps(rows, ensure_ascii=False, indent=2))
