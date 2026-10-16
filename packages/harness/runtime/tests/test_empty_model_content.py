"""Regression coverage for reasoning-only GUI model responses."""

from __future__ import annotations

import asyncio
import copy

import pytest

import model_stream


class FakeResponse:
    status_code = 200
    headers = {"content-type": "application/json"}
    text = ""

    def __init__(self, content: str, reasoning: str = "") -> None:
        self.content = content
        self.reasoning = reasoning

    def json(self):
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": self.content,
                    "reasoning_content": self.reasoning,
                },
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        }


class FakeHttp:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.payloads: list[dict] = []

    async def post(self, url, *, json, headers):
        self.payloads.append(copy.deepcopy(json))
        return self.responses.pop(0)


def make_client(http: FakeHttp, *, model: str = "deepseek-flash", retries: int = 1,
                salvage: bool = False):
    client = model_stream.ModelClient(
        base_url="https://example.test/v1", api_key="test", model_name=model,
        timeout=10, retries=retries, stream=False, reasoning_effort="low",
        reasoning_code_salvage=salvage,
    )
    client.client = http
    return client


def test_deepseek_reasoning_only_retries_with_thinking_off():
    http = FakeHttp([
        FakeResponse("", "<tool_call>pyautogui.click(1, 2)</tool_call>"),
        FakeResponse("<tool_call>pyautogui.click(1, 2)</tool_call>"),
    ])
    client = make_client(http)
    content, _, _ = asyncio.run(client.complete(
        [{"role": "user", "content": "Click"}], max_tokens=128,
    ))
    assert content.startswith("<tool_call>")
    assert [p["reasoning_effort"] for p in http.payloads] == ["low", "none"]
    assert len(client.last_call_attempts) == 2
    first = client.last_call_attempts[0]
    assert first["response_content_chars"] == 0
    assert first["response_reasoning_chars"] > 0
    assert first["retry_strategy"] == "reasoning_effort_none"
    assert first["error"].startswith("EmptyModelContent:")


def test_repeated_empty_response_keeps_attempts_and_never_executes_reasoning():
    http = FakeHttp([FakeResponse("", "hidden action"), FakeResponse("", "hidden action")])
    client = make_client(http)
    with pytest.raises(model_stream.AgentError, match="empty content") as excinfo:
        asyncio.run(client.complete([{"role": "user", "content": "Click"}], max_tokens=128))
    assert len(excinfo.value.attempts) == 2
    assert all(a["response_content_chars"] == 0 for a in excinfo.value.attempts)
    assert [p["reasoning_effort"] for p in http.payloads] == ["low", "none"]


def test_other_model_gets_final_content_reminder():
    http = FakeHttp([FakeResponse("", "thinking"), FakeResponse("Done")])
    client = make_client(http, model="another-model")
    content, _, _ = asyncio.run(client.complete(
        [{"role": "user", "content": "Start"}], max_tokens=128,
    ))
    assert content == "Done"
    assert http.payloads[0]["reasoning_effort"] == "low"
    assert http.payloads[1]["reasoning_effort"] == "low"
    assert len(http.payloads[1]["messages"]) == 2
    assert "normal content field" in http.payloads[1]["messages"][-1]["content"]


def test_terminal_reasoning_code_is_salvaged_only_when_complete():
    final_code = '<intent>Inspect the workbook</intent><code>print("ok")</code>'
    http = FakeHttp([FakeResponse("", "Thinking aloud.\n" + final_code)])
    client = make_client(http, retries=0, salvage=True)
    content, _, _ = asyncio.run(client.complete(
        [{"role": "user", "content": "Inspect"}], max_tokens=128,
    ))
    assert content == final_code
    assert len(http.payloads) == 1
    assert client.last_call_attempts[0]["response_salvage"] == "terminal_reasoning_code"
    assert client.last_call_attempts[0]["response_original_content_chars"] == 0


@pytest.mark.parametrize("reasoning", [
    '<intent>One</intent><code>print(1)</code><intent>Two</intent><code>print(2)</code>',
    '<intent>One</intent><code>print(1)</code> actually never mind',
    '<intent>One</intent><code>not valid Python !!</code>',
    '<intent>One</intent><code>print(1)</code></｜｜DSML｜｜ calls>',
])
def test_reasoning_salvage_rejects_ambiguous_or_malformed_code(reasoning):
    assert model_stream.terminal_reasoning_code(reasoning) is None
