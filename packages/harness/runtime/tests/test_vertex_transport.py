"""Vertex Gemini transport preserves the existing action/usage contract."""

from __future__ import annotations

import asyncio

import model_stream


class FakeResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def json(self):
        return {
            "candidates": [{
                "content": {"parts": [{"text": "<tool_call>computer.wait()</tool_call>"}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {
                "promptTokenCount": 123,
                "candidatesTokenCount": 12,
                "thoughtsTokenCount": 3,
                "cachedContentTokenCount": 20,
            },
        }


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def post(self, url, *, json, headers):
        self.calls.append((url, json, headers))
        return FakeResponse()


def test_vertex_payload_image_and_usage():
    http = FakeHttp()
    client = model_stream.ModelClient(
        base_url="https://example.test/protocol/vertex/v1beta",
        api_key="test", model_name="gemini-3.8-flash", protocol="vertex",
        timeout=10, retries=0, stream=True,
    )
    client.client = http
    text, reasoning, usage = asyncio.run(client.complete([
        {"role": "system", "content": "Use tool_call."},
        {"role": "user", "content": [
            {"type": "text", "text": "Observe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
    ], max_tokens=128))
    assert text == "<tool_call>computer.wait()</tool_call>"
    assert reasoning == ""
    assert usage["prompt_tokens"] == 123
    assert usage["completion_tokens"] == 15
    assert client.stream is False
    url, payload, headers = http.calls[0]
    assert url.endswith("/models/gemini-3.8-flash:generateContent")
    assert payload["systemInstruction"]["parts"][0]["text"] == "Use tool_call."
    assert payload["contents"][0]["parts"][1]["inlineData"]["data"] == "AAAA"
    assert payload["generationConfig"]["maxOutputTokens"] == 128
    assert headers["Authorization"] == "Bearer test"


def test_anthropic_usage_includes_cached_and_thinking_tokens():
    usage = model_stream._anthropic_usage({
        "input_tokens": 10,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 30,
        "output_tokens": 40,
        "output_tokens_details": {"thinking_tokens": 25},
    })
    assert usage["prompt_tokens"] == 60
    assert usage["completion_tokens"] == 40
    assert usage["prompt_tokens_details"]["cached_tokens"] == 30
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 25
