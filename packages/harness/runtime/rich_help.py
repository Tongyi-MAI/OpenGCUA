"""On-demand manual for the sandbox: mu.help(topic) rendered from the generated contract.

Topics:
  core                 overview + topic list
  native/coordinates   short macOS window workflow and coordinate rules
  apps/windows/ax/vision/input/ui/device/session/web
                       the matching api.pyi class signatures
  playwright|page|locator|mouse|keyboard|builders
                       the Playwright subset (method + options + effect)
  <Class>.<method> or ui.<group>.<method> or playwright.<kind>.<method>
                       a single exact signature
"""

from __future__ import annotations

import difflib
import json
import re
from functools import lru_cache
from pathlib import Path

_GROUP_ALIASES = {
    "apps": "App",
    "windows": "Window",
    "window": "Window",
    "ax": "AX",
    "vision": "Vision",
    "input": "Input",
    "ui": "UI",
    "device": "Device",
    "session": "Session",
    "web": "Browser",
    "browser": "Browser",
    "tab": "Tab",
    "runtime": "Runtime",
}


@lru_cache(maxsize=1)
def _sources() -> tuple[str, dict]:
    import mai_ui.unified as unified

    base = Path(unified.__file__).resolve().parent
    api = (base / "api.pyi").read_text(encoding="utf-8")
    contract = json.loads((base / "playwright-contract.json").read_text(encoding="utf-8"))
    return api, contract


@lru_cache(maxsize=1)
def _classes() -> dict[str, list[str]]:
    api, _ = _sources()
    out: dict[str, list[str]] = {}
    current = None
    for line in api.splitlines():
        m = re.match(r"class (\w+)", line)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
            continue
        if current and re.match(r"\s+(async )?def ", line):
            out[current].append(line.strip())
    return out


def _flat_index() -> dict[str, tuple[str, str]]:
    index: dict[str, tuple[str, str]] = {}
    for cls, sigs in _classes().items():
        for sig in sigs:
            name = re.match(r"(?:async )?def (\w+)", sig)
            if name:
                index[f"{cls}.{name.group(1)}".lower()] = (cls, sig)
    return index


def _playwright_lines(kind: str) -> list[str]:
    _, contract = _sources()
    if kind == "builders":
        return [f"locator.{b}(...)  (chainable query builder)" for b in contract.get("builders", [])]
    lines = []
    for name, spec in (contract.get("methods", {}).get(kind) or {}).items():
        opts = ",".join(spec.get("options") or []) or "-"
        lines.append(f"{kind}.{name}(options: {opts})  effect={spec.get('effect')}")
    return lines


_TOPICS = ("core native coordinates apps windows ax vision input ui device session web tab "
           "playwright page locator mouse keyboard builders")

_NATIVE_GUIDE = """## 原生窗口：绑定 → 观察 → 操作 → 核验
找窗口：apps = await device.apps.list()；按真实 id 选 app = await device.apps.get(id)。
再查 windows = await app.windows.list()；按标题/身份选 win = await app.windows.get(id)；ui = win.ui。窗口未变时复用 win/ui。
读控件：ax = await ui.ax.observe()；在 ax.tree.nodes 中按 role/name/text 筛选；长树可用 ax.text.search/page/read。需要模型看见时 await mu.emit(ax)。
读外观：vision = await ui.vision.observe()；image = vision.images[0]；需要模型看见时 await mu.emit(vision)。
后台控件点击：await ui.ax.click(node)；后台 AXValue 写入只在控件确实支持时 await ui.ax.set_text(node, value)，并核验。
可见点击：绑定精确窗口后 await win.activate()；宿主会就本任务/窗口申请一次授权，演示模式也不豁免。再重新观察目标窗；有节点时 await ui.vision.click(node, image=image, delivery="foreground")；无节点时从截图生成 point，再 await ui.vision.click(point, delivery="foreground")。
可见输入：确认目标窗和编辑焦点后 await ui.input.text(value, delivery="foreground")；需要组合键用 await ui.input.press_key("a", modifiers=["meta"], delivery="foreground")。
滚动：point = image.point(x, y, space="normalized_1000")；await ui.vision.scroll("down", x=point, y=None, amount=3, unit="lines")。
核验：回执只证明动作送达；在写入、导航、翻页和焦点依赖的字段组后重新观察，确认实际结果。不要在一段 Tab 链中跨几十个字段盲做。
脚本：r = bash("命令") 是同步调用，不要 await bash(...)；检查 r.ok/r.timed_out，需要展示时 await mu.emit(r)。
坐标与截图绑定规则见 mu.help("coordinates")；完整签名分别查 mu.help("ax")、mu.help("vision")、mu.help("input")。"""

