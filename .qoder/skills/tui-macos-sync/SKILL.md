---
name: tui-macos-sync
description: 指导 OpenGCUA 的 TUI 与原生 macOS App 协同开发和功能适配，覆盖 Python 事件与控制协议、SwiftUI 状态和配置、打包与分层验证。适用于新增终端命令、原生界面能力、模型协议、授权、中断、恢复、队列或排查两端行为不一致。
---

# OpenGCUA TUI 与原生 App 协同开发

## 范围

- TUI：`packages/harness/runtime/terminal_tui.py`。
- 原生前端：`packages/macos/Sources/`，SwiftUI + AppKit。
- 共享执行语义：`mac_agent.py`、`code_executor.py`、`code_runtime.py`、LLM 和 Skill 模块。
- 原生通道：`RuntimeBridge.swift → native_host.py` 的私有 NDJSON 管道，不是 demo_hub HTTP。
- 本 Skill 是开发者使用的项目级 Qoder Skill，不放进 `packages/harness/skills/`，不向运行中的产品 agent 广告。
- 仅请求检查时只读；只有请求实现时才修改产品代码。不要顺手恢复 MCP、增加桌面多 agent 或改变权限默认值。

先阅读 [frontend-parity.md](frontend-parity.md)，其中包含功能差异、源码证据和命令/事件路径。该参考是快照，操作前重新核查当前 Git 和源码。

## 1. 先区分缺口类型

对每项功能列出 `TUI 入口 → 共享处理 → NativeHost 支持 → Swift 状态 → View → 测试`。

分类：

1. **已有**：两端已有等价行为，不必复制终端交互。
2. **展示缺口**：共享执行能力存在，原生侧缺少事件投影、状态或控件。
3. **控制通道缺口**：原生 host 不认识命令或无法调用共享处理；不能只加按钮。
4. **行为不一致**：两端都能执行，但消费消息、取消范围、配置或恢复边界不同。
5. **有意入口差异**：快捷键、菜单、浮动窗和语音无需一一对应。

不要将“原生没有 `/resume` 命令”误判为“没有历史恢复”；检查 `resume_path`。原生已有专用 session 审批和控制通道，不能沿用旧盘点的“仅文字授权/命令未实现”结论。

## 2. 找对修改入口

| 变更 | 核查/修改位置 |
|---|---|
| TUI 命令、补全、展示开关 | `terminal_tui.py` 的 `SlashCommandCompleter`、`_local_command`、`_submit_enter`、`_consume` |
| 跨前端事件/控制类型 | `agent_events.py` 的 `AgentEvent`、`AgentPrompt`、`TerminalCommand`、`ForegroundApprovalPrompt` |
| 用户消息队列 | `terminal_interaction.py`；原生 `TaskControl` 与 `AppModel` 消息收据 |
| 共享 Skill/压缩/恢复 | `mac_agent.run_agent` 内边界处理；`skill_*`、`llm_compaction.py`、`resume_catalog.py` |
| 原生命令/事件接线 | `native_host.py` 的 `NativeHost.command/run`、`TaskControl`、`NativeFrontend` |
| Swift 状态和发送/接收 | `AppModel.swift` 的 `sendInput/command/receive`；`SessionControls.swift`、`RuntimeBridge.swift` |
| 会话控件、预览和详细状态 | `ControlPanelViews.swift`；模型设置保持既有 Settings 路径 |
| 主面板、输入、控制、小窗内容 | `Views.swift`；`MiniWindow.swift`、`TaskOrbView.swift` |
| 配置字段/选择器/持久化 | `Settings.swift`、`SettingsEditor.swift`、`ModelSettingsForm.swift`、`SettingsView.swift` |
| 历史和首启 | `HistoryView.swift`、`OnboardingView.swift`、`FirstLaunch.swift` |
| 中英文文案 | `Localization.swift` 与 `Resources/{en,zh-Hans}.lproj/Localizable.strings` |
| 源码到 bundle | `packages/macos/scripts/build.py`；发布校验见 `.github/workflows/macos-app.yml` |

## 3. 先定契约，再分别适配

