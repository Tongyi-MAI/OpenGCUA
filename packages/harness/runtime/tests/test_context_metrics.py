"""本地上下文估算分项：只解释同一启发式总量，不改变总量、校准锚点或待发送消息。"""
from __future__ import annotations

import copy
import json
from typing import Any

import pytest

import model_metrics
from model_metrics import (
    TokenEstimator, count_message_images, estimate_messages_components, estimate_messages_tokens,
)


def _baseline_estimate(messages: list[dict], image_token_estimate: int = 1600) -> int:
    """基线 64a8857 的 estimate_messages_tokens 原样副本，用于确认总量逐值不变。"""
    def text_tokens(text: str) -> int:
        ascii_count = sum(1 for char in text if ord(char) < 128)
        return (ascii_count + 3) // 4 + len(text) - ascii_count

    def content_tokens(content: Any) -> int:
        if isinstance(content, str):
            return text_tokens(content)
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
        response_tokens = 0
        if isinstance(output, list):
            response_tokens = sum(
                text_tokens(item["encrypted_content"])
                for item in output if isinstance(item, dict)
                and item.get("type") == "reasoning"
                and isinstance(item.get("encrypted_content"), str)
            )
        native = message.get("anthropic_content")
        anthropic_tokens = 0
        if isinstance(native, list):
            anthropic_tokens = sum(
                text_tokens(value)
                for block in native if isinstance(block, dict)
                and block.get("type") in ("thinking", "redacted_thinking")
                for value in (block.get("thinking"), block.get("signature"), block.get("data"))
                if isinstance(value, str)
            )
        return response_tokens + anthropic_tokens

    return 2 + sum(4 + content_tokens(message.get("content")) + replay_tokens(message)
                   for message in messages)


class _NoTouch(dict):
    """图片 URL/base64 载体：被读取即失败，证明估算只按类型判别。"""
    def __getitem__(self, key):
        raise AssertionError("图像数据不应被读取")

    def values(self):
        raise AssertionError("图像数据不应被遍历")


IMAGE_VARIANTS = [
    {"type": "image_ref", "path": "/tmp/a.png"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5000}},
    {"type": "image", "source": {"type": "base64", "data": "B" * 5000}},
    {"type": "input_image", "image_url": "data:image/jpeg;base64," + "C" * 5000},
    {"image_ref": "screenshots/step_001.png"},
    {"image_url": {"url": "https://example.invalid/x.png"}},
]

RESPONSES_REPLAY = [
    {"type": "reasoning", "encrypted_content": "gAAAA" + "x" * 397, "summary": []},
    {"type": "message", "content": [{"type": "output_text", "text": "visible"}]},
    {"type": "reasoning", "encrypted_content": None},
]
ANTHROPIC_REPLAY = [
    {"type": "thinking", "thinking": "推理过程 step", "signature": "sig" * 30},
    {"type": "redacted_thinking", "data": "opaque-" * 20},
    {"type": "text", "text": "answer"},
]

CASES = {
    "empty": [],
    "ascii": [{"role": "system", "content": "abc"}, {"role": "user", "content": "hello world"}],
    "unicode": [{"role": "user", "content": "核实上次提交 ✅ émoji 🙂 ascii"}],
    "parts": [{"role": "user", "content": [{"type": "text", "text": "看图"}, *IMAGE_VARIANTS,
                                           {"type": "text", "text": 42}, {"extra": ["深层", {"k": "v"}]}]}],
    "tuple_and_none": [{"role": "user", "content": ("a", None, 3.5)}, {"role": "assistant"}],
    "responses": [{"role": "assistant", "content": "done", "response_output": RESPONSES_REPLAY}],
    "anthropic": [{"role": "assistant", "content": "done", "anthropic_content": ANTHROPIC_REPLAY}],
    "both_providers": [{"role": "assistant", "content": [{"type": "text", "text": "x"}],
                        "response_output": RESPONSES_REPLAY, "anthropic_content": ANTHROPIC_REPLAY}],
    "malformed_replay": [{"role": "assistant", "content": "x", "response_output": "not-a-list",
                          "anthropic_content": {"type": "thinking"}}],
}


@pytest.mark.parametrize("name", sorted(CASES))
@pytest.mark.parametrize("image_estimate", [1, 1600, 2048])
def test_components_sum_exactly_to_unchanged_baseline_total(name, image_estimate):
    messages = CASES[name]
    before = copy.deepcopy(messages)
    components = estimate_messages_components(messages, image_estimate)
    assert set(components) == {"text", "images", "opaque_reasoning", "overhead"}
    assert all(type(value) is int and value >= 0 for value in components.values())
    expected = _baseline_estimate(messages, image_estimate)
    assert sum(components.values()) == expected == estimate_messages_tokens(messages, image_estimate)
    assert components["overhead"] == 2 + 4 * len(messages)
    assert messages == before


def test_unicode_counts_each_non_ascii_char_and_rounds_ascii_up():
    components = estimate_messages_components([{"role": "user", "content": "中文🙂ab"}])
    # 3 个非 ASCII + ceil(2/4)=1
    assert components == {"text": 4, "images": 0, "opaque_reasoning": 0, "overhead": 6}


