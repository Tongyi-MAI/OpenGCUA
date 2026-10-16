"""Strict compatibility for DeepSeek's alternate final-content action envelope."""

import pytest

import gui_agent


def test_complete_dsml_click_uses_normal_allow_list():
    content = (
        '<｜｜DSML｜｜ calls>\n'
        '<invoke name="pyautogui.click">\n'
        '<parameter name="x">712</parameter>\n'
        '<parameter name="y">241</parameter>\n'
        '</invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    actions = gui_agent.parse_actions(content)
    assert len(actions) == 1
    assert actions[0].kind == "pyautogui"
    assert actions[0].function == "click"
    assert actions[0].kwargs == {"x": 712, "y": 241}
    assert actions[0].repair_method == "dsml_invoke"


@pytest.mark.parametrize("content", [
    '<｜｜DSML｜｜ calls><invoke name="pyautogui.click">'
    '<parameter name="x">1</parameter><parameter name="y">2</parameter>'
    '</invoke></｜｜DSML｜｜ calls> extra text',
    '<｜｜DSML｜｜ calls><invoke name="pyautogui.click">'
    '<parameter name="x">1</parameter><parameter name="x">2</parameter>'
    '</invoke></｜｜DSML｜｜ calls>',
    '<｜｜DSML｜｜ calls><invoke name="pyautogui.click">'
    '<parameter name="x"><script>bad</script></parameter>'
    '</invoke></｜｜DSML｜｜ calls>',
    '<｜｜DSML｜｜ calls><invoke name="subprocess.run">'
    '<parameter name="args">["echo", "bad"]</parameter>'
    '</invoke></｜｜DSML｜｜ calls>',
])
def test_dsml_rejects_ambiguous_or_unsupported_content(content):
    with pytest.raises(gui_agent.ActionParseError):
        gui_agent.parse_actions(content)
