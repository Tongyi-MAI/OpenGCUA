"""Native Anthropic thinking blocks must survive the next Messages request."""

import asyncio
import argparse
import copy
import hashlib
import json

import gui_agent
import llm_entry
import mac_agent
import model_metrics
import model_stream


SIGNED_CONTENT = [
    {"type": "thinking", "thinking": "I should check the arithmetic.", "signature": "signed-1"},
    {"type": "redacted_thinking", "data": "opaque-redacted"},
    {"type": "text", "text": "The answer is 42."},
]


class Response:
    status_code = 200
    headers = {"content-type": "application/json"}

    def json(self):
        return {"type": "message", "content": copy.deepcopy(SIGNED_CONTENT),
                "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 20}}


class Http:
    def __init__(self):
        self.requests = []

    async def post(self, url, *, json, headers):
        self.requests.append(copy.deepcopy(json))
        return Response()


def test_anthropic_json_round_trips_signed_and_redacted_blocks_unchanged():
    """Dropping either opaque block from the next assistant turn breaks continuity."""
    http = Http()
    client = model_stream.ModelClient(
        protocol="anthropic", base_url="https://api.anthropic.com/v1",
        api_key="test", model_name="claude-test", timeout=10, retries=0, stream=False,
    )
    client.client = http
    first = [{"role": "user", "content": "What is six times seven?"}]
    text, reasoning, _ = asyncio.run(client.complete(first, max_tokens=256))
    assert text == "The answer is 42."
    assert reasoning == "I should check the arithmetic."
    assert client.last_anthropic_content == SIGNED_CONTENT
    second = first + [
        {"role": "assistant", "content": text, "anthropic_content": client.last_anthropic_content},
        {"role": "user", "content": "Now add one."},
    ]
    asyncio.run(client.complete(second, max_tokens=256))
    assert http.requests[1]["messages"][1] == {"role": "assistant", "content": SIGNED_CONTENT}
    attempt = client.last_call_attempts[0]
    fingerprint = hashlib.sha256(b"signed-1").hexdigest()[:16]
    assert attempt["anthropic_thinking_sent"]["signature_fingerprints"] == [fingerprint]
    assert attempt["anthropic_thinking_received"]["signature_fingerprints"] == [fingerprint]


def test_anthropic_stream_assembles_signature_delta_by_block_index():
    """A stream must retain the final signature, not just thinking_delta text."""
    class Stream:
        async def aiter_lines(self):
            events = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "thinking", "thinking": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "thinking_delta", "thinking": "I should check "}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "thinking_delta", "thinking": "the arithmetic."}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "signature_delta", "signature": "signed-1"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "content_block_start", "index": 1,
                 "content_block": {"type": "redacted_thinking", "data": "opaque-redacted"}},
                {"type": "content_block_stop", "index": 1},
                {"type": "content_block_start", "index": 2,
                 "content_block": {"type": "text", "text": "The answer"}},
                {"type": "content_block_delta", "index": 2,
                 "delta": {"type": "text_delta", "text": " is 42."}},
                {"type": "content_block_stop", "index": 2},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"output_tokens": 20}},
                {"type": "message_stop"},
            ]
            for event in events:
                yield "event: " + event["type"]
                yield "data: " + json.dumps(event)
                yield ""

    client = model_stream.ModelClient(
        protocol="anthropic", base_url="https://api.anthropic.com/v1",
        api_key="test", model_name="claude-test", timeout=10, retries=0,
    )
    text, reasoning, _, _ = asyncio.run(
        client._anthropic_sse_result(Stream(), model_stream._TextPreview())
    )
    assert text == "The answer is 42."
    assert reasoning == "I should check the arithmetic."
    assert client._anthropic_content_candidate == SIGNED_CONTENT


def test_gui_history_keeps_native_anthropic_blocks_separate_from_text():
    turn = gui_agent.Turn(
        step=0, observation_text="Observed", screenshot_path=gui_agent.Path("/unused.png"),
        reasoning="I should check the arithmetic.", assistant_response="<think>summary</think>\naction",
        actions=[], execution_feedback="ok", completed_at="now",
        anthropic_content=SIGNED_CONTENT,
    )
    original = gui_agent.image_path_to_data_url
    gui_agent.image_path_to_data_url = lambda *_args, **_kwargs: "data:image/jpeg;base64,AA=="
    try:
        messages = gui_agent.build_messages(
            system_prompt="Act", turns=[turn], current_text="Next",
            current_image=gui_agent.Image.new("RGB", (1, 1)), history_n=1,
            jpeg_quality=85,
        )
    finally:
        gui_agent.image_path_to_data_url = original
    _, native = model_stream.anthropic_messages(messages)
    assert native[1] == {"role": "assistant", "content": SIGNED_CONTENT}


def test_mac_resume_restores_native_anthropic_blocks(tmp_path):
    context = mac_agent.ContextManager(
        task="test", coordinate_range=1000, img_keep_n=1, img_interval=1,
        history_turns=5, micro_compaction=False, micro_trigger_steps=1,
        micro_keep_turns=1, micro_interval=1, todo_diff_snapshot=False,
        max_image_dimension=1000, jpeg_quality=80,
    )
    step = {"step": 0, "turn": {"step": 0, "observation_text": "Observed",
            "intent": "Wait", "code": "pass", "execution_feedback": "ok",
            "anthropic_content": SIGNED_CONTENT}}
    trajectory = {"task": "test", "config": {"ui_api": "mai-ui"}, "steps": [step]}
    mac_agent.restore_context_from_trajectory(context, tmp_path, trajectory, [step])
    messages = context.build_messages(
        current_feedback="done", current_screenshot=None, current_user_response="",
    )
    _, native = model_stream.anthropic_messages(messages)
    assert next(m for m in native if m["role"] == "assistant")["content"] == SIGNED_CONTENT