def test_image_variants_count_by_type_without_reading_payload():
    guarded = [{"type": "image_url", "image_url": _NoTouch(url="data:" + "Z" * 9999)},
               {"type": "input_image", "image_url": _NoTouch()},
               {"image_ref": _NoTouch()}]
    messages = [{"role": "user", "content": [*IMAGE_VARIANTS, *guarded, {"type": "text", "text": "hi"}]}]
    components = estimate_messages_components(messages, 1000)
    assert components["images"] == 9 * 1000
    assert components["text"] == 1
    assert count_message_images(messages) == 9
    assert count_message_images([{"role": "user", "content": "no images"}, {"role": "system"}]) == 0


def test_opaque_reasoning_only_counts_provider_replay_not_visible_text():
    responses = estimate_messages_components(CASES["responses"])
    assert responses["opaque_reasoning"] == (402 + 3) // 4
    assert responses["text"] == 1  # "done"
    anthropic = estimate_messages_components(CASES["anthropic"])
    # thinking "推理过程 step" = 4 非 ASCII + ceil(5/4)；signature 90/4；data 140/4
    assert anthropic["opaque_reasoning"] == (4 + 2) + (90 + 3) // 4 + (140 + 3) // 4
    assert anthropic["text"] == 1
    # 可见 output_text/text 块不进入 opaque，也不重复计入 text。
    plain = estimate_messages_components([{"role": "assistant", "content": "done"}])
    assert anthropic["text"] == responses["text"] == plain["text"]


def test_zero_and_invalid_image_estimate_are_rejected_like_baseline():
    for bad in (0, -1, True, 1.5, "1600"):
        with pytest.raises(ValueError):
            estimate_messages_components([], bad)
        with pytest.raises(ValueError):
            estimate_messages_tokens([], bad)


def _estimator_with_anchor(messages, usage):
    estimator = TokenEstimator(image_token_estimate=1600, scope="test")
    estimator.observe(copy.deepcopy(messages), usage, purpose="action", attempt_id="a1")
    return estimator


def test_estimate_attaches_heuristic_components_without_faking_calibrated_breakdown():
    sent = [{"role": "system", "content": "system " * 40},
            {"role": "user", "content": [{"type": "text", "text": "观察"}, IMAGE_VARIANTS[0]]},
            {"role": "assistant", "content": "ok", "response_output": RESPONSES_REPLAY}]
    estimator = _estimator_with_anchor(sent, {"prompt_tokens": 5000, "completion_tokens": 7})
    state_before = estimator.state_dict()
    longer = sent + [{"role": "user", "content": "新的反馈 more text"}]
    snapshot = copy.deepcopy(longer)
    result = estimator.estimate(longer, purpose="action", reserve_tokens=100, context_limit=9000)
    assert result["source"] == "usage_delta"
    assert result["components_source"] == "heuristic"
    assert sum(result["components"].values()) == result["heuristic_input_tokens"]
    assert result["heuristic_input_tokens"] == _baseline_estimate(longer)
    # 校准总量与基线公式一致；分项不按比例缩放成“精确”实际值。
    expected_input = 5000 + _baseline_estimate(longer) - _baseline_estimate(sent)
    assert result["input_tokens"] == expected_input != sum(result["components"].values())
    assert result["total_tokens"] == expected_input + 100
    # 纯读取：锚点、消息与 provider 原始回放均不变，结果可 JSON 落盘。
    assert estimator.state_dict() == state_before
    assert longer == snapshot
    json.dumps(result)
    result["components"]["text"] = -1
    assert estimator.estimate(longer)["components"]["text"] >= 0


@pytest.mark.parametrize("usage", [None, {}, {"completion_tokens": 3}, {"prompt_tokens": -1},
                                   {"prompt_tokens": True}])
def test_missing_usage_keeps_heuristic_source_and_components(usage):
    messages = CASES["unicode"]
    estimator = _estimator_with_anchor(messages, usage)
    assert estimator.state_dict()["anchors"] == {}
    result = estimator.estimate(messages)
    assert result["source"] == "heuristic" and result["reason"] == "no_anchor"
    assert result["input_tokens"] == result["heuristic_input_tokens"] == sum(result["components"].values())


def test_zero_usage_anchor_falls_back_without_changing_components():
    messages = CASES["ascii"]
    estimator = _estimator_with_anchor(messages, {"prompt_tokens": 0})
    result = estimator.estimate(messages)
    assert result["reason"] == "non_positive_usage_delta"
    assert result["input_tokens"] == sum(result["components"].values())


def test_state_dict_schema_is_unchanged_by_components():
    estimator = _estimator_with_anchor(CASES["ascii"], {"input_tokens": 10})
    state = estimator.state_dict()
    restored = TokenEstimator(image_token_estimate=1600, scope="test")
    assert restored.load_state(copy.deepcopy(state))
    assert "components" not in json.dumps(state)
    assert restored.estimate(CASES["ascii"]) == estimator.estimate(CASES["ascii"])


def test_compact_step_metrics_still_deduplicates_budget_with_components():
    budget = TokenEstimator().estimate(CASES["parts"], reserve_tokens=10)
    metrics = {"attempts": [{"attempt_id": "x", "purpose": "action", "input_estimate": copy.deepcopy(budget)}],
               "context_budget": copy.deepcopy(budget)}
    packed = model_metrics.compact_step_metrics(copy.deepcopy(metrics))
    assert "context_budget" not in packed and packed["context_budget_attempt_id"] == "x"
    assert model_metrics.expand_step_metrics(packed)["context_budget"] == budget
