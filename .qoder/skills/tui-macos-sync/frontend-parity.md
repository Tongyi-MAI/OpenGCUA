# TUI 与原生 macOS App 适配盘点

核查对象：OpenGCUA 当前迭代中的工作树，2026-10-21；会话控制已接线，但源码、测试和 bundle 仍可能变化。本文件记录契约与源码落点，不是原生 App UI/发布验收报告；本次不操作 App、不 build。修改前重新核查 Git、生产源码和当前测试，不沿用旧 commit 的“候选命令未实现”结论。

## 1. 当前能力与后续核查

| 优先级 | 功能 | TUI 现状 | 原生 App 现状 | 真正缺口 |
|---|---|---|---|---|
| P0 | 前台授权选择卡与授权状态 | `choose_approval`，默认 Reject，可取消 | `approval_reply/set_policy`、session policy、ApprovalCard 已接线 | 验证独立审批、默认拒绝、过期 ID、跨 turn 保留与 restart/resume 不继承 |
| P0 | 仅中止本轮，保留对话 | Ctrl-C + `on_turn_interrupt`；二次可强制停 worker | `interrupt` → aborted/interrupted，支持 force；保留 session 和旧队列 | 验证 stream/cell/approval 边界与纠正；不得把 pause/resume 或 stop 当 abort |
| P1 | 消息队列列表、数量、继续和清空 | `/queue`、`/queue send`、`/queue clear` | queue_list/resume/clear、queue_state、队列 UI 已接线 | 普通消息完整 turn 边界 FIFO，旧队列 suspended，correction 不自动释放旧消息 |
| P1 | Responses 与 Vertex 模型协议 | Python 配置接受 openai/openai-thinking/anthropic/vertex | Swift 当前仍只允许 openai/anthropic，JSON 也受校验 | 仍是协议入口缺口，另行授权适配；不要因为模型列表控件新增而宣称已支持 |
| P1 | 手动上下文压缩 | `/compaction`；复用 maybe_compact(manual)，严格缩短/取消 guard | compaction/compaction_cancel → 共享 callback → control_result，已有 UI | 验证 callback 仅完整 turn boundary 运行、取消不提交、失败不重试业务 |
| P1 | reasoning/code 展示开关 | 默认隐藏，可 `/reasoning on`、`/code on`，增量/重置事件 | NativeFrontend semantic adapter 和 Swift 展示开关/预览已接线 | 验证多块、reset、attempt/turn/sequence 与有界缓存；不改变执行 |
| P1 | 有效状态/等待原因/耗时 | `/status` 与状态栏；model_status 展示 API 等待等 | session_info/model_status、attempt/timeout、真实阶段/耗时诊断已接线 | 验证无敏感字段、旧 attempt 不污染新预览；回执不能当执行完成 |
| P1 | Skill 目录、选择和刷新 | `/skills`、`/skills reload`、`/skill <name>` | skills_list/reload/select、pending/apply、来源/digest UI 已接线 | 共享 session system 单槽；空槽可自动 load 一个且持续，仅用户替换/清空；核查命名选择预检 |
| P2 | 后台/演示和高级配置入口 | CLI/config 可指定模式、预算、历史图片等参数 | NativeHost 可从所选 JSON 透传多数参数；设置页没有同等控件 | 先显示有效配置及来源，再按需增加模式/预算控件；不是执行层不支持 |
| P2 | Skill 创建模板和打开目录 | `/skill new <name>` | 无专用入口 | 按需增加，只创建明确用户请求的模板，不自动写业务流程 |
| P2 | 恢复摘要与来源详情 | 最近十个兼容 run 选择、直接路径、恢复后计数/新消息 | 已有 HistoryView + openConversation + start.resume_path | 补恢复的 turn/TODO/模型/验证信息；外部路径导入是新范围，不是现有恢复缺失 |
| 可选 | 仅清可见输出 | `/clear` 不清真实上下文 | 有新对话，但没有等价仅清可见输出 | 若增加，不能复用 reset/end 去清会话 |

P0/P1 表示契约核查优先级，不代表生产能力已获 GUI 验收或已授权扩展实现。当前范围只允许新增指定测试与更新指定 CI/文档；测试失败交给负责生产实现的主代理，不修改生产或既有 tests。

