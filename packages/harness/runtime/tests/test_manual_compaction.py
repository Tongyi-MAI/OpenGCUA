from __future__ import annotations

import asyncio

import pytest

from llm_compaction import CompactionConfig, CompactionState, SimpleContext, maybe_compact, records_digest, restore_state


SUMMARY = "<summary><constraints>c</constraints><progress>p</progress><findings>f</findings><state>s</state></summary>"


class SummaryClient:
    on_compaction_event = None
    last_attempt_id = "test-summary"

    def __init__(self, response=SUMMARY):
        self.calls = 0
        self.response = response

    async def complete(self, *_args, **_kwargs):
        self.calls += 1
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response, "", {}


def context(*, enabled=False, count=6, old_state=None, micro=False):
    turns = ["history %d " % index + "x " * 850 for index in range(count)]
    records = [{"step": index, "text": text} for index, text in enumerate(turns)]
    config = CompactionConfig(enabled=enabled, trigger_tokens=200000, max_tokens=256, retries=0)
    holder = {}

    def build_step_input(**_kwargs):
        state = holder["context"].compaction_state
        visible = turns[state.cutoff:]
        return [{"role": "system", "content": "system"},
                {"role": "user", "content": (state.summary + "\n" if state.summary else "") + "\n".join(visible)}]

    instance = SimpleContext(
        turns=turns, compaction_config=config, build_step_input=build_step_input,
        records=lambda start, end: records[start:end], compaction_state=old_state,
    )
    instance.micro_compaction = micro
    instance.micro_keep_turns = 1
    instance.micro_cutoff = 1 if micro else 0
    holder["context"] = instance
    return instance


def test_manual_below_threshold_runs_even_when_auto_disabled_and_auto_does_not():
    state = context(enabled=False)
    client = SummaryClient()
    assert asyncio.run(maybe_compact(state, client, current={}, action_max_tokens=128)) is None
    assert client.calls == 0
    result = asyncio.run(maybe_compact(state, client, current={}, action_max_tokens=128, trigger="manual"))
    assert result["status"] == "completed", result
    assert result["trigger_reasons"] == ["manual"]
    assert client.calls == 1
    assert state.compaction_state.summary == SUMMARY
    assert state.compaction_state.cutoff > 0
    assert state.compaction_state.source_digest == records_digest(state.compaction_records(0, state.compaction_state.cutoff))


def test_manual_insufficient_history_does_not_call_model():
    state = context(count=1)
    client = SummaryClient()
    result = asyncio.run(maybe_compact(state, client, current={}, action_max_tokens=128, trigger="manual"))
    assert result["status"] == "failed"
    assert result["blocked"]
    assert client.calls == 0


@pytest.mark.parametrize("response", [RuntimeError("offline"), "<bad/>", "<summary><constraints>" + "z " * 12000 + "</constraints><progress>p</progress><findings>f</findings><state>s</state></summary>"], ids=["transport", "invalid_xml", "too_large"])
def test_manual_failure_restores_previous_summary_and_micro_boundary(response):
    prior = CompactionState(SUMMARY, 1, 2, "old-digest")
    state = context(old_state=prior, micro=True)
    client = SummaryClient(response)
    result = asyncio.run(maybe_compact(state, client, current={}, action_max_tokens=128, trigger="manual"))
    assert result["status"] == "failed"
    assert state.compaction_state is prior
    assert state.micro_cutoff == 1
    assert len(state.turns) == 6


def test_manual_cancellation_restores_previous_state():
    prior = CompactionState(SUMMARY, 1, 2, "old-digest")
    state = context(old_state=prior, micro=True)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(maybe_compact(state, SummaryClient(asyncio.CancelledError()), current={},
                                  action_max_tokens=128, trigger="manual"))
    assert state.compaction_state is prior
    assert state.micro_cutoff == 1


def test_manual_does_not_commit_when_summary_fails_to_shorten_next_input():
    state = context()
    state._build_step_input = lambda **_kwargs: [{"role": "user", "content": "same input " * 1000}]
    old = state.compaction_state
    result = asyncio.run(maybe_compact(state, SummaryClient(), current={}, action_max_tokens=128, trigger="manual"))
    assert result["status"] == "failed"
    assert "未严格缩短" in result["error"]
    assert state.compaction_state is old


def test_server_limit_trigger_still_attempts_below_soft_threshold_when_enabled():
    state = context(enabled=True)
    client = SummaryClient()
    result = asyncio.run(maybe_compact(state, client, current={}, action_max_tokens=128, trigger="server_limit"))
    assert result["trigger_reasons"] == ["server_context_limit"]
    assert client.calls == 1


def test_manual_summary_is_used_with_auto_disabled_in_real_context_manager():
    from mac_agent import ContextManager, Turn

    manager = ContextManager(
        task="original", coordinate_range=1000, img_keep_n=0, img_interval=10,
        history_turns=1, micro_compaction=False, micro_trigger_steps=5,
        micro_keep_turns=1, micro_interval=5, todo_diff_snapshot=False,
        max_image_dimension=1200, jpeg_quality=80,
        compaction_config=CompactionConfig(enabled=False),
    )
    for index in range(3):
        manager.append(Turn(index, "", None, "observe", "print(1)", "old output " + str(index), "", task="original"))
    manager.compaction_state = CompactionState(SUMMARY, 2, 1, records_digest(manager.compaction_records(0, 2)))
    assert restore_state(manager.compaction_state.to_dict(), manager.compaction_records(0, 3)).summary == SUMMARY
    messages = manager.build_step_input(current_feedback="new", current_screenshot=None, current_user_response="")
    serialized = str(messages)
    assert "# Previous summary" in serialized
    assert "old output 0" not in serialized
    assert "old output 1" not in serialized
    assert "old output 2" in serialized
