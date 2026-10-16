import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import gui_agent
from llm_compaction import CompactionConfig, CompactionState, maybe_compact
from model_metrics import TokenEstimator


def completed(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class GuiInputSafetyTests(unittest.TestCase):
    def test_tool_call_parser_accepts_canonical_and_legacy_closing_tags(self):
        actions = gui_agent.parse_actions(
            '<tool_call>pyautogui.click(500, 300)</tool_call>\n'
            '<tool_call>computer.terminate(status="success", answer="你好")</call>'
        )

        self.assertEqual([action.kind for action in actions], ["pyautogui", "terminate"])
        self.assertEqual(actions[-1].status, "success")
        self.assertEqual(actions[-1].answer, "你好")

    def test_input_preflight_is_opt_in(self):
        args = gui_agent.build_parser().parse_args(["你好"])

        self.assertFalse(args.verify_input_preflight)
        self.assertEqual(args.action_pause, 0.1)
        self.assertNotIn("</call>", gui_agent.SYSTEM_PROMPT)

    def test_gui_parser_uses_shared_llm_configuration(self):
        args = gui_agent.build_parser().parse_args([
            "你好", "--context-limit", "12345", "--no-stream", "--api-key", "cli-key",
        ])
        settings = gui_agent.llm_entry.resolve_llm_settings(args)
        self.assertEqual(settings["context_limit"], 12345)
        self.assertEqual(settings["api_key"], "cli-key")
        self.assertFalse(settings["stream"])

    def test_empty_api_key_does_not_corrupt_redacted_text(self):
        with patch.object(gui_agent, "_ACTIVE_API_KEY", ""):
            self.assertEqual(gui_agent.redact_sensitive("ordinary text"), "ordinary text")

    def test_agent_has_no_automatic_app_or_modal_cleanup(self):
        self.assertFalse(hasattr(gui_agent, "dismiss_transient_system_modals"))
        self.assertFalse(hasattr(gui_agent, "dismiss_known_startup_modals"))

    def test_interactive_messages_are_plain_user_roles(self):
        prompt = gui_agent.build_system_prompt(
            input_size=(100, 100), range_scale=1000,
        )
        self.assertNotIn("Immutable Task Instruction", prompt)
        self.assertNotIn("你是单任务代理", prompt)

        prior = gui_agent.Turn(
            step=0,
            observation_text="# Observation Step: 0",
            screenshot_path=gui_agent.Path("/unused.png"),
            reasoning="",
            assistant_response="<tool_call>computer.terminate(status=\"success\")</tool_call>",
            actions=[],
            execution_feedback="",
            completed_at="now",
            user_message="先帮我规划行程",
        )
        messages = gui_agent.build_messages(
            system_prompt=prompt,
            turns=[prior],
            current_text="# Observation Step: 1",
            current_image=gui_agent.Image.new("RGB", (10, 10)),
            history_n=1,
            jpeg_quality=85,
            current_user_message="我选择方案A",
        )

        self.assertEqual(messages[1], {"role": "user", "content": "先帮我规划行程"})
        self.assertEqual(messages[4], {"role": "user", "content": "我选择方案A"})
        self.assertEqual(messages[5]["role"], "user")
        self.assertIn("# Observation Step: 1", messages[5]["content"][0]["text"])

    def test_gui_compaction_candidate_replaces_folded_turns(self):
        image = gui_agent.Image.new("RGB", (8, 8), "white")
        turns = [
            gui_agent.Turn(
                step=index,
                observation_text="observed " + ("abcd " * 250),
                screenshot_path=gui_agent.Path(f"/unused-{index}.png"),
                reasoning="",
                assistant_response="<tool_call>computer.wait(1)</tool_call>",
                actions=[],
                execution_feedback="ok",
                completed_at="now",
                user_message="start" if index == 0 else "",
            )
            for index in range(6)
        ]
        estimator = TokenEstimator(image_token_estimate=1600)
        state = CompactionState()
        context = gui_agent.GuiCompactionContext(
            turns=turns,
            compaction_config=CompactionConfig(
                trigger_tokens=8000, max_tokens=256, image_token_estimate=1600,
            ),
            records=lambda a, b: gui_agent._compaction_records(turns, a, b),
            token_estimator=estimator,
            compaction_state=state,
            rolling_summary=None,
        )

        # Avoid filesystem image reads while preserving the production message shape.
        original = gui_agent.image_path_to_data_url
        gui_agent.image_path_to_data_url = lambda *_a, **_k: "data:image/jpeg;base64,AA=="

        class Client:
            token_estimator = estimator
            last_attempt_id = "compaction-test"
            on_compaction_event = None

            async def complete(self, *_args, **_kwargs):
                return (
                    "<summary><constraints>c</constraints><progress>p</progress>"
                    "<findings>f</findings><state>s</state></summary>",
                    "",
                    {},
                )

        try:
            result = asyncio.run(maybe_compact(
                context,
                Client(),
                current={
                    "system_prompt": "system",
                    "turns": turns,
                    "current_text": "current",
                    "current_image": image,
                    "history_n": 5,
                    "jpeg_quality": 85,
                    "rolling_summary": None,
                    "current_user_message": "latest",
                },
                action_max_tokens=256,
            ))
        finally:
            gui_agent.image_path_to_data_url = original

        self.assertEqual(result["status"], "completed", result)
        self.assertGreater(result["cutoff_after"], 0)
        self.assertLess(
            result["estimated_tokens_after"], result["estimated_tokens_before"]
        )

    def test_ascii_text_uses_clipboard_and_atomic_hotkey(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return completed()

        downs, ups = [], []
        with (
            patch.object(gui_agent.subprocess, "run", side_effect=fake_run),
            patch.object(gui_agent.pyautogui, "keyDown", side_effect=downs.append),
            patch.object(gui_agent.pyautogui, "keyUp", side_effect=ups.append),
            patch.object(gui_agent, "safe_hotkey") as hotkey,
            patch.object(gui_agent.time, "sleep", return_value=None),
        ):
            method = gui_agent.paste_unicode_text("Hello mrdh 12345 . / @")

        self.assertEqual(method, "macos_clipboard_paste")
        self.assertEqual(calls[0][0], ["pbcopy"])
        self.assertEqual(calls[0][1]["input"], "Hello mrdh 12345 . / @")
        hotkey.assert_called_once_with("command", "v")
        self.assertEqual(downs, [])
        self.assertEqual(ups, [])

    def test_safe_hotkey_releases_pressed_keys_on_failure(self):
        downs, ups = [], []

        def key_down(key):
            downs.append(key)
            if key == "v":
                raise RuntimeError("injected failure")

        with (
            patch.object(gui_agent.sys, "platform", "linux"),
            patch.object(gui_agent.pyautogui, "keyDown", side_effect=key_down),
            patch.object(gui_agent.pyautogui, "keyUp", side_effect=ups.append),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                gui_agent.safe_hotkey("command", "v")

        self.assertEqual(downs, ["command", "v"])
        self.assertEqual(ups, ["command"])

    def test_mouse_action_does_not_release_keyboard_modifiers(self):
        record = {"type": "pyautogui", "function": "click", "source": "pyautogui.click(1, 2)",
                  "screen_args": [1, 2], "screen_kwargs": {}}
        with (
            patch.object(gui_agent.pyautogui, "position", return_value=SimpleNamespace(x=0, y=0)),
            patch.object(gui_agent.pyautogui, "click") as click,
            patch.object(gui_agent.pyautogui, "keyUp") as key_up,
        ):
            gui_agent.execute_action(record, dry_run=False, max_wait_seconds=1)
        click.assert_called_once_with(1, 2)
        key_up.assert_not_called()

    def test_execute_write_never_calls_raw_pyautogui_write(self):
        record = {
            "type": "pyautogui",
            "function": "write",
            "source": 'pyautogui.write("abc")',
            "screen_args": ["abc"],
            "screen_kwargs": {},
        }
        with (
            patch.object(gui_agent.pyautogui, "position",
                         return_value=SimpleNamespace(x=10, y=20)),
            patch.object(gui_agent, "paste_unicode_text", return_value="paste:abc"),
            patch.object(gui_agent, "release_all_modifiers"),
            patch.object(gui_agent.pyautogui, "write",
                         side_effect=AssertionError("raw write called")),
        ):
            feedback, stop, status, answer = gui_agent.execute_action(
                record, dry_run=False, max_wait_seconds=1,
            )

        self.assertFalse(stop)
        self.assertIsNone(status)
        self.assertIsNone(answer)
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["input_method"], "paste:abc")
        self.assertIn("paste:abc", feedback)

    def test_raw_pixel_coordinates_are_rejected_instead_of_guessed(self):
        with self.assertRaisesRegex(gui_agent.ActionParseError, "raw screenshot pixels"):
            gui_agent.resolve_axis(
                1440, screen_extent=1512, input_extent=3024, range_scale=1000,
            )

    def test_keyword_double_click_uses_projected_coordinates(self):
        action = gui_agent.ParsedAction(
            kind="pyautogui",
            source="pyautogui.doubleClick(x=500, y=500)",
            function="doubleClick",
            args=[],
            kwargs={"x": 500, "y": 500},
        )
        record = gui_agent.action_to_record(
            action,
            screen_size=(1512, 982),
            input_size=(3024, 1964),
            range_scale=1000,
        )
        with (
            patch.object(gui_agent.pyautogui, "position",
                         return_value=SimpleNamespace(x=10, y=20)),
            patch.object(gui_agent, "mac_multi_click") as click,
            patch.object(gui_agent, "release_all_modifiers"),
        ):
            feedback, stop, status, answer = gui_agent.execute_action(
                record, dry_run=False, max_wait_seconds=1,
            )

        click.assert_called_once_with(756, 490, clicks=2, button="left", interval=0.08)
        self.assertEqual(record["status"], "success")
        self.assertFalse(stop)
        self.assertIsNone(status)
        self.assertIsNone(answer)
        self.assertIn("quartz_multiclick", feedback)

    def test_keydown_and_keyup_are_not_exposed_to_model(self):
        self.assertNotIn("keyDown", gui_agent.GUI_FUNCTIONS)
        self.assertNotIn("keyUp", gui_agent.GUI_FUNCTIONS)


if __name__ == "__main__":
    unittest.main()
