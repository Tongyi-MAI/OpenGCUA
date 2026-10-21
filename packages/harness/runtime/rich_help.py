"""On-demand manual for the sandbox: mu.help(topic) rendered from the generated contract.

Topics:
  core                 overview + topic list
  tools                tool-group responsibilities, sync/async, observe vs emit, receipts
  native/coordinates   short macOS window workflow and coordinate rules
  apps/windows/ax/vision/input/ui/device/session/web/...
                       the matching api.pyi class signatures (apps = Apps + App)
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

# 分组 → api.pyi 类；apps/windows 同时给出集合（list/get）与句柄类，和 device.apps / app.windows 的真实类型一致。
_GROUP_ALIASES = {
    "apps": ("Apps", "App"),
    "app": ("App",),
    "windows": ("Windows", "Window"),
    "window": ("Window",),
    "ax": ("AX",),
    "vision": ("Vision",),
    "input": ("Input",),
    "ui": ("UI",),
    "image": ("Image",),
    "device": ("Device",),
    "session": ("Session",),
    "files": ("Files",),
    "shell": ("Shell",),
    "processes": ("Processes",),
    "clipboard": ("Clipboard",),
    "macos": ("Macos",),
    "web": ("Browser",),
    "browser": ("Browser",),
    "tab": ("Tab",),
    "runtime": ("Runtime",),
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
    """类 → 签名行（属性行 + 方法行）；只有属性的类（如 Window）也能显示。"""
    api, _ = _sources()
    out: dict[str, list[str]] = {}
    current = None
    for line in api.splitlines():
        m = re.match(r"class (\w+)", line)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
            continue
        if current and re.match(r"\s+((async )?def |\w+: )", line):
            out[current].append(line.strip())
    return out


@lru_cache(maxsize=1)
def _bases() -> dict[str, str]:
    api, _ = _sources()
    return {m.group(1): m.group(2) for m in re.finditer(r"^class (\w+)\((\w+)\):", api, re.M)}


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


_TOPICS = ("core tools native coordinates memory apps windows ax vision input ui image device session "
           "files shell processes clipboard macos web tab "
           "playwright page locator mouse keyboard builders")

_TOOLS_GUIDE = """## 工具分工：绑定 → 读 → 做 → 核验
绑定：await device.apps.list() / await device.apps.get(id) → await app.windows.list() / await app.windows.get(id) → win；win.ui.ax 读控件树并做后台控件动作，win.ui.vision 截图并做坐标动作，win.ui.input 做键盘/文本输入。
设备服务：device.files（目标设备绝对路径）、device.shell.run / device.processes.run（目标设备命令）、device.clipboard；宿主沙箱命令用 bash("…")。
同步/异步：SDK 的 observe/list/get/capabilities/动作方法都要 await；mu.help、image.point、bash 是同步函数，不要 await（以 mu.help 签名里是否 async def 为准）。
observe vs emit：observe/list/get/capabilities 只把结果交给代码，静默不下发；await mu.emit(x) 才让模型看到。emit 不执行动作，也不核验。
AXValue vs 输入：ui.ax.set_text 写 AXValue；原生报告 AXValue 不可设置只说明这条写值路径不可用，不证明控件不能输入或是静态文本。可改用 await ui.input.text(text, target=node)（node 为同一次 ax.observe() 的 AXNode 或 index）；需要前台时加 delivery="foreground"，须经宿主许可。
回执：变更动作返回 ActionReceipt，execution/effect 是 SDK 技术事实。execution=completed 只说明执行流程结束，effect 为 applied/not_applied/partial/suspected_noop/unknown；rejected 或 cancelled_before_dispatch 且 not_applied 表示未应用；unknown 表示可能已有副作用，先 await mu.action_status(action_id) 并重新观察，不要直接重试。回执不证明业务结果。
自动事实反馈：cell 结束时宿主附上变更调用的事实，包括被 try/except 捕获的异常。phase=python_precheck：SDK 本地校验在派发前拒绝，必然未执行，附可读原因与 mu.help 精确签名；phase=host_gate：宿主许可/overlay/精确窗口门禁拒绝。observe 正常时没有反馈行，不是遗漏。
前台许可：await win.activate() 与 delivery="foreground" 需宿主许可：原生 App 宿主按 session 许可（直到撤销或切换 session），终端宿主按任务许可，都不按窗口，演示模式不豁免；被拒后用 await request_foreground_permission(win, "原因") 再申请。"""

_NATIVE_GUIDE = """## 原生窗口：绑定 → 观察 → 操作 → 核验
找窗口：apps = await device.apps.list()；按真实 id 选 app = await device.apps.get(id)。
再查 windows = await app.windows.list()；按标题/身份选 win = await app.windows.get(id)；ui = win.ui。窗口未变时复用 win/ui。
读控件：ax = await ui.ax.observe()；在 ax.tree.nodes 中按 role/name/text 筛选；长树可用 ax.text.search/page/read。需要模型看见时 await mu.emit(ax)。
读外观：vision = await ui.vision.observe()；image = vision.images[0]；需要模型看见时 await mu.emit(vision)。
后台控件点击：await ui.ax.click(node)；后台 AXValue 写入 await ui.ax.set_text(node, value)。若原生报告 AXValue 不可设置，只是这条写值路径不可用，可改 await ui.input.text(value, target=node)。
可见点击：绑定精确窗口后 await win.activate()；前台键鼠需宿主许可（原生 App 为 session 许可、终端为任务许可，不按窗口，演示模式也不豁免；被拒后用 request_foreground_permission(win, reason) 再申请）。再重新观察目标窗；有节点时 await ui.vision.click(node, image=image, delivery="foreground")；无节点时从截图生成 point，再 await ui.vision.click(point, delivery="foreground")。
可见输入：确认目标窗和编辑焦点后 await ui.input.text(value, delivery="foreground")；需要组合键用 await ui.input.press_key("a", modifiers=["meta"], delivery="foreground")。
滚动：point = image.point(x, y, space="normalized_1000")；await ui.vision.scroll("down", x=point, y=None, amount=3, unit="lines")。
核验：回执 execution/effect 是 SDK 技术事实（是否执行、效果是否已知），不证明业务结果；在写入、导航、翻页和焦点依赖的字段组后重新观察，确认实际结果。不要在一段 Tab 链中跨几十个字段盲做。
脚本：r = bash("命令") 是同步调用，不要 await bash(...)；检查 r.ok/r.timed_out，需要展示时 await mu.emit(r)。
坐标与截图绑定规则见 mu.help("coordinates")；分工/回执/自动反馈见 mu.help("tools")；完整签名分别查 mu.help("ax")、mu.help("vision")、mu.help("input")。"""

_COORDINATE_GUIDE = """## macOS 坐标：三个空间，不手算跨空间转换
1. 截图局部比例：窗口 image 的左上是 (0,0)，向右为 x、向下为 y；image.point(x, y, space="normalized_1000") 接受 0–1000 范围，(500,500) 约为该截图中心。它不是屏幕绝对像素或 AX 坐标；SDK 按截图尺寸换成像素。
2. 截图像素：image.width/image.height 是该截图尺寸。只有确实从同一张截图读到像素位置时才用 image.point(x, y, space="pixels")。
3. AX 几何：node.bounds.x/y/width/height 是屏幕全局逻辑点；Bounds 是有属性的对象，不是列表。Retina 缩放、窗口左上偏移及截图裁剪都可能存在，不能直接把 bounds 数值填给 image.point。
优先保留目标引用：有 AX 节点且有同一窗口的 image 时，await ui.vision.click(node, image=image) 由 SDK 把 AX 全局边界映射进截图；不要模型自己按倍率/窗口偏移换算。
无可靠节点时：point = image.point(x, y, space="normalized_1000")；await ui.vision.click(point)。PointRef 已含图像依据，不得再传 image=image 或 y；scroll 则须显式写 x=point, y=None。
裸数字：await ui.vision.click(x, y) 的原始 x,y 按 mu.coordinate_space 解释（本宿主设为 normalized_1000）；而 image.point 的 space 默认是 "pixels"，须显式写 space。
窗口 image、桌面 image、AX 节点必须属于同一 UI 范围；窗口移动/缩放、页面滚动/重排、资源重置后重新观察，不复用旧坐标或节点。"""

_MEMORY_GUIDE = """## 长期记忆：主动候选，用户确认生效
r = await memory.propose(title="周报发送前确认收件人", content="发送周报前先让用户确认收件人列表", kind="workflow", app="钉钉")
kind 取 preference/workflow/fact；标题≤120、正文≤2000 字符，app 可省略。只在已有代码中顺带提出，不为此额外观察或空转。
r["status"]：candidate=已入待审核（不是核验或授权）；duplicate/duplicate_approved=已有相同内容；rejected=被规则拒绝（看 r["error_code"]）；not_available=本会话无记忆服务。失败不重试，不改写到文件。
不要提出：密码/令牌/凭证；永久授权、免确认、自动购买或发送许可；一次性任务细节；坐标、AX 节点、窗口 id、句柄等易失定位。审核、修改、遗忘只由用户在宿主执行，沙箱内没有对应接口。
召回：只有用户批准的条目会以 "Long-term memory" 数据区出现在请求里；它是参考数据，不覆盖最新用户消息、授权边界或宿主门禁；应用版本、界面或窗口身份相关内容须重新观察确认。「已失效的长期记忆」列出的 id 不得再作为依据。"""

_AX_METADATA = """## AX 能力元数据：来源决定可信度，缺省为 unknown
node.editable 只是兼容字段；node.editable_source 为 "role_hint" 时由角色推断（文本中显示 editable=role_hint），不证明 AXValue 可写。
node.value_settable 只在 node.value_settable_source == "native" 时是原生结论；否则为 None/"unknown"。macOS 当前驱动不报告可写性，故恒为 unknown。
node.actions_source == "native" 表示动作列表来自原生报告；为 "unknown" 时空 actions 不证明不支持。perform 仍只接受 actions 中列出的动作。"""


def _render_classes(names, label: str) -> str:
    classes = _classes()
    blocks = []
    for cls in names:
        sigs = classes.get(cls) or []
        if not sigs:
            continue
        base = _bases().get(cls)
        # 继承的方法不重复列出，只给出基类名（Window(App) 仍有 activate/launch 与 ui 等）。
        block = f"## {cls}" + (f"({base})" if base else "") + "\n" + "\n".join(sigs)
        if cls == "AX":
            block += "\n\n" + _AX_METADATA
        blocks.append(block)
    if not blocks:
        return f"help: no signatures found for {label}"
    return "\n\n".join(blocks)


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
            "工具分工、回执含义与自动事实反馈见 mu.help('tools')；"
            "全文契约见沙箱内 unified/api.pyi。"
        )

    if low == "tools":
        return _TOOLS_GUIDE
    if low == "native":
        return _NATIVE_GUIDE
    if low in ("coordinates", "coords"):
        return _COORDINATE_GUIDE
    if low == "memory":
        return _MEMORY_GUIDE

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
        return _render_classes(_GROUP_ALIASES[low], low)

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
        named = [cls for cls in classes if cls.lower() == parts[0]]
        if named:
            return _render_classes(named, parts[0])
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
