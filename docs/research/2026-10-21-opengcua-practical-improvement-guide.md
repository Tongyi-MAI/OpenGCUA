# OpenGCUA：当前问题、解决办法与迭代指南

> 面向维护者的实用版，不是外部产品源码综述，也不是功能已经完成的声明。
> 核查日期：2026-10-21。源码基线：`8d5930e`，提交标题“优化app前端展示v1.3”；本次开始核查时工作树干净。
> 初版只做源码盘点；随后实施 D01/D02、D04 规则反馈与 AX 能力来源、钉钉 Skill 配方及本地候选记忆管理。2026-10-22 按用户要求新增环境、上下文和 App 改进，并按 feature 提交，见第 5 节夜间迭代。只做离线与真实终端验证，没有运行真实桌面业务、修改系统权限、启动/退出或替换用户应用。

## 1. 先看结论

OpenGCUA 已经能连续对话、执行代码、调用 SDK、请求前台授权、保留上下文并显示进度。当前主要问题不是缺少更多按钮，而是：

1. **三端已统一模型/动作的基础状态；应用内部条件等待和真实业务核验仍未形成完整闭环。**
2. **动作级持久日志和未知结果分类已接入；恢复后仍需核实真实目标，不能把历史回执当业务成功。**
3. **模型说“完成”与文件正确、消息发对人、预订真的成功之间，缺少统一验证。**
4. **能读 AX 文本不等于能点/填；控件能力与等待机制不足，模型容易重复试错。**
5. **电脑有 SDK 锁，但真人与 Agent 如何协作、让出和紧急停止，还没有产品级闭环。**
6. **UI 已改善，构建签名、版本来源、数据预算和应用 Skill 的可靠性仍需治理。**

推荐顺序：**建立基线 → 统一事件 → 动作日志与一种未知恢复 → 结果引用/交付验证 → AX helper → 人机调度原型 → 上下文质量优化。**

钉钉 Skill 不必等所有架构完成：可以先整理真实失败案例，再随着每轮补上经过验证的操作手册。回归测试也必须从第一轮开始，不能放到最后。

## 2. 本文如何分类问题

| 分类 | 用普通话解释 | 例子 |
|---|---|---|
| 功能 F | 用户需要的能力没有形成闭环 | 保存结果未知后无法安全核对；没有统一文件验证 |
| 结构 S | 同一件事在多个模块各管一部分，修一处容易漏另一处 | Python 已输出 Intent，原生通道或 Swift 没消费 |
| 体验 U | 能力存在，但用户看不见、看不懂或难以操作 | 授权列表开着，App 仍显示待授权；找不到退出入口 |
| 安全/隐私 P | 权限、目标、资源或数据边界不明确 | 本地 HTTP 写接口门禁不统一；分享截图泄露内容 |

同一问题可以属于多类。**换一段文案只能修体验，不会自动修执行可靠性；拆文件只能改善结构，不会自动增加验证能力。**

本文另区分三种证据：
- **已遇到的问题**：此前本地调试或用户轨迹中出现过；注明是否已经修复。
- **代码缺口**：当前代码有基础，但没有完整实现建议中的契约。
- **待原型结论**：需要真实 OS/App 条件测量，不能靠阅读代码证明。

## 3. 先避免重复开发：现在已经有什么

| 现有能力 | 现在应如何理解 | 不要再次当作待添加能力 |
|---|---|---|
| 持续对话与消息队列 | 输入是普通 user message，运行中默认排到完整轮次结束 | 不要重新包装成不可变 task |
| App 控制面 | session 选择、单 Skill、ask_user 开关、审批、中止、压缩与诊断已有 | 不要把 App 写成仅有 start/stop |
| 显示入口 | Dock、独立主窗口、菜单栏、悬浮气泡、状态与 Intent 已补 | 不要再说只有菜单栏入口或生成阶段完全无 Intent |
| 前台授权 | 当前 live session 跨轮保留，切换/恢复/重连重新判断 | 不按旧报告在每次 terminate 后撤销 |
| 本轮中止 | 不等于结束整个 session；已派发操作不回滚 | 不把 pause、interrupt、stop、quit 混成一件事 |
| 单 Skill 插槽 | 所选正文每请求一次，跨轮/压缩保留；空槽可由模型加载一个 | 不重新加入多 Skill 全量 prompt |
| SDK 资源控制 | Authority、冲突域 FIFO、macOS 每用户 flock 已有 | 不再加一把互不相知的“桌面总锁” |
| SDK 动作状态 | action_id、execution/effect、同 session 内请求一致性和去重已有 | 不声称跨进程重启已保证 exactly-once |
| 精确目标 | scope、NodeRef/PointRef、观察依据、几何和焦点校验已有 | 不把一个裸坐标当作稳定控件身份 |
| 压缩 | XML 校验、source digest、取消 guard、替换后严格缩短已有 | 不再新造一个宽松或“严格”压缩算法 |
| 原生推理回放 | Responses opaque output、Anthropic thinking/signature 已有 | 不再次列为未实现，也不转写为普通文本代替 |
| 恢复与落盘 | 历史恢复、分步 JSON、内容寻址图片已有 | 不重跑旧任务，不宣称 Python 对象和真实桌面被恢复 |

现有实现的入口见第 6 节。它们是基础能力，不代表全部真实场景已经验收。

## 4. 为什么不能只看“completed”

以“给指定联系人发消息”为例，需要区分下面几件事：

```text
模型想发送
  → 宿主记录“发给谁、发什么”
  → 等到可用资源和有效授权
  → 调用发送动作
  → 收到技术回执
  → 读取正确会话里的实际消息
  → 核对收件人和内容
  → 才能报告业务成功
```

目前最强的基础集中在资源检查、动作回执、执行反馈和轨迹保存；两端仍缺一个统一的业务验证闭环。

- `execution=completed`：这次执行调用结束了。
- `effect=unknown`：不能确认外部结果；不是“肯定没执行”。
- “已核验”：有目标和内容对应的证据，才允许使用这个描述。
- “已中止”：不再派发后续动作；不能保证系统已经接收的点击被撤回。
- “正在检查结果”：UI 阶段文字，不是验证证据。

同理，“PDF 路径存在”不等于“PDF 已填写正确”，“Excel 保存成功”不等于“公式和单位正确”。

## 5. 当前主要问题：影响、原因、解决办法

### D01 三个前端还不是同一套完整事件系统【F/S/U】

**用户看到什么**：某个入口显示运行，另一个入口已结束；只知道模型在生成，不知道工具在等锁还是应用在等待。

**现有基础**：`AgentEvent`、App 的序号与 task/turn/attempt 防护、Hub generation、模型阶段、消息 receipt。

**基线缺口**：三端的 envelope、SDK action 关联、资源等待和验证状态没有完整统一；消息排队与工具资源排队是不同概念。Hub 每订阅者队列仍可无界增长，缺完整 cursor 重连闭环。

**本轮状态**：共享 v1 信封、动作状态、三端投影、delta 合并、有界队列和 cursor/snapshot 恢复已接入，离线回放已覆盖；真实 App/桌面验收仍未执行。

**解决**：统一结构化事件，让三端投影同一事实；增加动作归属、policy revision、来源与快照水位。高频 delta 合并，慢消费者用有界队列和 snapshot/cursor 恢复。

