# 每段 Agent 连续会话只管理一个浏览器：设计

## 目标与边界

一段连续会话（包括多轮用户消息、SDK 会话重建和网页后端切换）共享一个任务专属的、有界面的 Chrome 实例。浏览器在首次需要网页时才启动；同一实例里可以有多个标签页。正常情况下，Agent 的浏览器不抢占用户前台，也不接管用户正在使用的 Chrome。会话结束、失败或取消后，Agent 启动的浏览器必须被清理。

“一个浏览器”指最多一个由该会话拥有的 Chrome **主进程**，不是只准一个标签页或一个 Chrome helper 进程；不限制不同 Agent 会话各自拥有一个浏览器。此约束由项目的 `web` API 和宿主生命周期管理保证，**不是对模型任意 Python/Bash 代码的安全沙箱**：当前 harness 允许执行任意本机命令，模型仍能绕过 API 自行启动程序。若需不可绕过的系统级限制，另需执行沙箱或进程策略，不在本设计范围内。

## 现状与根因

- `packages/harness/runtime/code_runtime.py` 的 `reset_web()` 关闭旧 `web` 再创建资源；`_create_web_resource()` 在 SDK 浏览器失败时启动本地 Playwright shim。它们都可能触发新的物理 Chrome。
- `packages/harness/runtime/web_shim.py` 的 `create_web()` 和 `_Browser._ensure_alive()` 自行启动或重建 Chrome。SDK 浏览器 worker 也有自己的启动路径；目前没有共同的浏览器所有者。
- SDK 的 `DEVICE_OFFLINE` 可能只是先前超时动作使资源被隔离，并不能单凭错误码证明 Chrome 主进程退出。因此，把它直接当作“浏览器死了，重新启动”会制造多开和遗留进程。
- 当前 SDK worker 的 `init` 路径调用 `engine.launch()` 或后台启动函数；尚不能假设 harness 传入 `connect_url` 后它就会连接已有 CDP 浏览器。实施时必须补齐并验证这条连接路径。

## 方案选择

采用**会话级 BrowserLease**：宿主是浏览器唯一的生命周期管理者；SDK worker 和本地 shim 只是可替换的连接者。相比只在 prompt 中要求复用，它能约束项目 API 的创建路径；相比所有 Agent 共用全局 Chrome，它避免不同会话混用标签页、登录态与清理责任。默认 isolated 模式使用独立临时 profile。显式 `attach` 模式连接用户现有 Chrome，标记为**非拥有**，绝不关闭用户浏览器或清理其 profile；显式 `borrow` 模式在用户退出自己的 Chrome 后使用其 profile 启动 Agent 进程，Agent 结束时只清理自己启动的进程，绝不删除用户 profile。

## 生命周期与接口

BrowserLease 由宿主按连续会话创建一个逻辑对象，记录会话 ID、所有权模式、profile 标识、主进程 PID（如适用）、CDP 端点、连接代次、状态和清理结果。状态为 `idle → starting → ready → reconnecting → ready`，确认进程死亡时可进入 `replacing`，结束时进入 `stopping → stopped`；无法安全恢复则为 `failed`。启动、重连、替换、关闭都由同一互斥锁串行化；并发调用共享同一个进行中的操作，不得各自启动 Chrome。

`idle` 不创建 Chrome。首次 `web` 请求启动一个 headed Chrome，取得其唯一 profile 和 CDP 端点，再由 SDK worker 或 shim 建立连接。macOS 后台启动仍使用当前无激活路径。主进程的身份以 PID、启动时刻和**精确的** `--user-data-dir` 标识共同核验；`open -g -n` 可能让 Chrome 脱离启动器父进程，因此不能仅靠父子进程关系清理。端点连接还需核验它属于这份 profile，避免接入用户 Chrome。

宿主的 `web` 接口保留 `web.browser.new_tab/list_tabs` 等现有用法。`reset_web()` 对 Agent 仍可用，但语义改为“恢复或重连当前 lease”，不再默认新开浏览器。`visible=True` 只在既有前台接管授权范围内改变可见性/焦点策略；它本身不创建第二个 Chrome。旧 `web`/tab 句柄在 SDK 重建后可以失效，新句柄须重新列出标签页并绑定；物理浏览器与标签页不能仅因句柄失效而关闭。

SDK factory 每个 runtime 只注册一次，或把重复注册做成幂等。SDK worker 增加明确的 CDP attach 路径，连接 lease 管理的实例而不是自行 `launch`；shim 的创建和 `_ensure_alive()` 也只从 lease 获取端点、重连并重建必要的 Playwright context。SDK 会话重建时重新登记/连接浏览器资源，不创建第二个 Chrome。若某后端尚不支持安全 attach，返回明确错误并暂停该后端；**禁止**静默退到一个会自行启动浏览器的 shim。Agent 不获得另一个物理浏览器的创建入口，但仍可在已拥有的浏览器中新增标签页。

