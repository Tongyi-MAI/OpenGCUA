from rich_help import build_help
from mac_agent import MAI_UI_SYSTEM_PROMPT


def test_native_help_gives_a_window_observe_act_verify_route():
    guide = build_help("native")

    assert "device.apps.list()" in guide
    assert "app.windows.list()" in guide
    assert "ui = win.ui" in guide
    assert "ui.ax.observe()" in guide
    assert "ui.vision.observe()" in guide
    assert "await mu.emit(" in guide
    assert "ui.input.text(" in guide
    assert "核验" in guide


def test_coordinate_help_distinguishes_image_points_from_ax_bounds():
    guide = build_help("coordinates")

    assert 'image.point(x, y, space="normalized_1000")' in guide
    assert "0–1000" in guide
    assert "node.bounds.x" in guide
    assert "ui.vision.click(node, image=image)" in guide
    assert "ui.vision.click(point)" in guide
    assert "PointRef" in guide
    assert "同一窗口" in guide


def test_core_help_advertises_task_guides_without_hiding_exact_signatures():
    guide = build_help("core")

    assert "native" in guide
    assert "coordinates" in guide
    assert "ui.vision.scroll" in guide


def test_macos_agent_gets_the_common_route_and_coordinate_contract_first():
    prompt = MAI_UI_SYSTEM_PROMPT

    assert prompt.index("## 原生任务速查") < prompt.index("## 原生应用与窗口")
    assert prompt.index("## 坐标系统") < prompt.index("## 网页浏览器 web")
    assert "目标依据" in prompt and "投递方式" in prompt
    assert "PointRef 已绑定截图" in prompt
    assert "node.bounds.x" in prompt
    assert "bash(...) 是同步调用" in prompt