## 2. 消息、纠正与队列边界

- TUI：`TerminalInteraction.submit()` 在 active turn 时入队；`RichTerminalFrontend.read()` 在完整轮次 boundary 取下一条。中断会挂起旧队列，可 `/queue send` 或 `/queue clear`。
- 原生：`TaskControl.message()` 标为 queued；普通消息不在 step 规划边界消费，`follow_up()` 在完整 turn 结束后一次消费一条 FIFO 消息并标为 consumed。旧 applied 收据仍需兼容，不能据此保留旧 steering 默认值。
- `interrupt` 只中止本轮并置 suspended；新的 correction 可先于旧消息进入下一 turn，但不释放旧队列。继续任务（pause/resume）不等于 `queue_resume`；旧队列需手动继续或清空。
- `queue_clear` 只取消未消费消息/控制；当前问题、已消费请求、已派发动作和已应用 Skill 不回滚。问题回复和审批走独立 ID-fenced 通道。
- `queue_state` 与 message 收据显示真实状态；命令受理不能宣称队列已执行，超时/迟到结果不自动重发。

## 3. 已实现，不应重复开发

- 持续普通对话：App `sendInput` 的 start/message/reply 路由、turn_started/turn_finished 和本地消息记录。
- 暂停/继续/停止与中止本轮：`TaskControls`、`TaskControl`、`SessionControls`；stop 关闭 session，interrupt 保留对话/策略并挂起旧队列。
- 普通 ask_user：question_id/reply，过期问题拒绝。
- 历史恢复：`HistoryView → AppModel.openConversation/loadConversation → start(resume_path) → NativeHost.resume_path/run`。
- 新对话：`AppModel.reset()`，live conversation 先 end/ack 再清 UI。
- 模型 endpoint/name/effort、OpenAI/Anthropic 配置、文件优先、endpoint Keychain 隔离。
- 多数共享高级参数可由所选 JSON 传入 NativeHost.task_arguments；App 默认 max_steps=100、api_retries=1、api_timeout=180 与 CLI 不同，文件可以覆盖；这项差异是否保留需由产品要求决定。不得因设置页没有控件而认定后端没有能力。
- 屏幕/辅助功能引导、语音、全局快捷键、浮动窗和遮挡避让。
- Skill 加载/警告与 session 单槽：`SkillContext.system_slot()` 每请求一次正文，`select/activate` 区分用户替换与模型首次选择，`mac_agent` 同步 worker 与轨迹记录；不代替任何业务授权。
- App 有 task_id、RuntimeBridge 有 generation、SessionControls 有 turn/sequence/attempt 和 pending command/approval 过滤；仍需验证迟到、重复、超时，不宣称没有身份隔离。

## 4. 当前协议地图

### TUI，不是 NDJSON

`agent_events.py`：

- `AgentEvent(kind, text, step, payload)`：呈现更新。
- `AgentPrompt(kind=task/question/retry/approval)`：输入请求。
- `ForegroundApprovalPrompt(request_id, text, task_index, app_id, window_id)`：专用前台审批。
- `TerminalCommand(kind, target)`：host-only 命令。
- 现有 kind：resume、compaction、skills_list、skills_reload、skill_select、skill_create。

TUI 本地 `/status`、`/queue`、显示开关、`/clear` 不会发送给模型。Skill/压缩/恢复由 `mac_agent` 的 `read_task_boundary` 分派。

### 原生 NDJSON

`NativeHost.command()` 当前支持：

- start：task_id、text、config，可有 resume_path。
- pause/resume/stop/end：task_id；resume 是继续暂停的任务，不是恢复历史。
- interrupt：task_id，可有 force；中止本轮，不关闭 session。
- set_policy：task_id，foreground_allowed/ask_user_enabled 必须 bool；两个策略独立。
- approval_reply：task_id、approval_id、choice=allow/reject/cancel；默认拒绝，不接普通 reply。
- queue_list/queue_resume/queue_clear：task_id；只管理未消费队列。
- compaction/compaction_cancel：task_id；前者排队到完整 turn boundary，后者请求取消。
- skills_list/skills_reload：可空闲请求目录；skill_select：task_id、name（名称或 null），运行中 pending。
- message：task_id、message_id、text；reply：task_id、question_id、text。
- overlay_ack：task_id、guard_id；shutdown：由 serve 消费。
- 有 command_id 的受理结果返回 command_ack/error；完成由 state/control_result 报告。