**验收**：同一录制事件流在三端得到相同终态；旧 delta/审批不能改变新轮；慢客户端不会无限占内存。参考 C1、A1。

### D02 动作前缺持久 journal，异常后难以对账【F/S】

**用户看到什么**：动作已经成功一半，后续报错；不知道该继续还是重新执行。

**现有基础**：cell checkpoint、SDK action_id、回执、调用后审计和未知结果提示。

**基线缺口**：`_instrument_sdk_session` 主要在调用返回或异常后记录；整个 cell 的 checkpoint 不能替代每个 SDK 调用派发前的持久意图。

**本轮状态**：SDK 公开副作用调用接入宿主持久日志 ACK 闸门，回执独立落盘，恢复先展示分类并告知模型，不自动重放旧 cell；业务验证器属于 D03，本轮不将 `unverified` 自动升级。

**解决**：每个动作先记录 intent，持久化失败就不派发；回执到达立即落盘；验证单独记账。保留 append journal，再生成兼容的 `traj.json` 投影。恢复时先对账，不从头重放 cell。

**验收**：分别注入“派发前崩溃”“派发后 receipt 丢失”“receipt 已到但未验证”；三种恢复状态不能混为一谈。参考 H1、C2。

### D01/D02 本轮落地：使用方式与保证边界

#### 事件：三端解释同一事实

- 新增 `event_protocol.py`：`EventClock`、共享信封校验与终态映射；`AgentEvent.to_wire/from_wire` 保持旧入口兼容。
- v1 必需字段：`schema_version=1`、`event_id`、`sequence>=1`、`session_id`、`task_id`、`turn_id`、`step_id`、`timestamp`、`kind`、`origin`、`policy_revision>=0`、`text`、`step`。归属与 step 可以是 `null`；整数不接受 bool/浮点。新源另带 `stream_id`，兼容旧 v1 可缺该字段。
- 动作关联保留 `action_id`、`sdk_session_id`、`cell_request_id`、`resource_id`、`observation_id`、`record_sequence`；SDK 的 `command_id` 保留在原请求，不改写其幂等请求字段。
- 原生 transport UUID 保留为 `native_task_id`；规范 `task_id` 属于 harness。旧 `event` alias 仅作原生视图适配，`kind` 和源事件身份保留。
- UI 分开显示模型等待、工具 `waiting_resource`、派发、运行、技术回执未验证、结果未知和历史恢复报告。工具排队来自 SDK `action_status`，不另造桌面锁；消息队列仍按完整轮次 FIFO 消费。
- reasoning/code/intent 高频 delta 在源头约 30ms 或 8192 字符合并；task/turn/attempt/block/policy 不混合。迟到、重复和退役轮次不能重新打开当前轮次或审批。
- Hub 每订阅者队列最多 **256** 帧，backlog 最多 **2000** 帧，动作/恢复展示摘要最多 **64** 项；慢订阅者溢出后收到 `resync_snapshot`，生产者不阻塞。
- SSE `id` 是 Hub 投递 `seq`，不是源 `sequence`。初次可用 `/api/events?after=<cursor>`；重连 `Last-Event-ID` 优先。游标过旧返回 `snapshot` 与水位 `cursor`，水位之后继续接实时帧；非法、负数或未来游标返回 400。
- 快照/恢复报告标为 `view_only=true`、`authoritative=false`：只能修复展示，不产生审批、业务成功或自动重试。源事件序号与快照水位不能混用。

#### 动作日志：派发前落盘，恢复不重跑

无需新增开关：`mac_agent.run_agent` 为当前 run 创建 `actions.jsonl`，TUI、demo Hub、原生 Host 都经过同一执行器链路。

```text
SDK 公开副作用调用
  → worker 提交 intent_recorded
  → 宿主 append + fsync 成功，返回 ACK
  → 授权/overlay 等现有门禁
  → 宿主持久记录 dispatching，返回 ACK
  → 原 SDK 调用
  → receipt_received 独立 append + fsync
  → cell 的后续代码
```

- intent 或 dispatching 的写入失败：**不调用该动作**；回执落盘失败：停止后续派发，保留已产生的可能副作用，不谎报 cell 成功。
- 文件以 0600 保存，拒绝符号链接，短写循环处理，append 记录包含序号与哈希链；新文件同步父目录。损坏/未完成尾行报 `CORRUPT`，不自动截断、覆盖或删除日志。
- 完整原始请求用于摘要比对；敏感字段键做脱敏，但正文仍可能包含隐私。日志是本地私有数据，不能直接当可分享报告。
- SDK 原始 `execution/effect` 单独保留；本轮 `verification=unverified`。模型答案或技术 `completed` 都不能替代业务验证。
- `traj.json` 继续兼容，新增 `action_journal` 路径/序号、`actions` 小索引和恢复时的 `action_recovery`；真正对账来源是 append journal，不是该展示投影。

| 崩溃窗口 | 恢复分类 | 安全含义 |
|---|---|---|
| 只有 `intent_recorded`，尚未进入派发边界 | `not_dispatched` | 不能说已执行 |
| 已持久记录 `dispatching`，没有可用 receipt | `outcome_unknown` | 派发边界不证明 OS 已接收；也不能断言没有副作用 |
| `receipt_received` 已落盘但未验证 | `receipt_unverified` | 保留技术事实，业务结果仍需核实 |
| 明确拒绝/派发前取消且未应用 | `rejected` / `cancelled_before_dispatch` | 保留 SDK 已确认的未应用事实 |

继续使用现有 `--resume <run目录>` 或 App 的历史会话入口。恢复沿用原 session 身份，先显示上述只读报告，并把分类作为首轮反馈交给模型；等用户新消息，不自动从头运行旧 cell。旧会话授权和 Python/SDK 对象不恢复。

**限制**：这不是跨重启 exactly-once。新 SDK session 重用旧 ID、未知结果的同 ID 重试或不同请求复用 ID 都被拒绝；live SDK 的同 ID/同请求已完成回执可沿原 SDK 内存幂等返回。新 ID 的语义重复不能仅靠本日志防住，仍必须先核对目标。Rust 内部组合调用可能只记录外层公开操作；任意 raw Python、subprocess 或绕过公开 SDK 的 core 调用不是 OS 沙箱内的强制审计保证。

#### 本轮验证与未验证范围

