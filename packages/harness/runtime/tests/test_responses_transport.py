"""Stateless Responses API replay keeps opaque reasoning outside assistant text."""

import asyncio
import copy

import gui_agent
import mac_agent
import model_metrics
import model_stream


OUTPUT = [
    {"id": "rs_1", "type": "reasoning", "status": "completed", "summary": [],
     "encrypted_content": "opaque-reasoning-token"},
    {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
     "content": [{"type": "output_text", "text": "<tool_call>computer.wait(1)</tool_call>",
                  "annotations": []}]},
]


class FakeResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, output):
        self.output = output

    def json(self):
        return {"status": "completed", "output": self.output,
                "usage": {"input_tokens": 12, "output_tokens": 8,
                          "output_tokens_details": {"reasoning_tokens": 3}}}


class FakeHttp:
    def __init__(self):
        self.requests = []

    async def post(self, url, *, json, headers):
        self.requests.append((url, copy.deepcopy(json)))
        return FakeResponse(copy.deepcopy(OUTPUT))


def test_responses_replays_encrypted_reasoning_item_without_flattening():
    http = FakeHttp()
    client = model_stream.ModelClient(
        base_url="https://api.openai.com/v1", api_key="test", model_name="gpt-6-astra",
        protocol="openai-thinking", timeout=10, retries=0, stream=False,
        reasoning_effort="medium",
    )
    client.client = http
    first = [{"role": "system", "content": "Act"},
             {"role": "user", "content": [{"type": "text", "text": "See"},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    text, reasoning, usage = asyncio.run(client.complete(first, max_tokens=128))
    assert text == "<tool_call>computer.wait(1)</tool_call>"
    assert reasoning == ""
    assert usage["prompt_tokens"] == 12
    assert client.last_response_output == OUTPUT
    next_messages = first + [{"role": "assistant", "content": text,
                              "response_output": client.last_response_output},
                             {"role": "user", "content": "Continue"}]
    asyncio.run(client.complete(next_messages, max_tokens=128))
    url, payload = http.requests[-1]
    assert url == "https://api.openai.com/v1/responses"
    assert payload["store"] is False
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["input"][0]["content"] == [
        {"type": "input_text", "text": "See"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
    ]
    assert payload["input"][1:3] == OUTPUT
    assert payload["input"][3] == {"role": "user", "content": "Continue"}
    assert "opaque-reasoning-token" not in str(payload["input"][3])


def test_gui_history_carries_response_output_as_separate_field():
    turn = gui_agent.Turn(
        step=0, observation_text="Observed", screenshot_path=gui_agent.Path("/unused.png"),
        reasoning="", assistant_response="<tool_call>computer.wait(1)</tool_call>",
        actions=[], execution_feedback="ok", completed_at="now", response_output=OUTPUT,
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
    assert messages[2]["response_output"] == OUTPUT
    assert "opaque-reasoning-token" not in messages[2]["content"]


def test_mac_history_carries_response_output_as_separate_field():
    context = mac_agent.ContextManager(
        task="test", coordinate_range=1000, img_keep_n=1, img_interval=1,
        history_turns=5, micro_compaction=False, micro_trigger_steps=1,
        micro_keep_turns=1, micro_interval=1, todo_diff_snapshot=False,
        max_image_dimension=1000, jpeg_quality=80,
    )
    context.append(mac_agent.Turn(
        step=0, observation_text="Observed", screenshot_path=None,
        intent="Wait", code="pass", execution_feedback="ok", user_response="",
        response_output=OUTPUT,
    ))
    messages = context.build_messages(
        current_feedback="done", current_screenshot=None, current_user_response="",
    )
    assistant = next(message for message in messages if message["role"] == "assistant")
    assert assistant["response_output"] == OUTPUT
    assert "opaque-reasoning-token" not in assistant["content"]


def test_mac_resume_restores_encrypted_response_items(tmp_path):
    context = mac_agent.ContextManager(
        task="test", coordinate_range=1000, img_keep_n=1, img_interval=1,
        history_turns=5, micro_compaction=False, micro_trigger_steps=1,
        micro_keep_turns=1, micro_interval=1, todo_diff_snapshot=False,
        max_image_dimension=1000, jpeg_quality=80,
    )
    step = {"step": 0, "turn": {"step": 0, "observation_text": "Observed",
            "intent": "Wait", "code": "pass", "execution_feedback": "ok",
            "response_output": OUTPUT}}
    trajectory = {"task": "test", "config": {"ui_api": "mai-ui"}, "steps": [step]}
    mac_agent.restore_context_from_trajectory(context, tmp_path, trajectory, [step])
    messages = context.build_messages(
        current_feedback="done", current_screenshot=None, current_user_response="",
    )
    assert next(m for m in messages if m["role"] == "assistant")["response_output"] == OUTPUT


def test_responses_stream_previews_text_and_requires_completed_event():
    class Stream:
        async def aiter_lines(self):
            import json
            for event in (
                {"type": "response.output_text.delta", "delta": "<intent>Wait</intent>"},
                {"type": "response.output_text.delta", "delta": "<code>pass</code>"},
                {"type": "response.completed", "response": {
                    "status": "completed", "output": [OUTPUT[0],
                        {"type": "message", "role": "assistant", "content": [
                            {"type": "output_text", "text": "<intent>Wait</intent><code>pass</code>"}]}],
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                }},
            ):
                yield "data: " + json.dumps(event)
                yield ""

    events = []
    client = model_stream.ModelClient(
        base_url="https://api.openai.com/v1", api_key="test", model_name="gpt-6-astra",
        protocol="openai-thinking", timeout=10, retries=0,
    )
    preview = model_stream._ContentPreview(lambda kind, **kwargs: events.append(kind))
    text, _, _, _ = asyncio.run(client._sse_result(Stream(), preview))
    assert text == "<intent>Wait</intent><code>pass</code>"
    assert events.count("response_started") == 1
    assert events.count("code_delta") > 0


def test_context_estimate_accounts_for_replayed_encrypted_reasoning():
    plain = [{"role": "assistant", "content": "Done"}]
    replay = [{**plain[0], "response_output": OUTPUT}]
    assert model_metrics.estimate_messages_tokens(replay) > model_metrics.estimate_messages_tokens(plain)


def test_unreadable_encrypted_replay_falls_back_once_to_text_history():
    class ErrorResponse:
        status_code = 400
        headers = {"content-type": "application/json"}
        text = 'The encrypted content could not be decrypted or parsed'

        async def aread(self):
            return self.text.encode()

    class FallbackHttp(FakeHttp):
        async def post(self, url, *, json, headers):
            self.requests.append((url, copy.deepcopy(json)))
            if len(self.requests) == 1:
                return ErrorResponse()
            return FakeResponse(copy.deepcopy(OUTPUT))

    http = FallbackHttp()
    client = model_stream.ModelClient(
        base_url="https://api.openai.com/v1", api_key="test", model_name="gpt-6-astra",
        protocol="openai-thinking", timeout=10, retries=0, stream=False,
    )
    client.client = http
    messages = [{"role": "assistant", "content": "Previous action", "response_output": OUTPUT},
                {"role": "user", "content": "Next"}]
    asyncio.run(client.complete(messages, max_tokens=128))
    assert len(http.requests) == 2
    assert http.requests[0][1]["input"][0]["type"] == "reasoning"
    assert all(item.get("type") != "reasoning" for item in http.requests[1][1]["input"])
    assert http.requests[1][1]["input"][0]["type"] == "message"


def test_mac_parse_retry_preserves_rejected_response_reasoning():
    class Client:
        token_estimator = None
        last_response_output = None

        def __init__(self):
            self.calls = []

        async def complete(self, messages, **_kwargs):
            self.calls.append(copy.deepcopy(messages))
            self.last_response_output = copy.deepcopy(OUTPUT)
            if len(self.calls) == 1:
                return "not an action", "", {}
            return "<intent>Wait</intent><code>pass</code>", "", {}

    client = Client()
    asyncio.run(mac_agent.plan_code(
        client, [{"role": "user", "content": "Go"}], max_tokens=128,
        parse_retries=1, require_intent=True,
    ))
    assert client.calls[1][1]["response_output"] == OUTPUT
    assert client.calls[1][2]["role"] == "user"


def test_new_protocol_is_explicit_and_legacy_default_is_unchanged():
    legacy = model_stream.ModelClient(
        base_url="https://example.test/v1", api_key="test", model_name="qwen3.8-max",
        protocol="openai", timeout=10, retries=0,
    )
    assert legacy.endpoint() == "https://example.test/v1/chat/completions"
    assert gui_agent.build_parser().parse_args(
        ["task", "--llm-protocol", "openai-thinking"]
    ).llm_protocol == "openai-thinking"
    assert mac_agent.build_parser().parse_args(
        ["--llm-protocol", "openai-thinking"]
    ).llm_protocol == "openai-thinking"


def test_thinking_protocol_preserves_requested_xhigh_effort_on_routify():
    http = FakeHttp()
    client = model_stream.ModelClient(
        base_url="https://routify.example/protocol/openai/v1", api_key="test",
        model_name="gpt-6-astra", protocol="openai-thinking", timeout=10,
        retries=0, stream=False, reasoning_effort="xhigh",
    )
    client.client = http
    asyncio.run(client.complete([{"role": "user", "content": "Go"}], max_tokens=128))
    assert http.requests[0][1]["reasoning"] == {"effort": "xhigh"}