- 把功能状态、命令参数、请求成功/拒绝/取消/终态定义在共享执行或控制边界；UI 只做适配和呈现。
- 优先复用已有回调与类型；若处理被埋在 `run_agent` 闭包，提取最小共享处理，不在 Swift 或 `native_host.py` 复制摘要/Skill/恢复算法。
- NativeHost 当前命令是 JSON 对象。TUI 的 `TerminalCommand` 不是 wire command；不能将 `/compaction` 或 `/skills` 当普通聊天文字发送并宣称接入成功。
- 为新增 wire 命令定义关联 ID、作用域、允许状态、错误和 ack；耗时完成用独立事件，ack 只表示受理。
- 新字段兼容旧配置/历史；不兼容协议须明确版本校验，不静默吞掉不支持的字段。
- NativeHost 将 agent stdout/stderr 丢到空设备；只在 `present()` 打印的事件不会自动到 App。必须显式通过结构化事件 adapter 或 `host_control` 转发。
- 控制失败不得触发模型业务重试；迟到事件要检查 generation、task/turn/request 身份。

## 4. 保持交互与安全语义

- 所有自然语言输入是普通 user message；继续、选择、纠正不变成不可变 task，不写进固定 system instruction。
- 运行中普通消息默认 FIFO 排到完整 turn 结束，每轮消费一条；不能在 step 边界消费或静默取消已规划动作。显式中止/纠正与普通排队分开。
- 普通消息、问题回复、前台授权、宿主控制命令分通道。禁止以输入“允许”替代所有业务授权。
- 中止本轮、暂停到边界、停止会话、退出进程四者不能混用；中止保留对话和未消费输入，不回滚已派发动作。
- 最新用户批准的前台授权范围是 **session 而非 task**：复用 `GuiGrantStore` 的 session 状态，默认关闭/拒绝；获准后跨 turn、窗口和本轮中止保留，可由用户显式撤销。停止/结束/切换会话、断开重连、App restart 和历史 resume 均不得继承旧授权，禁止写入 Preferences 或由 JSON 文件自动批准。
- `ask_user_enabled` 只控制业务提问，与专用前台审批独立；关闭后缺信息要显式阻塞，不能借审批卡提问，也不能将关闭提问当授权。每次请求只投影一次动态策略，不能累计追加到固定 system_prompt。
- 恢复展示历史并等待新消息；不重跑存档任务，不宣称 Python/SDK 句柄已恢复。原生恢复路径继续限制在 App 拥有的 runs 根目录。
- 队列清空只清未消费消息/控制并发取消结果，不清当前问题、不回滚已消费指令或已派发动作。中止后的旧消息保持 suspended，新的 correction 可优先运行但不释放旧队列；旧队列必须手动继续或清空。
- **Skill 是 session 的 system 单槽**：新空槽可由模型自动 `skills.load` 一个并持续跨 turn；已有槽仅允许加载同名，替换/清空必须用户显式选择。运行中选择 pending，在完整 turn 边界 apply；刷新目录不是替换批准。正文每请求只出现一次，不放进普通 user message，也不靠下一消息一次性标记销毁选择。
- Skill 虽在 system 槽中，仍是工作流参考，不覆盖用户最新请求/其他系统规则，不代替前台或业务授权。跨 restart/resume 的 Skill 记录须验证 source/digest，不能据此恢复会话授权。
- reasoning/code 开关只是显示策略，不改变执行、审计、完整响应校验或权限；预览代码不能边生成边执行。
- 保留 overlay 握手及小窗避让，不为显示新控件抢占焦点。
- 不自动打开 TextEdit、扫描/点击权限弹窗、清理用户 App、修改 TCC 或系统偏好。

## 5. 配置变更的完整链

检查：`CLI/parser → llm_entry/configure_runtime_mode → NativeHost.task_arguments → Preferences/taskConfiguration/validate → SettingsEditor → Form → bundle`。

- 不把 `Preferences` 的两协议选项误当成 Python 支持的全部协议；检查 Swift 校验是否拒绝 JSON 文件里的新值。
- 新字段必须有旧版本 decode 默认值及无效值提示，确认手动配置和选中文件的优先级。
- UI 显示 effective model/protocol/effort，不只显示 draft。执行中不静默更换已捕获配置。
- NativeHost 限制的 host-owned 参数不能由 JSON 覆盖。
- 密钥继续按 endpoint 存 Keychain；文件密钥不复制到 UserDefaults；不通过 CLI/env 隐含导入其他 provider 的 key。
- 两端默认值可以有经过说明的产品差异；需记录真实生效值，而非强行统一所有数字。