- `test_event_replay.py`：同源信封经 TUI、Plain→Hub、NativeHost；另将录制的 Native 帧喂给**真实 Swift AppModel + 离线 Bridge**，检查成功/失败/中止/新轮终态与动作事实一致，拒绝迟到审批/delta，且不派发命令。
- `test_event_stream.py`：队列压力、游标补发、过期/未来游标、快照注册无空窗、浏览器真实 JS 函数离线回放、恢复摘要保留和有界计数。
- `test_action_journal.py` / `test_action_journal_executor.py`：三种故障窗口、fsync/短写/损坏、审批与 overlay 门禁、即时回执、取消/超时、SDK 公开 File/Shell 的真实 `command_id` 形状。
- `test_journal_agent_integration.py`：真实 Agent 主循环、worker 与宿主落盘/恢复接线；模型、桌面和设备适配器为 fake，不调用真实 API 或 GUI。
- D01/D02 收尾时全量离线回归：**642 passed，2 subtests passed**，包含同源 Swift AppModel 回放；VoiceRouter 与本地化独立 Swift 契约通过，生产 Sources typecheck 无错误/警告。D04 后当前测试数见下节。IDE 诊断插件不可用，未将该工具当作通过证据。
- Swift 契约包含 408 个畸形 v1 帧拒绝断言。直接从 shell 启动临时 router 二进制曾被 AMFI 签名校验终止；仅重签临时测试文件后经 Python subprocess 运行，完整契约返回 0。未调整系统签名策略或用户应用权限。
- CI 已加入本轮 Python 回归，Swift 原有契约入口继续执行；本地通过不代表远端 CI 已运行。
- **没有启动/安装/替换 App，没有打开 Hub 浏览器或操作真实桌面，没有验证真实业务结果，也没有提交代码。**这些测试只完成确定性与本地进程闭环，不算 L2/L3。