事件包括 ready(protocol=1)、state、message(queued/consumed/applied/cancelled)、reply、question、progress、intent、trajectory、overlay、turn_started/finished、finished、error；新增 session_info、policy、approval/approval_closed、interrupted、queue_state、skills_catalog、skill_state、control_result、model_status、reasoning_delta/end、code_start/delta/end/reset，保留 skill_loaded/skill_warning。

TaskControl.emit 递增 sequence 并携 task_id/turn_index；NativeFrontend 为 preview 附 attempt_id，为 control_result 附当前 command_id。Scope 是 session，但 task_id wire 名称不需要改为 session_id。命名 Skill 校验需先 discover 再 load；当前迭代不能把这项预检或 pending 回执当 apply 成功。

### 原生 adapter 与共享控制的边界

- `NativeHost.run()` 正常路径传 `frontend=NativeFrontend(control)`；runner fixture 替代执行层，但保留真实 Host/TaskControl 分派，不启动 driver/API/GUI。
- `serve()` 仍将 agent stdout/stderr 重定向到空设备；原生消费结构化 semantic event，不解析控制台文本。
- `NativeFrontend.emit()` 投影 session/policy、phase、preview/status/control_result；aborted 投影 interrupted，并取消问题/审批等待、保留 session、发 suspended queue snapshot。
- `TaskControl.follow_up()` 在完整 turn boundary 调 `process_controls()`；`bind_runtime()` 的 policy/skill/compaction 回调复用共享状态与算法，不能把 `boundary()` 暂停检查误当手动控制执行。
- 前台权限使用专用 `choose_approval` 与 approval_reply，不经 ask_user。授权只在本实时 session 跨 turn/窗口保留；stop/end/切换/断开重连/restart/resume 均不继承。
- ask_user disabled 只关闭业务提问，每请求动态投影一次策略；仍必须走独立前台门禁，不将 Skill、普通“允许”文本或提问关闭状态当业务授权。

## 5. 适配的源码落点

所有路径相对仓库根：

- `packages/harness/runtime/terminal_tui.py`：命令目录、队列、事件显示。
- `packages/harness/runtime/terminal_interaction.py`：TUI 队列转移。
- `packages/harness/runtime/agent_events.py`：共享前端/输入/审批/TerminalCommand 类型。
- `packages/harness/runtime/mac_agent.py`：present、read_task_boundary、approve_gui、model_event、manual compaction、host_control 消费边界。
- `packages/harness/runtime/native_host.py`：NativeFrontend、TaskControl、NativeHost.run/command/resume_path、私有管道。
- `packages/harness/runtime/tests/test_native_session_controls.py`：新增真实控制对象 runner fixture 与 ContextManager 单槽/动态策略离线契约；生产失败仅交主代理处理。
- `packages/harness/runtime/llm_entry.py`、`model_stream.py`：协议与 effective settings。
- `packages/harness/runtime/llm_compaction.py`：唯一压缩实现；不要复制到 App。
- `packages/harness/runtime/skill_registry.py`、`skill_context.py`、`skill_runtime.py`：唯一 Skill 处理。
- `packages/macos/Sources/AppModel.swift`：@Published 状态、sendInput/command/receive、消息 ID、历史恢复。
- `packages/macos/Sources/RuntimeBridge.swift`：Process/Pipe 与 generation guard。
- `packages/macos/Sources/SessionControls.swift`、`ControlPanelViews.swift`：session 状态、策略/队列/审批/控制回执、预览/诊断和 UI。
- `packages/macos/Sources/Views.swift`：MainView、InputComposer、QuestionView、TaskControls、CompactView。
- `packages/macos/Sources/MiniWindow.swift`、`TaskOrbView.swift`：浮动窗布局与操作避让。
- `packages/macos/Sources/Settings.swift`：Codable/validate/taskConfiguration；两协议限制也在这里。
- `packages/macos/Sources/SettingsEditor.swift`、`ModelSettingsForm.swift`、`SettingsView.swift`：设置草稿、选择器与保存。
- `packages/macos/Sources/HistoryView.swift`、`OnboardingView.swift`：恢复入口与首启表单。
- `packages/macos/Resources/en.lproj/Localizable.strings`、`zh-Hans.lproj/Localizable.strings`：新文案。