## 6. 回归验证

每项至少包括：

- 共享处理和 TUI 输入/事件单测。
- NativeHost JSON 命令/ack/终态测试；未知命令、过期 ID、取消/重连覆盖。
- Swift 真 `AppModel` + fixture `RuntimeBridge` 状态测试，以及相关 View 编译。`VoiceRouterContracts.swift` 编译输入须包含 `SessionControls.swift`；fixture 不链接真实 bridge、不调用模型网络。
- 配置字段的 Codable 旧值回退、文件优先级、凭据隔离。
- 有界失败、迟到事件、重复收据、不支持状态、中文输入法确认候选不误发送。

从仓库根仅运行新增原生 session 离线契约（不 build、不打开 App）：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest packages/harness/runtime/tests/test_native_session_controls.py -q -p no:cacheprovider
```

该文件用真实 TaskControl/NativeHost runner fixture、NativeFrontend 和 ContextManager 验证 session policy/独立审批/过期拒绝、FIFO/suspend/correction/clear、boundary compaction/取消、pending Skill/apply/失败、semantic sequence/attempt、中止保留 session、每请求一次 system 单槽与 ask policy。callback 为离线 fixture，不证明真实摘要/API/worker 或 GUI 成功。失败只报告给负责生产实现的主代理；若本次范围限于测试/文档，不修改 Python 生产、Swift、其他 source 或既有 tests。

需要全量回归且已获相应范围授权时：

```bash
PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests packages/macos/tests -q
```

Swift 契约测试不是全部由 pytest 自动执行；从 `.github/workflows/macos-app.yml` 复制对应 `swiftc` 编译与运行步骤。不要猜一个不存在的 Xcode 工程。

已有本地 bundle 的 UI/host-only 迭代：

```bash
uv run --no-project --python 3.12 python packages/macos/scripts/build.py --skip-runtime
```

首次构建或 SDK/Python/依赖/driver-lock 改动：

```bash
uv run --no-project --python 3.12 python packages/macos/scripts/build.py
```

`--skip-runtime` 仍复制 harness 源码和内置 Skills，但保留已有 Python 包/SDK/驱动；它不是热更新。保持既有签名策略，不使用 `codesign --deep --force` 重签 CuaDriver。

真实验收（仅在用户另行授权操作 App/build 时）：从 DMG 将 App 拖入 Applications，退出旧实例、弹出 DMG，再启动安装后的 App。ad-hoc 替换可能触发 Keychain/TCC 重授权，不扫描/自动点击权限弹窗。TUI 与新 bundle 执行同一可丢弃场景，验证两轮消息、控件、流式/终态、中止 vs stop、session 授权保留/撤销、manual 队列和恢复后的新输入；demo_hub 浏览器验证不能证明 Swift 前端可用。

截图或 UI 操作不可用时，报告阻塞；不能把 fixture 或仅编译通过写成原生端到端验证。

## 7. 输出与收尾

- 差异矩阵：功能｜两端证据｜缺口层｜建议范围｜验收。
- 实现摘要：共享改动、TUI adapter、NativeHost adapter、Swift UI/配置、兼容与 bundle。
- 验证清单区分已执行、跳过、无法验证，不沿用旧测试计数。
- 只在用户明确要求时提交；不要重启用户服务、杀 App 或扩大到无关功能。

## 示例

**核查“压缩上下文”按钮**：原生已有 `compaction/compaction_cancel`；复用 `maybe_compact(trigger="manual")` 与现有 commit guard，按 turn boundary 调用 callback，展示取消/收益/不足历史/失败，不能把 ack 当已压缩。普通消息和审批不会被该控制命令消费。

**适配 Responses 协议**：修改 Swift 协议选项与校验，验证 JSON 文件也接受 `openai-thinking`；NativeHost/LLM 复用现有客户端；旧设置保持可解码，界面显示生效协议，不显示加密推理原文。