_COORDINATE_GUIDE = """## macOS 坐标：三个空间，不手算跨空间转换
1. 截图局部比例：窗口 image 的左上是 (0,0)，向右为 x、向下为 y；image.point(x, y, space="normalized_1000") 接受 0–1000 范围，(500,500) 约为该截图中心。它不是屏幕绝对像素或 AX 坐标；SDK 按截图尺寸换成像素。
2. 截图像素：image.width/image.height 是该截图尺寸。只有确实从同一张截图读到像素位置时才用 image.point(x, y, space="pixels")。
3. AX 几何：node.bounds.x/y/width/height 是屏幕全局逻辑点；Bounds 是有属性的对象，不是列表。Retina 缩放、窗口左上偏移及截图裁剪都可能存在，不能直接把 bounds 数值填给 image.point。
优先保留目标引用：有 AX 节点且有同一窗口的 image 时，await ui.vision.click(node, image=image) 由 SDK 把 AX 全局边界映射进截图；不要模型自己按倍率/窗口偏移换算。
无可靠节点时：point = image.point(x, y, space="normalized_1000")；await ui.vision.click(point)。PointRef 已含图像依据，不得再传 image=image 或 y；scroll 则须显式写 x=point, y=None。
窗口 image、桌面 image、AX 节点必须属于同一 UI 范围；窗口移动/缩放、页面滚动/重排、资源重置后重新观察，不复用旧坐标或节点。"""


def build_help(topic: str = "core") -> str:
    topic = (topic or "core").strip()
    classes = _classes()
    low = topic.lower()

    if low in ("", "core", "help"):
        return (
            "mu.help(topic) 按需手册。topic 可为分组：" + _TOPICS +
            "；或具体方法路径：ui.vision.scroll / vision.scroll / AX.observe / "
            "playwright.locator.fill / Tab.goto。返回精确签名与选项。\n"
            "观察契约：observe 捕获是静默的，await mu.emit(obs) 才下发；能力总览用 "
            "await window.capabilities()，单方法详情 await mu.emit(caps['ui.vision.drag'])；"
            "全文契约见沙箱内 unified/api.pyi。"
        )

    if low == "native":
        return _NATIVE_GUIDE
    if low in ("coordinates", "coords"):
        return _COORDINATE_GUIDE

    if low in ("playwright", "page", "locator", "mouse", "keyboard", "builders"):
        kinds = [low] if low != "playwright" else ["page", "locator", "mouse", "keyboard", "builders"]
        blocks = []
        for k in kinds:
            lines = _playwright_lines(k)
            blocks.append(f"## playwright.{k}\n" + "\n".join(lines))
        return (
            "Playwright 子集（tab.playwright / locator）：locator 是描述而非句柄，链式查询；"
            "terminal 方法直接 await。options 列名即关键字参数。\n" + "\n\n".join(blocks)
        )

    if low in _GROUP_ALIASES:
        cls = _GROUP_ALIASES[low]
        sigs = classes.get(cls) or []
        if not sigs:
            return f"help: no signatures found for class {cls}"
        return f"## {cls}\n" + "\n".join(sigs)

    # method path: accept Class.method, ui.group.method, playwright.kind.method
    cand = low
    if cand.startswith("ui."):
        cand = cand[3:]
    if cand.startswith("playwright."):
        rest = cand[len("playwright."):]
        kind, _, method = rest.partition(".")
        if kind in ("page", "locator", "mouse", "keyboard") and method:
            _, contract = _sources()
            spec = (contract.get("methods", {}).get(kind) or {}).get(method)
            if spec is None:
                return f"help: unknown playwright method {kind}.{method}; see mu.help('{kind}')"
            opts = ",".join(spec.get("options") or []) or "-"
            return f"playwright.{kind}.{method}(options: {opts})  effect={spec.get('effect')}"
        return build_help(kind or "playwright")
    parts = cand.split(".")
    if len(parts) == 1:
        # maybe a class name or a method name search
        if parts[0].title() in classes or parts[0] in classes:
            return build_help(parts[0])
        hits = [k for k in _flat_index() if k.endswith("." + parts[0])]
        if hits:
            return "\n".join(f"{k}: {_flat_index()[k][1]}" for k in hits[:8])
    key = ".".join(parts[-2:])
    hit = _flat_index().get(key)
    if hit:
        return f"{hit[0]}.{hit[1].split('def ', 1)[1]}"
    near = difflib.get_close_matches(low, sorted(_flat_index()) + list(_GROUP_ALIASES), n=5)
    if not near:
        near = sorted(set(_GROUP_ALIASES))
    return ("help: unknown topic '" + topic + "'. 可用分组/方法：" +
            ", ".join(near) + f"；全列表 mu.help('core')")