## 6. 推荐验收与现有测试

| 适配 | 最低验收 | 可复用测试 |
|---|---|---|
| 授权卡 | 默认拒绝、独立于 ask_user disabled、过期 ID 拒绝、同 session 跨 turn 保留/撤销，restart/resume 不继承 | test_native_session_controls.py、test_gui_approval.py、VoiceRouterContracts.swift |
| 中止本轮 | stream/cell/approval 中止均有终态；未消费输入保留；同会话能发纠正；已派发动作不承诺回滚 | test_turn_interrupt.py、test_terminal_tui_pty.py、test_native_host.py、VoiceRouterContracts.swift |
| 队列 | 顺序/数量/暂停状态/已消费边界准确，clear 不丢当前操作或问题回复 | test_terminal_interaction.py、test_agent_frontend.py、test_native_host.py、VoiceRouterContracts.swift |
| provider | 手动和 JSON 均能选有效协议；旧偏好可加载；新请求不串 key，原生 thinking 历史可回放 | test_responses_transport.py、test_vertex_transport.py、test_anthropic_thinking_transport.py、ConfigContracts.swift |
| 手动压缩 | 只调用现有 manual 路径；少于两轮有反馈；取消不装摘要；有效摘要缩短且不覆盖新历史 | test_manual_compaction.py、test_agent_frontend.py、test_native_host.py |
| 流式诊断 | 分片 UTF-8/多块/reset/迟到数据处理正确；关闭显示不改变执行；高频输出不淹没输入 | test_terminal_tui.py、test_model_status.py、test_demo_hub.py、VoiceRouterContracts.swift |
| Skills | 只广告元数据、session system 单槽每请求一次、空槽自动 load 一个持续、仅用户替换/清空、pending 下一 turn apply/失败、来源/digest 可见 | test_native_session_controls.py、test_skill_context.py、test_sdk_session_controls.py、VoiceRouterContracts.swift |
| 恢复详情 | 仅选择历史不请求模型/执行；等新消息；旧环境对象不恢复；原生目录限制保留 | test_resume_catalog.py、test_agent_frontend.py、test_native_host.py、VoiceRouterContracts.swift |

从仓库根运行 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest packages/harness/runtime/tests/test_native_session_controls.py -q -p no:cacheprovider`。同时覆盖命名 Skill 的真实 wire 预检与 TaskControl 分派；只验证后者不能证明完整命令路径成功。

Swift 契约源的编译闭包以 `.github/workflows/macos-app.yml` 为准；VoiceRouterContracts 输入包含 SessionControls.swift，使用 fixture RuntimeBridge，不链接真实 bridge。pytest 通过不代表 Swift 已编译/执行；实际编译、UI 和 bundle 验证需分别运行、分别报告。

开发构建使用 `packages/macos/scripts/build.py` 调用 swiftc，不存在需要打开的 Xcode project。`--skip-runtime` 需要已有 bundle，仍复制 runtime Python 源码/Skills；完整构建重建依赖/SDK/驱动。改代码后要重新打开新 App，不是热更新。

## 7. 后续范围与真实验收

1. 先运行离线 session 控制契约，失败报告主代理；不擅自修生产、既有 tests、提交或操作用户 App。
2. 用户另行允许 build/UI 验收后，再验证新 bundle 的控件与 stream/cell/approval 中止、session 权限、manual 队列、真实 compaction 与模型目录。
3. 从 DMG 拖入 Applications，退出旧 App、弹出 DMG 后打开安装版本；ad-hoc 二进制替换可能需要 Keychain/辅助功能/录屏重新授权，不自动操作权限弹窗。
4. Responses/Vertex、Skill 模板、恢复详情、仅清可见输出仍属独立范围，不因本次控制适配而自动实施。

App 可以保留不同发布节奏，但不能把 stop vs interrupt、session 授权或协议缺口隐藏在外观里。区分源码已接线、fixture 已测、Swift 未编译、GUI/API/安装未测，不伪装成功。