def test_config_opt_in_sends_adaptive_thinking_without_chat_reasoning_effort(monkeypatch):
    """The documented local.json key must affect the actual Messages payload."""
    cfg = {
        "api_key": "test", "llm_protocol": "anthropic", "model_name": "claude-test",
        "llm_base_url": "https://api.anthropic.com/v1",
        "anthropic_thinking": {"type": "adaptive"},
    }
    monkeypatch.setattr(llm_entry, "load_llm_config", lambda: cfg)
    parser = argparse.ArgumentParser()
    llm_entry.add_llm_cli_args(parser)
    args = parser.parse_args(["--no-stream", "--api-retries", "0"])
    settings = llm_entry.resolve_llm_settings(args)
    client = llm_entry.build_model_client(settings)
    http = Http()
    client.client = http
    asyncio.run(client.complete([{"role": "user", "content": "Hello"}], max_tokens=256))
    assert http.requests[0]["thinking"] == {"type": "adaptive"}
    assert "reasoning_effort" not in http.requests[0]


def test_invalid_signature_retries_once_without_opaque_thinking_blocks():
    """Compacted history can invalidate signatures; retry only that precise 400."""
    class InvalidSignatureResponse:
        status_code = 400
        headers = {"content-type": "application/json"}
        text = ('messages.1.content.0: Invalid `signature` in `thinking` block. '
                'The block is bound to a different conversation.')

        async def aread(self):
            return self.text.encode()

    class PrefixSensitiveHttp(Http):
        async def post(self, url, *, json, headers):
            self.requests.append(copy.deepcopy(json))
            return InvalidSignatureResponse() if len(self.requests) == 1 else Response()

    http = PrefixSensitiveHttp()
    client = model_stream.ModelClient(
        protocol="anthropic", base_url="https://api.anthropic.com/v1",
        api_key="test", model_name="claude-test", timeout=10, retries=0, stream=False,
    )
    client.client = http
    history = [
        {"role": "user", "content": "Prior question"},
        {"role": "assistant", "content": "The answer is 42.",
         "anthropic_content": SIGNED_CONTENT},
        {"role": "user", "content": "Continue"},
    ]
    text, _, _ = asyncio.run(client.complete(history, max_tokens=256))
    assert text == "The answer is 42."
    assert len(http.requests) == 2
    assert http.requests[0]["messages"][1]["content"] == SIGNED_CONTENT
    assert http.requests[1]["messages"][1]["content"] == [{"type": "text", "text": "The answer is 42."}]
    assert client.last_call_attempts[0]["retry_strategy"] == "drop_invalid_anthropic_thinking"


def test_mac_parse_retry_replays_rejected_signed_response():
    """A malformed action must keep its signed thinking during parse recovery."""
    class Client:
        token_estimator = None
        last_response_output = None

        def __init__(self):
            self.calls = []
            self.last_anthropic_content = None

        async def complete(self, messages, **_kwargs):
            self.calls.append(copy.deepcopy(messages))
            self.last_anthropic_content = copy.deepcopy(SIGNED_CONTENT)
            if len(self.calls) == 1:
                return "not an action", "I should check the arithmetic.", {}
            return "<intent>Wait</intent><code>pass</code>", "", {}

    client = Client()
    asyncio.run(mac_agent.plan_code(
        client, [{"role": "user", "content": "Go"}], max_tokens=128,
        parse_retries=1, require_intent=True,
    ))
    assert client.calls[1][1]["anthropic_content"] == SIGNED_CONTENT


def test_anthropic_diagnostic_shows_replay_fingerprints_without_opaque_values():
    """A local request snapshot must be auditable without exposing signatures."""
    messages = [
        {"role": "system", "content": "Act"},
        {"role": "user", "content": "Previous observation"},
        {"role": "assistant", "content": "The answer is 42.",
         "anthropic_content": SIGNED_CONTENT},
        {"role": "user", "content": "Continue"},
    ]
    payload = gui_agent.anthropic_diagnostic_payload(
        messages, model="claude-test", max_tokens=256, stream=True,
        thinking={"type": "adaptive"},
    )
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["thinking_replay"]["thinking_blocks"] == 1
    assert payload["thinking_replay"]["redacted_thinking_blocks"] == 1
    assert payload["thinking_replay"]["signature_fingerprints"] == [
        hashlib.sha256(b"signed-1").hexdigest()[:16]
    ]
    assert payload["messages"][1]["content"][0]["type"] == "thinking"
    serialized = json.dumps(payload)
    assert "signed-1" not in serialized
    assert "opaque-redacted" not in serialized
    assert "I should check the arithmetic." not in serialized


def test_context_estimate_includes_anthropic_opaque_replay():
    """Opaque signatures must not disappear from the soft context budget."""
    plain = [{"role": "assistant", "content": "The answer is 42."}]
    signed = [{**plain[0], "anthropic_content": SIGNED_CONTENT}]
    assert model_metrics.estimate_messages_tokens(signed) > model_metrics.estimate_messages_tokens(plain)