仓库根的离线复验命令：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests packages/macos/tests -q -p no:cacheprovider
```

### D03 没有统一的业务结果与交付物验证【F/U】

**用户看到什么**：回复说完成了，但文件打不开、字段未填或结果不是指定目标。

**解决**：建议新增 `CompletionResult`/`ArtifactManifest`，保存路径、类型、来源动作、校验方法、证据和未完成项；先覆盖文件保存/下载，再逐步覆盖业务提交。

**验收**：文件不存在、格式损坏、内容不符都不能被技术 completed 覆盖。业务提交成功必须核对真实目标，不能只信“发送”按钮返回。

**注意**：不要求每个普通点击都做昂贵验证；关键提交和最终交付必须有明确验证计划。

### D04 AX 可读性与可操作性没有充分分开【F/S】

**用户看到什么**：模型反复点击静态标签、反复 set_text，或者不停算坐标而没有进展。

**现有基础**：节点动作信息、能力和引用校验、图像映射、原生动作错误。

**最初建议**：查候选、能力解释、条件等待和执行后读回。用户随后明确不需要新增 `find/inspect/wait_for/act_checked` 四类 helper，当前方向改为宿主自动给出规则级事实反馈；规则无法可靠核验时不勉强下结论。参考 P1/P2 的可解释失败原则，不照搬其完整 locator/等待体系。

**本轮实现：无需模型主动 emit 或自行判断**

- 新增内部 `action_feedback.py`，仅供 runtime/journal/executor 使用，没有新增模型 API、桌面操作或固定等待，也不修改 SDK 返回值和错误码。
- journal 回执落盘后，宿主即生成关联 `action_id` 的 `rule_feedback` 并随 D01 `action_status` 发布；cell 的自动反馈随后带进下一次模型请求。模型代码忽略返回值、捕获异常或不 `emit` 时，也不会隐藏这些事实。执行期间不会另开模型请求或打断当前 cell。
- 保留原始 `execution/effect`、阶段、SDK 错误码、可用的原生错误码、投递机制、SDK 耗时与已知证据类型。只陈述已报告的事实，不将鼠标移动、代码返回 0、AX role 或页面没变化当作成功/失败证据。
- 自动区分：宿主明确的派发前拒绝、SDK 明确未应用、执行错误、结果未知、SDK 报告的 `suspected_noop`/`partial`、调用结束但未核验，以及已有观察返回的不完整/不可用状态。SDK 的 rejected/not_applied 不一律描述成“派发前失败”。
- 已知原生诊断如 `AXValue is not settable`、窗口/键盘目标歧义、目标不在前台、AX 窗口映射未解析，匹配后输出固定短说明。任意原生自由文本可能含正文、凭证或指令，新增规则通道不原样回灌；完整诊断仍以私有日志为准。这不改变旧的显式输出/traceback 行为。
- 普通超时不改判为 `DEVICE_OFFLINE`；未知副作用提示先查原 action_id/当前状态，不自动重试。回执写失败不能被说成“动作未执行”；意图/派发 ACK 失败才可明确没有调用动作。
- 每 cell 最多保留 64 条事实，问题/未知结果优先保留，自动文本最多约 6000 字符；省略数量明确显示，不以省略代表成功。同动作的重复反馈去重，不扩张原始请求或截图。

**刻意保留的边界**：本轮统一 `condition=not_checked`、`verification=unverified`。当前 SDK 没有通用、可靠的点击后置条件证据，因此不生成“条件满足/条件超时”等业务核验结论，不额外观察、不重选节点、不自动改走视觉或前台。双同名输入框的自动消歧、PDF 输入控件识别和真实读回验证仍是后续缺口，不能把这次反馈改造当作已全部解决 D04。

**验证**：新增 17 项确定性/本地 worker/真实 Agent 主循环测试，覆盖原始返回不变、原生诊断脱敏、未知状态不被覆盖、观察不完整、不依赖 emit、捕获异常仍有反馈、cell 结束前发布、下一模型请求确实收到事实；既有日志故障注入新增派发前/回执写失败的反馈断言。规则反馈切片收尾时全量 **659 passed，2 subtests passed**（后续迭代计数见记忆管理节），CI 已纳入该测试。模型、SDK 设备适配器与桌面均为离线 fixture；没有操作真实 App、替换安装包、使用 CUA 或提交代码。

#### D04 后续：AX 能力证据来源已补

- SDK `Node` 保留 `editable` 兼容旧代码，并新增默认值字段 `editable_source`、`value_settable`、`value_settable_source`、`actions_source`；旧 payload 的来源默认 `unknown`。
- macOS 的 `editable_source=role_hint` 明确表示 role 推导；AX 文本展示为 `editable=role_hint`，不再把它表现成原生可写性证明。
- 当前锁定的 CuaDriver 0.36 元素输出没有可靠的 AXValue settable 字段，因此 `value_settable=None`、来源 `unknown`。只有原生明确报告并标记来源的布尔值，归一化才能保留 native 可写性结论；未改变外部签名驱动。
- 原生非空动作列表才标为 `actions_source=native`；缺失/空列表为 unknown，不能用空列表证明节点不可操作。仍保留 SDK 原动作校验，不自动挑首个、不试写、不新增那四类 helper。
- 静态标签、TextField、旧 payload、未知/异常来源、映射后为空的原生动作列表都有离线回归。`AXValue is not settable` 只能证明该写值路径不可用，不等于该节点一定是静态标签或不能真实键盘输入。
- **使用新字段必须重建 SDK 与 App runtime**；`--skip-runtime` 会保留旧 SDK，不适用于本轮。当前测试使用源码加隔离构建的 `_native`，未修改共享 `.venv`、已安装 App 或 CuaDriver。旧 wheel 对新增节点字段可能不兼容，不能混用新 native 与旧 Python Node。

### D05 SDK 锁与人机调度没有完整贯通【F/S/U，部分待原型】

**用户看到什么**：不知道谁占用电脑；真人操作时 Agent 仍可能继续争抢焦点。

**已有**：SDK Authority/冲突域 FIFO/flock、产品前台授权和撤回、本轮取消、气泡遮挡避让。

**缺口**：owner 可见性、用户活动识别、确认旧调用排空后的让出、全局急停。气泡警告不等于实际检测或自动抢占。

**解决**：给既有资源添加产品身份和状态映射；将“同意接管”和“当前能安全派发”分开。先做小原型，区分真人输入、Agent 注入和不明来源；让出时停止后续派发并核对 in-flight，再释放/转移资源。

**验收**：不被自己的鼠标注入触发暂停；不绕过 SDK 锁；急停不依赖聊天轮结束。真人识别准确率与误判必须实测。参考 C1 和 H2；不宣称外部产品已经解决了共享桌面问题。

### D06 完整结果不能统一按需取回，预算解释不足【F/S/U】

**用户看到什么**：长输出被截断；多轮后找不到原始数据；超限时不清楚是哪部分太大。

**已有**：输出限额、文本反馈裁剪、图像落盘、token estimator 和输出预留。

**解决**：建议新增 ResultRef，先保存完整文本/结构化结果，再返回来源、长度、摘要和分段读取接口。价格、表格等确定性数据由保存的结构化行计算，不从摘要重建。

**验收**：截断可见、原文可读、引用有身份/生命周期；大结果不会迫使模型反复生成同一查询。参考 H3。

### D07 provider 配置、缓存和压缩质量缺系统评测【F/S】

**缺口**：能力 profile、分项 ContextBudget、跨 provider 历史处理、缓存失效解释和压缩语义评测。

**解决**：区分稳定指令、用户消息、工具文本、图片、opaque reasoning 和输出预留；保持原生回放和严格摘要。稳定 SDK/Skill 前缀与动态状态分开，但不能为了缓存隐藏最新授权。

**验收**：摘要保留“不发送”、拒绝、证据位置和未知动作；改变模型/protocol 不强行回放不兼容历史。usage 缺失不写零；加密字段字符数不等于实际 reasoning token。

**后做**：没有语义质量基线前，不默认启用后台候选摘要。参考 C3、H4。

### D08 浏览器和后台操作缺完整 owner/因果链【F/S/U，部分待原型】

**用户看到什么**：多出 tab、导出还没结束但聊天显示完成、失败后资源是否残留说不清。

**已有**：隔离 headed browser、后台创建路径和 owned cleanup。

**解决**：记录 browser/profile/context/tab 的创建原因、opener、触发动作、owner 与终态；异步下载/导出单独展示，不让整个聊天永久 running，也不提前宣告文件已交付。

**验收**：启动成功但连接失败能清理自己的资源；不杀用户 Chrome；主动 tab、网站 popup、OAuth、系统文件选择器分别测焦点行为。

### D09 本地 HTTP 与轨迹分享需要安全边界【P/S/U】

**代码事实**：demo_hub 的部分写接口有 Origin 检查，但 `/api/message` 等没有统一的同等入口检查；不是“所有接口都毫无保护”。原生 App 私有管道与 HTTP 应分别讨论。

**解决**：为读取/SSE/写入建立统一的绑定地址、Origin/token 与会话策略；明确公开静态资源/预检例外。分享用脱敏投影，覆盖正文、截图、路径、命令输出，不只去掉 key/base64。

**验收**：越权请求被拒绝，分享预览可核查，删除策略覆盖派生副本。当前没有复现完整攻击链，不写成“任意网页一定能控制电脑”。

### D10 任意 Python 不等于安全沙箱，依赖安装边界不清【P/F/S】

**现有边界**：SDK 拦截保护经 SDK 的路径；任意 Python/Bash/subprocess 不因此被 OS 隔离。

**解决**：明确本地可信代码模式，先补进程/文件/网络/桌面资源归属、执行预算和审计。安装前检查已有环境，选定解释器，分开基础依赖、按需依赖与用户批准安装。

**验收**：安装可取消、有输出、有期限；页面/文档文字不会自动升级为权限或长期指令。

**不做伪保证**：import 黑名单、字符串扫描、增加一段 prompt 都不能构成可靠沙箱；VM/独立用户会话属于另一个产品范围。

### D11 签名与多版本来源导致权限和更新体验混乱【U/S，已遇到】

**实际问题**：此前系统辅助功能开关开启，但 tccd 因旧授权签名与新 ad-hoc cdhash 不匹配而拒绝；多个构建副本同时运行也使用户难辨版本。

**解决**：发布/测试版本分开；展示源码版本、bundle build、运行路径和签名类型。正式更新复用稳定签名身份；没有证书时明确本地测试限制，不默认让用户反复重置权限。

**保持**：不修改 TCC、不自动生成/导入证书、不清钥匙串、不偷偷覆盖用户 App。系统权限与 session 接管授权分别显示。

**验收**：用户能说明自己跑的是哪一版；在指定签名条件下测试更新后的权限识别。此项不是简单改变 App 名称。

### D12 钉钉 Skill 是通用路由说明，不是操作手册【F/U，已遇到】

**基线内容**：原 Skill 约 36 行，主要讲 MCP/DWS/GUI 路由；本轮已改为约 126 行的身份、联系人/群、草稿、授权、单次发送、失败恢复配方，会议室流程放在按需读取的 `meeting-room.md`。

**当前状态**：首批按用户选择只做钉钉；MCP 不再是路线，默认已登录 GUI，已有且身份正确的 DWS 仅在用户方式允许时使用。补充版本/来源/验证状态，与新 AX provenance 和 D04 自动反馈保持一致。固定索引/坐标、默认选首个、未知结果重发、授权混用均有离线契约约束；没有实际操作钉钉，标为 `offline-contract`，不是 `real-GUI-verified`，未编造会议室按钮路径。

**实际问题**：此前焦点丢失被模型误当作需要再次征求接管。实时授权状态和焦点错误说明已补入模型上下文，离线契约通过；真实钉钉任务尚未重测，应作为回归案例，不能称已证明模型再也不会重复询问。

**解决**：先整理一个脱敏会议室流程：准确窗口→入口识别→确认导航成功→读取可用时间/必要字段→填写→提交前核对→提交后证据。记录 AX 与自绘 webview 的实际能力和回退条件。

**验收**：已授权失焦只重新激活准确窗口；不重复点旧页面；只询问必要业务信息；无法验证时承认未完成。

**边界**：不恢复 MCP，不猜公司内接口，不安装未知 DWS；不能靠更长 Skill 假装支持不可操作控件。真机提交须使用测试目标并取得授权。稳定的 GUI 工作偏好可提出 memory 候选；权限授权、旧坐标/句柄不能变成永久许可。

### 本地记忆管理：短期复用，长期候选经确认后生效

这是产品 OpenGCUA 的 memory，不是开发助手自身的记忆服务。

- **短期**：仍由 ContextManager、对话轮、TODO、严格 compaction、journal 与 resume 维护。不会复制第二份短期数据库，恢复也不重跑旧动作。
- **长期**：新增 `memory_store.py` / `memory_runtime.py`，本机 SQLite，类型为偏好 `preference`、流程 `workflow`、事实 `fact`。条目有来源、状态、revision；候选最多 128 条、已批准最多 256 条、正文最多 2000 字符。目录 0700、数据库 0600，拒绝明显不安全链接；这些不是 OS 沙箱。
- **主动候选**：模型可在已有规划代码中 `await memory.propose(title=..., content=..., kind="workflow", app="钉钉")`。不另开提取模型请求、不扫描历史聊天/截图、不额外观察。宿主绑定实际来源，模型不能通过该接口批准、修订或遗忘记忆。
- **确认生效**：未批准内容绝不召回；用户确认仅表示允许后续参考，不是业务验证或权限批准。修订已批准条目会退回候选，重新确认后生效；revision CAS 拒绝过期审批，不静默覆盖不同内容。
- **检索**：每次模型请求重新从当前数据库读取。已确认的全局偏好参与参考，应用专属条目需要应用词匹配，其他条目按相关词法检索；最多 5 条、正文预算约 4000 字符。参考区只注入一次普通数据，不成为 system 权限，不覆盖最新消息/真实观察。不是向量语义检索，也没有自动验证模型经验为真。
- **遗忘**：拒绝/遗忘会删除 memory 库正文，后续请求不再注入，并在当前会话标记旧 id 失效。已经写进过去的运行记录/模型请求、备份或模型服务的数据不会因此擦除；不能声称全局彻底遗忘。
- **安全边界**：拒绝明显凭证、永久免确认承诺及易失定位内容；规则只是尽力筛查，用户仍需审阅正文。候选不会继承前台、发送或购买权限。

**管理入口**：App 会话控制面板新增“长期记忆”，展开刷新、分页查看全文、确认/拒绝候选、编辑后重新确认、遗忘；ACK 仅受理，目录回执后才改变状态。目录丢失/超时变为未确认只读，刷新后核对，不自动重发。

TUI 命令（记忆 id/revision 从目录读取，不送给模型）：

```text
/memory list
/memory list 64
/memory show <id>
/memory approve <id> <revision>
/memory reject <id> <revision>
/memory update <id> <revision> <新正文>
/memory forget <id> <revision>
```

默认与 App 共用 `GENERALIST_DATA_DIR/memory`，未设置时为 `~/Library/Application Support/Generalist/memory`。CLI 可 `--memory-dir` 指定测试目录，`--disable-memory` 停止该次运行的提案和召回；App 的模型配置文件不能重定向宿主记忆目录。首次只读列表不创建数据库。单页最多 64 条，能翻页管理全部条目。

**该轮验证结果与边界**：记忆/AX 迭代收尾时使用源码和隔离新 SDK，全量 **741 passed，2 subtests passed**；SDK Rust **35 passed**；Swift Memory/Localization 契约与完整 Sources typecheck 通过。SQLite、worker ACK、真实 Agent 主循环、NativeHost 与 Swift AppModel 离线契约覆盖候选隔离、修改复审、遗忘、过期 revision/命令、分页及目录丢失。临时 Swift 二进制被 AMFI 拒绝时使用同源 JIT 回放，不跳过断言、不更改系统策略。没有真机 GUI 或钉钉业务验收，未替换安装包、未提交；远端 CI 尚未执行。

### 2026-10-22 夜间迭代：环境、上下文与 App

本轮用户要求不再询问、每个 feature 单独 commit、先 TUI 验证；App 的真机端到端由用户后续完成。

| 提交 | 范围 | 用户可见变化 |
|---|---|---|
| `64a8857` | 保存之前的统一事件、journal、AX 与候选记忆基线 | 保留夜间迭代前可回退起点 |
| `0c09438` | 环境自动反馈与工具职责说明 | SDK 尚未进入请求时的参数/目标/能力校验异常，即使被捕获也会进入自动事实反馈；`mu.help("tools")` 解释职责、同步/异步和回执边界 |
| `40953a1` | 上下文分项与未核实操作索引 | TUI `/status` 显示预算组成；未知/部分/疑似无效动作不会被摘要覆盖后悄悄丢失；手动规划重试会重新读取已确认记忆 |
| `94a571f` | App 环境事实卡与上下文面板 | 可展开每条动作的机制、错误和诊断；完成后独立提示未核实动作；查看输入/输出预留、图片、摘要、保留轮数与长期参考数量 |
| `8393391` | 中止后的恢复原因说明 | 区分主动中止与意外退出，仍明确本轮代码未执行，不自动重放 |

**环境反馈范围**：公开 mutation 前置包装只对已经核对其真实派发入口的方法生效；采用入口探针证明未派发，不凭错误码猜。SDK/Rust 内部隐含子动作、raw Python/native core 绕过、未逐条核对的 Browser/Tab 前置路径不在该强保证范围内。原始 execution/effect、journal 闸门、FIFO 与权限不变；SDK 正常结束仍不等于业务已达成，不会自动重试未知副作用。

**上下文取舍**：参考 [Codex rust-v0.77.0 的 compact.rs](https://raw.githubusercontent.com/openai/codex/rust-v0.77.0/codex-rs/core/src/compact.rs) 的最新用户原文保留、[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/typescript) 的本地用量分类，以及 [Hermes](https://hermes-agent.nousresearch.com/docs/developer-guide/context-compression-and-caching) 的截断可解释性；不照搬循环、协议、默认 token 数，也不多发计数/摘要模型请求。

- 原有严格摘要/digest/取消门禁、用户原文和 provider opaque 回放不变。新增启发式四分项 text/images/opaque_reasoning/overhead，合计等于旧启发式总量；usage 校准只影响总量，不把分项伪装成实际用量。
- 风险索引单独作为有界只读数据，最多 64 条/3000 字符，来自持久动作状态和执行控制，不解析模型自述或 stdout；不进入 LLM 摘要，不变成授权/verified。普通技术回执不会制造错误；恢复的未核验回执与已有未知、部分/疑似无效记录保守保留。
- 长期记忆在发送前、摘要后和手动规划重试时刷新；遗忘后不再注入当前参考区。已送出的模型请求、历史或摘要转述不能因此可靠物理抹除，不能宣称全局彻底遗忘。

**App 取舍**：只显示固定白名单事实，不回灌原生自由文本；参数校验失败、不支持操作、SDK 拒绝与业务未核验是不同概念。审批/问题/API 状态保持显示优先级，不将一次普通点击的未核验变成整轮失败。中止、暂停、结束会话、新对话和退出的 tooltip 说明分别明确，没有新增强制权限或强制退出机制。

**TUI 验证**：新增真实 `pty.openpty` + `Popen` 端到端测试，键入 Enter/Ctrl-C/Ctrl-D 和 `/status`、`/help`、`/queue`、`/memory list`，覆盖自然语言 FIFO、未知回执反馈、上下文面板、挂起队列、纠正与退出回收。终端、Agent 主循环、worker、journal 为真实代码；模型与 SDK 设备为离线替身，HOME/runs/memory 为临时目录。这不是所有真机 action 正确性的证明。

独立复验（先按 SDK 源码完整构建，不能把新 native 与旧 wheel 混用）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/harness/runtime .venv/bin/python -m pytest packages/harness/runtime/tests/test_tui_pty.py -q -p no:cacheprovider
```