## 故障恢复

恢复先区分三种情况：

1. **网页动作失败或超时**：报告目标/操作错误，尽可能让 Playwright 内部超时早于 SDK 外层截止时间。未知结果先查 `action_status` 与当前 DOM/标签页，不盲目重放点击，不重启浏览器。
2. **worker、SDK session 或 CDP 连接失效**：检查主进程和 CDP 健康状态。进程仍活着时重连同一端点，刷新 `web`/tab 句柄；不得替换进程。`DEVICE_OFFLINE` 本身只触发此诊断，不是死亡证明。
3. **主进程已确认退出**：先确认旧实例及同 profile 的残留进程全部退出，在互斥锁下最多发起一次受限替换；替换后重新建立连接，并告知 Agent 旧标签页/未保存状态可能丢失。若旧实例不能确认退出或替换再次失败，停止自动恢复并向用户展示可操作的错误，不循环 `reset_web()`。

连接者不得拥有或关闭 Chrome 主进程；只有 lease 能执行物理启动与停止。为避免“进程活着但连接不上”时积累新浏览器，连接不可恢复时宁可暂停网页操作，也不能启动替身。

## 退出与异常清理

成功、失败、取消、`Ctrl-C`、输入 EOF 和 worker 异常都进入同一幂等关闭流程：尽力保存允许保存的网页状态；关闭连接者/context；对**自己启动的**浏览器请求 CDP `Browser.close`；等待退出；仍存活时只向核验过的精确 PID/profile 发送 `SIGTERM`，再有界等待；最后必要时仅对同一已核验目标使用 `SIGKILL`。确认无匹配进程后，只删除 isolated lease 的临时 profile。`attach` 仅断开连接；`borrow` 关闭 Agent 自己启动的进程但保留用户 profile。

宿主为 lease 写入最小化的本地清理记录，供异常退出后的下次启动检查。只有记录对应的会话不再活动、PID/启动时刻/profile 全部匹配时，才能清理孤儿进程；不匹配则报告人工处理，不能按进程名批量结束 Chrome。清理失败应在 CLI/demo hub 明示，并提供精确目标的清理入口。日志不记录 cookie、CDP 授权内容或完整用户 profile 数据。

## 前台行为与用户反馈

“单实例”只解决多开与清理，不自动解决网站脚本新开窗口抢焦点。沿用现有后台标签页创建与 foreground-takeover 预检；Agent 仍优先 DOM 操作，不因连接恢复而激活窗口。界面区分“正在启动浏览器”“重连现有浏览器”“浏览器已停止”“需要用户允许前台操作”，避免把普通 locator 超时显示成 `DEVICE_OFFLINE`。用户可以看到有界面的浏览器，但后台操作默认不打断其当前应用。

## 验收标准

- 非网页任务不启动 Chrome；首次网页请求启动一个任务专属主进程。连续多轮消息、多个标签页和重复 `reset_web()` 后，仍是同一个主进程/profile。
- SDK session 重建、worker 断线以及 SDK→shim 切换，都重连同一实例；可列出仍存在的标签页。locator 超时与未知动作不创建新 Chrome。
- 真实主进程退出后才允许一次受限替换；旧进程尚存、CDP 无法连接或替换失败时不得多开或无限重试。
- 正常结束、失败、取消、EOF 和异常退出后的孤儿清理，最终没有该会话拥有的 Chrome 主进程、helper 或临时 profile；用户自己的 Chrome 和其他会话不受影响。
- 默认后台打开网页和新标签页不主动抢焦点；无法保证的网站自发弹窗仍按现有前台接管机制提示用户。`attach` 断连后用户浏览器继续运行；`borrow` 退出后只保留用户 profile，不保留 Agent 启动的 Chrome 进程。
- 测试覆盖并发首次请求、十次重复 reset、SDK/Playwright 两后端交接、超时围栏、真实死亡、关闭中断、非拥有模式和精确 PID 防误杀；至少做一次 macOS 真机连续多轮与取消清理验证。

## 实施范围

预计改动集中在 `packages/harness/runtime/code_runtime.py`、`web_shim.py`、浏览器 Playwright worker/adapter、相关测试以及 Agent 的 `reset_web` 提示。先打通宿主所有权与 CDP attach，再迁移 shim/SDK 恢复路径，最后接入关闭钩子和 UI 状态。现有默认后台、有界面、独立 profile 的产品决策不变；不顺带改造原生 App 操作或任意 Bash 执行权限。