**本轮最终验证**：当前树配合隔离构建的新 SDK，全量 **818 passed，2 subtests passed**；包含两项真实 PTY TUI 端到端。App 的完整 Sources typecheck、真实 AppModel + fixture 的 VoiceRouter/Memory/Localization 契约分别通过，不把 pytest 计数当作 GUI 验收。完整 App/SDK 重建后再做用户端到端测试；未覆盖 Applications、未改 TCC/钥匙串、未 push。

TUI 原始屏幕/事件证据保存在工作区 `tui-validation/`，只含离线 fixture 数据。功能可逐个用对应提交执行 `git revert <commit>`，无需 destructive reset；涉及 UI/上下文依赖时优先从最新提交往前撤回。每批之后均运行 `git diff --check`，未跳过 Git hooks。

### D13 状态所有权分散、测试证据容易混用【S/U】

**结构问题**：mac_agent、worker、NativeHost、Swift、Hub 分别管理一部分状态；修复可能只贯通其中两端。文件大只是症状，不是根因。

**解决**：先定义事件/命令/终态的唯一提交者，锁定回放契约，再逐步提取模块。建议新增的 event_store/action_coordinator/verification_service 等，不是当前已经存在的文件。

**验收**：前端只能投影事实，不自行授予权限或推断成功；模拟、真实进程、受控 App、共享桌面验证分别报告。

## 6. 哪些代码耦合，必须一起修改

下表路径均相对仓库根；这是修改前必须核查的链，不表示每次都要改链上的全部文件。

| 修改主题 | 必须联动检查的代码 | 最容易漏掉什么 |
|---|---|---|
| 事件与阶段 | `runtime/agent_events.py` → `mac_agent.present/model_event` → `native_host.NativeFrontend/TaskControl.emit`；`terminal_tui._consume`；`console_server/demo_hub`；Swift `RuntimeBridge` → `AppModel.receive/SessionControls.accept` → View | 在 TUI 打印了但 App 没收到；旧流重启新轮；模型校验被当业务验证 |
| 消息/中止/终态 | `terminal_interaction.py`、`native_host.py`、`mac_agent.with_abort/_settle/run_agent`、`code_executor.interrupt_active`、Swift `SessionControls` | 控制命令混入聊天；中止后旧队列自动运行；取消等待导致整个 session 结束 |
| 前台授权 | `gui_approval.GuiGrantStore`、`code_runtime.gui_action_scope/_request_gui_approval`、`CodeExecutor.approval_handler`、`mac_agent.approve_gui/apply_policy`、NativeHost 审批、Swift policy/approval | UI 显示批准但模型不知道；较新的撤回被旧 allow 覆盖；把失焦当缺授权 |
| Skill 单槽 | `skill_registry`、`SkillContext`、`SkillRuntime`、`CodeExecutor.set_skill_selection/skill_handler`、`code_runtime` 配置同步、`ContextManager.build_messages`、恢复与 Swift selector | worker 已加载但 parent 未及时 pin；明确空槽恢复成旧 Skill；pending 被 effective 覆盖 |
| 日志与恢复 | `code_runtime._instrument_sdk_session`、`code_executor`、`TrajectoryWriter/checkpoint`、SDK `Authority.begin/finish`、`resume_catalog/native_host.resume_path`、前端恢复展示 | 只存 cell；副作用已生效但回执丢了；恢复重放旧动作 |
| AX/视觉 | SDK `unified/ui.py`、`unified/_capture.py`、`unified/observations.py`；Rust observations、macOS provider/actions/managed_input；执行审计与模型能力说明 | 静态标签被当输入框；默默升级前台；图像尺寸/坐标源与回执不一致 |
| 资源与调度 | Rust `runtime/src/{lib,scheduler,embedded}.rs`、macOS `setup.acquire_lease/provider`、执行器/浏览器 owner、NativeHost 与 UI | 产品又造一把锁；释放 lease 时旧原生调用还没结束 |
| 压缩/预算/引用 | `ContextManager`、`llm_compaction.maybe_compact`、`model_metrics.TokenEstimator`、`model_stream`、Skill slot、轨迹与 UI 控制回执 | 摘要覆盖新历史；原生推理被扁平化；原始结果截断后无法取回 |
| 气泡/输入文案 | Swift `AppModel`、`SessionControls`、`TaskOrbView`、`Views/VoiceBar`、`MiniWindowPlacement`、`OpenGCUAApp`、本地化 | 只改文字不调整高度；手动隐藏后进度更新又弹出；旧 intent 混入重试 |
| 构建/权限 | `scripts/build.py/signing.py/package.sh`、`MacPermissions.swift`、bundle Info.plist、CI/契约测试 | 源码更新但 App 未更新；更换 ad-hoc 身份使旧授权不匹配 |

`runtime/` 指 `packages/harness/runtime/`；Swift 文件在 `packages/macos/Sources/`。当前共享执行不是 Electron/WebView 前端；App 不通过 demo_hub HTTP 操作。

规则：**先定义行为和身份，再接 Python、TUI、App、Hub，最后验证 bundle；不能只增加按钮或只补 prompt。**

## 7. 参考哪个产品，哪些代码可以用

这些是固定版本的参考，不是建议增加新依赖。C1/H1/A1/P1 四个代表文件本次已核查可读，其余链接沿用原研究快照；没有运行外部项目测试。

| 编号 | 产品与源码 | 值得借鉴什么 | 能否直接搬到 OpenGCUA |
|---|---|---|---|
| C1 | [Codex parallel.rs][c1] | 工具准入期间可取消；等待与执行分别计时；一次终态 | 借鉴契约和测试；Rust 类型依赖其 Session/StepContext，不整段复制，更不替换 SDK 锁 |
| C2 | [Codex rollout recorder][c2] | append 日志、writer 的 persist/flush/shutdown 边界 | 可以按思路写小型 journal；持久化契约和故障注入测试要适配本项目 |
| C3 | [Codex prepared compaction][c3] | 候选摘要绑定历史身份，提交时校验 | 后续优化参考，不替换现有严格压缩安装路径 |
| A1 | [Claude Python SDK query.py][a1] | 控制请求与普通输出分路由、pending/request ID、取消唤醒 | Python 中较容易借鉴小组件；不能据此声称掌握 closed binary 的模型循环 |
| A2 | [Claude SDK subprocess transport][a2] | 有界 EOF/terminate/kill 的进程回收 | 参考清理边界；不能无确认地强杀用户应用。退出超时方案仍需单独设计和授权 |
| H1 | [Hermes turn_tool_round.py][h1] | 副作用前先持久化，持久化失败停止执行 | 最适合借鉴执行顺序；它记录工具轮，本项目必须继续细化到 cell 内每次真实动作 |
| H2 | [Hermes turn_facade_lease.py][h2] | 会话 writer 所有权/epoch，失去所有权后中止 | 借鉴会话写入资格，不作为另一套桌面锁 |
| H3 | [Hermes tool_executor.py][h3] | 大结果保存、引用与增量结果提交 | 借鉴 ResultRef/结果预算思路；不整套搬 SQL/工具注册层 |
| H4 | [Hermes context_compressor.py][h4] | 摘要前减少巨大结果，保护首尾、检查异常摘要 | 只提取适用策略，保留现有 XML/digest/严格缩短校验 |
| P1 | [Playwright server/dom.ts][p1] | 可见/可用/稳定/命中检查，有界条件重试 | 借鉴 actionability 设计；这些是浏览器机制，不是原生 AX 已实现的能力 |
| P2 | [Playwright locator.ts][p2] | 动作前解析目标、歧义和重定位 | 借鉴语义 locator；AX 信息不足时不能承诺浏览器同等稳定性 |
| Q1 | [Anthropic computer-use demo loop][q1] | 顺序批次、失败后剩余操作的明确结果 | 只参考 batch/反馈语义，不照搬截图前固定等待或整套 toolset |

许可边界：Codex/Playwright 根许可 Apache-2.0；Hermes/Claude Python SDK/Anthropic quickstarts 根许可 MIT，依据原研究固定快照。复制实现前检查具体文件、依赖与通知义务；**Claude Code binary/产品许可不因 Python SDK 是 MIT 而变成 MIT。**

“有参考代码”不等于“粘贴就能运行”。建议优先借鉴控制顺序、类型边界和测试，再实现小适配器；不要直接换掉当前 agent loop。

## 8. 应该怎么一步步迭代

每一轮先有失败 fixture，再改一个可归因闭环；本轮全部门槛通过后才扩展。以下是未来实施建议，不是本次已经执行的计划。

### 第 0 轮：确定能信任的基线与更新流程【D11/D12/D13】
- 做什么：记录 commit、bundle build、运行路径、签名来源和有效配置；固定回归矩阵。选择一个脱敏钉钉失败案例。
- 联动：build/signing/package、MacPermissions、诊断 UI、现有测试/CI。
- 验收：源码、测试包和安装包来源可辨；系统授权与 session 授权不混淆；复用已有证书或明确 ad-hoc 限制。
- 不做：不生成/导入证书，不改 TCC，不先拆整个 mac_agent，不宣布通用 Skill 已会全部业务。

### 第 1 轮：三端事件一致、控制结果可解释【D01/D13】
- 做什么：建立兼容 envelope，覆盖 session/turn/step/action、序号、来源和 policy revision；旧日志由同一事件投影；补 bounded SSE/snapshot。
- 联动：agent_events、mac_agent、NativeFrontend/TaskControl、TUI、Hub、RuntimeBridge/AppModel/SessionControls。
- 验收：重复/迟到事件只结算一次；取消、授权、重连不串轮；资源等待与模型等待可区分。
- 本轮独立安全补丁：统一 HTTP Origin/token 入口、读取/SSE/写入策略，保留原生私有管道区别。
- 不做：不更改默认队列注入时机，不把所有 SDK 方法变成模型工具 schema。

### 第 2 轮：一种动作的日志和未知恢复闭环【D02】
- 做什么：先覆盖一种本地文件保存路线；派发前意图持久成功才执行，receipt 即时记录，unknown 保留并核对；兼容 traj JSON 导出。
- 联动：worker SDK interception、CodeExecutor、SDK action 身份、journal writer、恢复展示。
- 验收：三个崩溃窗口得到不同状态；journal 写失败不派发；恢复不重复保存/提交、不从头执行 cell。
- 不做：不承诺 exactly-once；不在真人消息/订单上试恢复；发送仅用 fixture。

### 第 3 轮：结果引用与文件交付验证【D03/D06】
- 做什么：建议新增 ArtifactManifest/ResultRef，覆盖上轮文件；验证存在、可打开、内容符合用户要求；长结果先完整保存再摘要展示。
- 联动：执行输出、journal、ContextManager/摘要引用、历史与前端打开/验证状态。
- 验收：坏文件不成功；原始结果能分段读取；摘要/恢复不丢证据位置。
- 不做：不全面修改所有 SDK 返回类型，不用“另一个模型说没问题”代替确定性检查。

### 第 4 轮：高频 AX helper 和条件等待【D04，兼顾 R06】
- 做什么：只覆盖查询、点击、设置文本、目标/值等待；返回候选数、拒绝原因和最后观察。把坐标来源补到回执。
- 联动：SDK Python UI/生成契约、Rust provider/observations、执行审计、模型能力说明。
- 验收：静态标签不可写、同名多候选报歧义、目标变化停止剩余批次；多屏/缩放来源可复现。
- 不做：不静默升前台，不默认取第一个，不恢复截图时间 TTL，不删除必要 UI 等待。

### 第 5 轮：电脑资源 owner、受控让出和急停原型【D05/D08】
- 做什么：先展示已有租约和 owner；再做真人/注入/unknown 活动原型，提供停止派发、排空、让出、继续和全局急停。
- 联动：现有 scheduler/flock/Authority、执行器、owned browser/process、NativeHost、前端。
- 验收：两进程争用给出可解释等待/拒绝；自己注入不触发暂停；旧 in-flight 尚未排空时不移交派发资格。
- 不做：不建第二把桌面锁；不自动恢复；不按 Chrome 名称清场；原型未通过前不宣称可靠识别真人。

### 第 6 轮：分项预算、provider 兼容与摘要质量【D06/D07】
- 做什么：模型 capability profile、ContextBudget、ResultRef 和语义摘要评测；测缓存 usage/失效原因，再优化稳定前缀。
- 联动：model_stream、model_metrics、llm_compaction、ContextManager、Skill slot、轨迹和 UI。
- 验收：约束/拒绝/未知/证据不丢；同 provider 回放保留；跨 provider 不强行兼容；收益同时看质量、token、TTFT。
- 不做：不默认启用后台候选摘要、不引入未经用户审核自动生效的长期记忆、不放宽严格摘要校验。当前候选记忆功能独立于这轮摘要质量优化。

### 钉钉 Skill 的并行小切片
- 第 0 轮开始归档已遇到的失败；第 4 轮 helper 稳定后补具体配方，不必等所有调度功能完成。
- 第一版只保证一个测试环境中的可核验流程；先导航/读表单/定向填写但不提交，最后经授权做测试提交。
- 用户说“最近可用两小时”不代表模型可以随意补楼层、人数等必填信息；只问缺失且实际需要的信息，不扩大澄清。
- 发布前给出应用版本、入口差异、失败恢复、验证步骤和维护者；更换页面/版本后可以降为未验证。

### 后续可选，不纳入首批必做
会话 SQLite 搜索索引、可审阅经验候选、后台 prepared summary、无桌面写权限研究 worker。先有规模/质量/费用/隐私需求再做。

MCP 已被用户否定，不作为“延期后必做”；整套换 Codex/Claude SDK、默认 headless、多 Agent 同桌面、截图 TTL 不属于当前方向。

## 9. 验收怎么做，才能避免再次“测试全绿但不会用”

| 层级 | 实际验证对象 | 不能证明什么 |
|---|---|---|
| L0 确定性 fixture | 事件、队列、取消、摘要、预算、目标校验 | 不证明系统权限、真实焦点或业务结果 |
| L1 本地进程/真实浏览器 | 传输、EOF、取消、资源回收、DOM、popup | 不证明原生 AX 或真人共用桌面 |
| L2 受控 macOS App/可丢弃文件 | AX、截图、投递、坐标、焦点、文件结果 | 不证明所有钉钉版本与真实组织业务 |
| L3 明确授权的人机共用流程 | 真人活动、打扰、让出、理解、交付 | 不以一次演示代表普遍成功率 |

用户当前禁止使用 CUA 工具；因此本次没有进行 L2/L3。未来真机验收须先明确允许的工具、窗口、账号、数据与提交边界。

每轮最低场景：旧 delta 迟到、审批中取消、已批准跨轮、撤回晚于 allow、普通消息 FIFO、中断后纠正、日志写失败、receipt 丢失、验证未完、静态 AX 标签、坏产物、压缩丢约束、分享泄露。

复用测试入口：
- Python：`test_agent_frontend.py`、`test_native_host.py`、`test_native_session_controls.py`、`test_native_round_interrupt.py`、`test_sdk_session_controls.py`、`test_gui_approval.py`、`test_manual_compaction.py`、`test_skill_context.py`、`test_responses_transport.py`、`test_anthropic_thinking_transport.py`。
- Swift：`VoiceRouterContracts.swift`、`MiniWindowContracts.swift`、`MiniWindowLayoutContracts.swift`、`ConfigContracts.swift`、`TaskInputContracts.swift`、本地化测试。
- Swift 契约需要独立编译/执行，pytest 通过不代表 Swift 已验证；具体命令依 `.github/workflows/macos-app.yml`。
- 已有的真实浏览器焦点测试可能需要显式 opt-in；默认跳过不能记成通过。

指标只保留最能决策的几项：目标/结果正确率、重复副作用、未授权打扰、取消响应与停止派发时间、unknown 未闭合数量、API/资源/应用/验证分项 p50/p95、token 与成本。报告样本数、环境和未验证项。

## 10. 维护者执行清单

每个改动提交前确认：

- [ ] 先选第 5 节的具体问题编号，而不是泛泛“优化 harness”。
- [ ] 写明改的是功能、结构还是体验，预期用户行为是什么。
- [ ] 标出唯一状态提交者与第 6 节联动链。
- [ ] 新接口/模块明确标“新增”，保留旧配置和轨迹迁移方式。
- [ ] 对应失败 fixture 先能失败，修复后通过。
- [ ] 三端状态、真实 bundle 与源码版本分别核验。
- [ ] 不把 completed、截图、reasoning 或测试计数当业务成功证据。
- [ ] 列出 performed/skipped/unsupported，保留日志与未知结果。
- [ ] 不在同一批改动同时改变默认授权、存储格式、压缩规则和整个 UI。
- [ ] 先测一条闭环，再扩到更多应用；用户数据与正式业务不当测试沙箱。

**最先做的不是大重构，而是让一次真实动作能够被解释、被记录、被核验，并在出错后不被盲目重复。**

## 11. 最后汇总：主要缺陷与先后顺序

| 顺序 | 首要问题 | 对应编号 | 解决后能看到的变化 |
|---|---|---|---|
| 先建立基线 | 版本/签名/测试证据难分辨 | D11/D13 | 用户和维护者知道正在跑哪一版、哪些结果确实验证过 |
| 第一闭环 | 统一事件/有界重连已接入，真实桌面体验待验收 | D01/D13 | 同源离线回放终态一致，区分消息排队与工具等待 |
| 第二闭环 | 持久动作日志/恢复分类已接入，业务核验仍需补 | D02/D03 | 崩溃后不重放旧 cell，能说明哪些操作仍需核实 |
| 第三闭环 | 业务成功无统一证据、原始结果不能统一取回 | D03/D06 | 文件/提交结果有验证，长资料有可取回的来源 |
| 高频操作 | AX 可读却不可操作、条件等待不足 | D04 | 少做无效尝试，遇静态标签/歧义会解释并选替代路径 |
| 原型验证 | 人机让出、owner 与浏览器焦点/回收不完整 | D05/D08 | 用户知道谁占资源，能安全让出；不误清理自己的浏览器 |
| 后续质量 | 模型能力/预算/缓存/摘要语义欠系统评测 | D07 | 成本下降不会以丢失约束和证据为代价 |
| 全程伴随 | HTTP、隐私、依赖与可信执行边界 | D09/D10 | 入口和数据范围明确，不把本地 Python 宣传为沙箱 |
| 应用小切片 | 钉钉 Skill 缺具体流程与验证 | D12 | 先让一个可测试流程可靠，而不是宣称支持所有钉钉操作 |

**建议第一次迭代只选：一个统一动作事件、一个可取消等待、一个错误回放场景，以及版本来源显示。暂不同时重写权限、存储和压缩。**

## 附录：当前源码入口与参考链接

当前源码（相对本文件）：
- [共享事件](../../packages/harness/runtime/agent_events.py)
- [Agent/ContextManager](../../packages/harness/runtime/mac_agent.py)
- [原生控制/事件适配](../../packages/harness/runtime/native_host.py)
- [worker SDK 包装](../../packages/harness/runtime/code_runtime.py)
- [执行器](../../packages/harness/runtime/code_executor.py)
- [严格摘要](../../packages/harness/runtime/llm_compaction.py)
- [Swift 状态投影](../../packages/macos/Sources/SessionControls.swift)
- [SDK Authority](../../packages/sdk/crates/runtime/src/lib.rs)
- [SDK FIFO](../../packages/sdk/crates/runtime/src/scheduler.rs)
- [macOS 租约](../../packages/sdk/adapters/macos/src/setup.rs)
- [当前钉钉 Skill](../../packages/harness/skills/dingtalk-workflow/SKILL.md)
- [原始研究报告](2026-10-21-agent-harness-source-review.md)

[c1]: https://github.com/openai/codex/blob/61469096fe086ccdbe87c06f9efc4ee59a4b51fe/codex-rs/core/src/tools/parallel.rs
[c2]: https://github.com/openai/codex/blob/61469096fe086ccdbe87c06f9efc4ee59a4b51fe/codex-rs/rollout/src/recorder.rs
[c3]: https://github.com/openai/codex/blob/61469096fe086ccdbe87c06f9efc4ee59a4b51fe/codex-rs/core/src/compact_remote_v2_installation.rs
[a1]: https://github.com/anthropics/claude-agent-sdk-python/blob/c01a99416a02805b0a050d058da51bde53f6df64/src/claude_agent_sdk/_internal/query.py
[a2]: https://github.com/anthropics/claude-agent-sdk-python/blob/c01a99416a02805b0a050d058da51bde53f6df64/src/claude_agent_sdk/_internal/transport/subprocess_cli.py
[h1]: https://github.com/NousResearch/hermes-agent/blob/0319e113c9602b86ffda350e931cb4b035e1d5d8/agent/turn_tool_round.py
[h2]: https://github.com/NousResearch/hermes-agent/blob/0319e113c9602b86ffda350e931cb4b035e1d5d8/agent/turn_facade_lease.py
[h3]: https://github.com/NousResearch/hermes-agent/blob/0319e113c9602b86ffda350e931cb4b035e1d5d8/agent/tool_executor.py
[h4]: https://github.com/NousResearch/hermes-agent/blob/0319e113c9602b86ffda350e931cb4b035e1d5d8/agent/context_compressor.py
[p1]: https://github.com/microsoft/playwright/blob/27219bd2cc94e1ebceec58f188760a6bf458c192/packages/playwright-core/src/server/dom.ts
[p2]: https://github.com/microsoft/playwright/blob/27219bd2cc94e1ebceec58f188760a6bf458c192/packages/playwright-core/src/client/locator.ts
[q1]: https://github.com/anthropics/anthropic-quickstarts/blob/9ec32b91df50b0d4906cae64b13c6298055ff40d/computer-use-demo/computer_use_demo/loop.py
