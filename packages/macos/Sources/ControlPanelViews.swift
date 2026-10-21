import SwiftUI

struct SessionPicker: View {
    @ObservedObject var model: AppModel
    var body: some View {
        Picker(L("会话"), selection: Binding(get: { model.currentConversationID }, set: { id in
            if id.isEmpty { model.persistCurrent(); model.reset() }
            else if let record = model.history.first(where: { $0.id == id }) { _ = model.openConversation(record) }
        })) {
            Text(L("新会话")).tag("")
            if !model.currentConversationID.isEmpty && !model.history.contains(where: { $0.id == model.currentConversationID }) {
                Text(model.taskTitle.isEmpty ? L("当前会话") : model.taskTitle).tag(model.currentConversationID)
            }
            ForEach(model.history) { record in
                Text(recordLabel(record)).lineLimit(1).tag(record.id)
            }
        }.disabled(!model.canSwitchSession)
            .help(model.phase.active ? L("活动轮次中不能切换会话。") : L("选择历史只加载记录，发送新消息后才恢复执行。"))
    }
    private func recordLabel(_ record: TaskRecord) -> String {
        let date = record.date.formatted(date: .numeric, time: .shortened)
        let state = TaskPhase(rawValue: record.state)?.label ?? record.state
        return [record.title, date, state].joined(separator: " · ")
    }
}

struct SkillPicker: View {
    @ObservedObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Picker("Skill", selection: Binding(get: { model.controls.selectedSkill ?? "" }, set: { model.selectSkill($0.isEmpty ? nil : $0) })) {
                    Text(L("不选")).tag("")
                    ForEach(model.controls.skills) { skill in Text(skill.name).tag(skill.name).help(skill.description) }
                    if let selected = model.controls.selectedSkill, !model.controls.skills.contains(where: { $0.name == selected }) {
                        Text(selected).tag(selected)
                    }
                }.disabled(model.controlPending("skill_select") || (model.conversationOpen && !model.liveSession))
                Menu {
                    Button(L("刷新 Skill 目录")) { model.refreshSkills() }
                    Button(L("重新加载 Skill")) { model.refreshSkills(reload: true) }
                    Button(L("不选")) { model.selectSkill(nil) }
                } label: { Image(systemName: "arrow.clockwise") }.menuStyle(.borderlessButton).fixedSize()
                    .disabled(!model.ready || model.controlPending("skills_list") || model.controlPending("skills_reload"))
                    .accessibilityLabel(L("刷新 Skill 目录"))
            }
            if model.controls.skillPending { Text(L("Skill 切换待生效：下一消息轮使用所选 Skill。")) }
            if !model.controls.catalogAvailable { Text(L("Skill 目录尚不可用，请刷新。")) }
            if !model.controls.catalogStatus.isEmpty { Text(model.controls.catalogStatus) }
            if let skill = model.controls.skills.first(where: { $0.name == model.controls.selectedSkill }) {
                Text(skill.description).lineLimit(2).help(skill.description)
                Text(L("来源：%@", skill.source)).textSelection(.enabled)
            }
        }.font(.caption).foregroundStyle(.secondary)
    }
}

struct SessionPolicyView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Toggle(L("允许前台操作（仅本实时会话）"), isOn: Binding(get: { model.controls.foregroundAllowed }, set: { model.setForegroundAllowed($0) }))
                .disabled(!model.liveSession || model.controlPending("set_policy"))
            Toggle(L("允许助手提问"), isOn: Binding(get: { model.controls.askUserEnabled }, set: { model.setAskUserEnabled($0) }))
                .disabled(model.controlPending("set_policy") || (model.conversationOpen && !model.liveSession))
            Text(L("前台授权跨本会话轮次保留；切换或重新连接后关闭。提问开关不影响前台审批。"))
                .font(.caption).foregroundStyle(.secondary)
        }.toggleStyle(.switch).font(.system(size: 12))
    }
}

struct ApprovalCard: View {
    @ObservedObject var model: AppModel
    let approval: SessionApproval
    @ViewState private var choice = "reject"
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(L("前台操作审批"), systemImage: "hand.raised.fill").font(.headline)
            Text(approval.text).textSelection(.enabled)
            Text(L("应用：%@ · 窗口：%@", approval.appID, approval.windowID)).textSelection(.enabled)
                .font(.caption).foregroundStyle(.secondary)
            Text(L("范围：仅本实时会话；默认拒绝。")).font(.caption).foregroundStyle(.secondary)
            Picker(L("审批选择"), selection: $choice) {
                Text(L("拒绝")).tag("reject")
                Text(L("允许")).tag("allow")
                Text(L("取消")).tag("cancel")
            }.pickerStyle(.segmented)
            Button(model.approvalPending(approval.id) ? L("正在等待执行端确认…") : L("确认审批选择")) {
                model.replyToApproval(approval.id, choice: choice)
            }.disabled(!model.liveSession || model.approvalPending(approval.id))
        }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.orange.opacity(0.10), in: RoundedRectangle(cornerRadius: 12))
            .accessibilityElement(children: .contain)
    }
}

struct QueueControlsView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        DisclosureGroup(L("消息队列（%@）", String(model.controls.queue.count))) {
            VStack(alignment: .leading, spacing: 8) {
                if !model.controls.queueAvailable { Text(L("队列状态尚未收到，请刷新。")) }
                if model.controls.queueSuspended { Label(L("队列已挂起，不会自动执行"), systemImage: "pause.circle") }
                if model.controls.queue.isEmpty && model.controls.queueAvailable { Text(L("队列为空")) }
                ForEach(model.controls.queue) { item in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(item.text).textSelection(.enabled)
                        Text(item.kind).font(.caption2).foregroundStyle(.secondary)
                    }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
                        .background(.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 8))
                }
                HStack {
                    Button(L("刷新队列")) { model.refreshQueue() }.disabled(model.controlPending("queue_list"))
                    Button(L("继续队列")) { model.resumeQueue() }
                        .disabled(!model.controls.queueSuspended || model.controls.queue.isEmpty || model.controlPending("queue_resume"))
                    Button(L("清空队列")) { model.clearQueue() }
                        .disabled(model.controls.queue.isEmpty || model.controlPending("queue_clear"))
                }.disabled(!model.liveSession)
            }.padding(.top, 6).font(.caption)
        }.font(.system(size: 12))
    }
}

// 长期记忆只显示 host 目录；所有变更都需用户阅读当前条目后显式点击，状态等待目录回写。
struct MemoryControlsView: View {
    @ObservedObject var model: AppModel
    @ViewState private var expanded = false
    @ViewState private var selectedID: String?
    @ViewState private var editingBase: MemoryEntry?
    @ViewState private var draftTitle = ""
    @ViewState private var draftContent = ""
    @ViewState private var draftKind = "preference"
    @ViewState private var draftApp = ""
    @ViewState private var removal: MemoryEntry?
    private var memory: MemoryControlsState { model.controls.memory }
    private var actionsDisabled: Bool { !model.ready || model.disconnected || !memory.available || memory.busy || memory.catalogUnknown }
    private var listDisabled: Bool { !model.ready || model.disconnected || model.memoryPending("memory_list") }
    var body: some View {
        // 只在用户展开时读取，不在后台自动请求。
        DisclosureGroup(isExpanded: Binding(get: { expanded }, set: { expanded = $0; if $0 { model.refreshMemory() } })) {
            VStack(alignment: .leading, spacing: 8) {
                Text(L("记忆只作参考：用户确认不代表业务结果已核验。不得保存权限、凭证、坐标或窗口句柄；短期上下文由当前会话维护。"))
                    .foregroundStyle(.secondary)
                Text(L("记忆目录是本机私有数据，只经执行端读取与修改；网页内容不会被当作指令。"))
                    .foregroundStyle(.secondary)
                if !memory.root.isEmpty { Text(L("本地私有目录：%@", memory.root)).foregroundStyle(.secondary).textSelection(.enabled) }
                Button(L("刷新记忆目录")) { model.refreshMemory() }
                    .disabled(listDisabled)
                if !memory.received { Text(L("记忆目录尚未收到，请刷新。")) }
                else if !memory.available { Text(L("记忆存储不可用：%@", MemoryControlsState.errorText(memory.errorCode))) }
                else if memory.entries.isEmpty { Text(memory.offset > 0 ? L("此页没有条目，请返回上一页。") : L("暂无记忆。")) }
                if memory.catalogUnknown { Text(L("记忆目录状态未确认，仅可阅读；请刷新后再操作。")).foregroundStyle(.orange) }
                // 分页只列出总数；当前页以收到的目录为准，翻页不乐观切换。
                if memory.received && (memory.offset > 0 || memory.omitted > 0 || memory.nextOffset != nil) {
                    HStack {
                        Button(L("上一页")) { model.previousMemoryPage() }.disabled(listDisabled || memory.offset == 0)
                        Button(L("下一页")) { model.nextMemoryPage() }.disabled(listDisabled || memory.nextOffset == nil)
                        Text(memory.entries.isEmpty ? L("共 %@ 条。", String(memory.candidateCount + memory.approvedCount))
                             : L("第 %@–%@ 条，共 %@ 条。", String(memory.offset + 1), String(memory.offset + memory.entries.count),
                                 String(memory.candidateCount + memory.approvedCount)))
                            .foregroundStyle(.secondary)
                    }
                }
                if !memory.entries.isEmpty {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 2) {
                            ForEach(memory.entries.prefix(MemoryCatalog.entryLimit)) { entry in
                                Button { select(entry) } label: {
                                    HStack {
                                        Text(MemoryControlsState.statusLabel(entry.status)).foregroundStyle(entry.isCandidate ? .orange : .green)
                                        Text(entry.title.isEmpty ? L("（无标题）") : entry.title).lineLimit(1)
                                        Spacer()
                                    }.contentShape(Rectangle())
                                }.buttonStyle(.plain).padding(4)
                                    .background(entry.id == selectedID ? Color.accentColor.opacity(0.15) : .clear, in: RoundedRectangle(cornerRadius: 6))
                            }
                        }
                    }.frame(maxHeight: 160)
                }
                if let entry = memory.entries.first(where: { $0.id == selectedID }) { detail(entry) }
                if !memory.status.isEmpty { Text(memory.status).textSelection(.enabled) }
            }.padding(.top, 6).font(.caption)
        } label: {
            Text(L("长期记忆（候选 %@ · 已批准 %@）", String(memory.candidateCount), String(memory.approvedCount)))
        }.font(.system(size: 12))
            .confirmationDialog(removal?.isCandidate == true ? L("拒绝这条候选记忆？") : L("遗忘这条记忆？"),
                                isPresented: Binding(get: { removal != nil }, set: { if !$0 { removal = nil } })) {
                if let entry = removal {
                    Button(entry.isCandidate ? L("拒绝候选") : L("遗忘"), role: .destructive) {
                        if entry.isCandidate { model.rejectMemory(entry) } else { model.forgetMemory(entry) }
                        removal = nil
                    }
                }
                Button(L("取消"), role: .cancel) { removal = nil }
            } message: {
                Text(L("将从本机记忆目录删除「%@」；结果以执行端目录为准。", removal?.title ?? ""))
            }
    }
    private func select(_ entry: MemoryEntry) {
        selectedID = entry.id
        if editingBase?.id != entry.id { editingBase = nil }
    }
    private func beginEdit(_ entry: MemoryEntry) {
        editingBase = entry
        draftTitle = entry.title; draftContent = entry.content; draftKind = entry.kind; draftApp = entry.app ?? ""
    }
    private func sourceText(_ source: MemorySource) -> String {
        [source.sessionID ?? L("无会话"), source.turn.map { L("第 %@ 轮", String($0)) } ?? L("无轮次"),
         source.step.map { L("第 %@ 步", String($0)) } ?? L("无步骤"), source.origin].joined(separator: " · ")
    }
    private func time(_ milliseconds: Int) -> String {
        Date(timeIntervalSince1970: Double(milliseconds) / 1000).formatted(date: .numeric, time: .shortened)
    }
    @ViewBuilder private func detail(_ entry: MemoryEntry) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            if let base = editingBase, base.id == entry.id {
                if base != entry { Text(L("条目已在执行端变化，草稿基于旧版本；请取消后重新阅读并编辑。")).foregroundStyle(.orange) }
                TextField(L("标题"), text: $draftTitle)
                TextEditor(text: $draftContent).frame(minHeight: 60, maxHeight: 140)
                    .overlay(RoundedRectangle(cornerRadius: 4).stroke(.secondary.opacity(0.3)))
                Picker(L("类型"), selection: $draftKind) {
                    ForEach(MemoryEntry.kinds, id: \.self) { Text(MemoryControlsState.kindLabel($0)).tag($0) }
                }
                TextField(L("应用（可留空）"), text: $draftApp)
                Text(L("保存后条目回到候选，需要重新确认才会生效。")).foregroundStyle(.secondary)
                HStack {
                    Button(L("保存为候选，重新确认")) {
                        model.updateMemory(base, title: draftTitle, content: draftContent, kind: draftKind, app: draftApp)
                        if model.controls.memory.isPending(memory: base.id) { editingBase = nil }
                    }.disabled(actionsDisabled)
                    Button(L("取消编辑")) { editingBase = nil }
                }
            } else {
                Text(entry.title).font(.headline).textSelection(.enabled)
                ScrollView { Text(entry.content).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                    .frame(maxHeight: 160)
                LabeledContent(L("状态"), value: MemoryControlsState.statusLabel(entry.status))
                LabeledContent(L("类型"), value: MemoryControlsState.kindLabel(entry.kind))
                LabeledContent(L("应用"), value: entry.app ?? L("不限应用"))
                LabeledContent(L("修订号"), value: String(entry.revision))
                LabeledContent(L("核验"), value: MemoryControlsState.validationLabel(entry.validation))
                LabeledContent(L("来源"), value: sourceText(entry.source))
                LabeledContent(L("创建 / 更新"), value: time(entry.createdAt) + " / " + time(entry.updatedAt))
                if memory.isPending(memory: entry.id) {
                    Label(L("正在等待执行端确认…"), systemImage: "clock").foregroundStyle(.secondary)
                }
                HStack {
                    if entry.isCandidate {
                        Button(L("确认记忆")) { model.approveMemory(entry) }
                        Button(L("拒绝候选"), role: .destructive) { removal = entry }
                    } else {
                        Button(L("遗忘"), role: .destructive) { removal = entry }
                    }
                    Button(L("编辑")) { beginEdit(entry) }
                }.disabled(actionsDisabled)
            }
        }.textSelection(.enabled).padding(8).frame(maxWidth: .infinity, alignment: .leading)
            .background(.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 8))
    }
}

struct RecoveryNoticeView: View {
    @ObservedObject var model: AppModel
    var compact = false
    var body: some View {
        if !model.recoveryNotice.isEmpty {
            Label(model.recoveryNotice, systemImage: "exclamationmark.shield")
                .font(.system(size: compact ? 10 : 12)).foregroundStyle(.orange)
                .lineLimit(compact ? 2 : nil).help(model.recoveryNotice).textSelection(.enabled)
                .accessibilityIdentifier("RecoveryReport")
        }
    }
}

struct SessionControlPanel: View {
    @ObservedObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SkillPicker(model: model)
            SessionPolicyView(model: model)
            QueueControlsView(model: model)
            MemoryControlsView(model: model)
            RecoveryNoticeView(model: model)
            HStack {
                Button(L("手动压缩上下文"), systemImage: "arrow.down.right.and.arrow.up.left") { model.compactContext() }
                    .disabled(!model.canCompact || model.controlPending("compaction"))
                // 取消入口只属于会话级手动压缩；轮内自动压缩由中止本轮处理。
                if model.controls.compactionRunning || model.controlPending("compaction") {
                    Button(L("取消压缩")) { model.cancelCompaction() }
                        .disabled(!model.liveSession || model.controlPending("compaction_cancel"))
                }
            }.font(.caption)
            if model.controls.interrupted {
                Label(L("本轮已中止，可在同一会话发送纠正。"), systemImage: "arrow.uturn.backward")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if !model.controls.pending.isEmpty {
                Label(L("正在等待执行端确认…"), systemImage: "clock").font(.caption).foregroundStyle(.secondary)
            }
            if !model.controls.result.isEmpty { Text(model.controls.result).font(.caption).textSelection(.enabled) }
        }.padding(12).background(.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 12))
    }
}

private struct ActionDiagnosticView: View {
    let action: NativeActionStatus
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            LabeledContent(L("最后动作 ID"), value: action.id)
            LabeledContent(L("动作阶段"), value: action.stage)
            LabeledContent(L("执行 / 效果"), value: (action.execution.isEmpty ? L("尚未报告") : action.execution) + " / " + (action.effect.isEmpty ? L("尚未报告") : action.effect))
            LabeledContent(L("核验状态"), value: action.verification)
            ForEach(action.metadata.keys.sorted(), id: \.self) { key in
                LabeledContent(key, value: action.metadata[key] ?? "")
            }
        }.font(.caption).textSelection(.enabled)
    }
}

struct ExecutionDetailsView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Toggle(L("显示推理"), isOn: $model.preferences.showReasoning)
                Toggle(L("显示代码"), isOn: $model.preferences.showCode)
            }.toggleStyle(.checkbox).font(.caption)
                .onChange(of: model.preferences.showReasoning) { _, _ in model.preferences.save() }
                .onChange(of: model.preferences.showCode) { _, _ in model.preferences.save() }
            Text(L("仅控制界面显示，不改变模型请求或执行。")).font(.caption2).foregroundStyle(.secondary)
            if model.preferences.showReasoning {
                DisclosureGroup(L("推理内容")) {
                    if model.controls.reasoning.isEmpty {
                        Text(model.phase.active && !model.controls.reasoningComplete ? L("尚未收到推理内容；模型可能不提供。") : L("本次请求未提供可显示的推理内容。")).font(.caption).foregroundStyle(.secondary)
                    } else {
                        ScrollView { Text(model.controls.reasoning).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                            .frame(maxHeight: 180)
                    }
                    if model.controls.reasoningTruncated { Text(L("界面预览已达上限；完整内容请查看运行记录，不影响执行。")).font(.caption).foregroundStyle(.secondary) }
                }
            }
            if model.preferences.showCode {
                DisclosureGroup(L("执行代码")) {
                    if model.controls.code.isEmpty { Text(L("尚未收到执行代码。")).font(.caption).foregroundStyle(.secondary) }
                    ForEach(model.controls.code) { block in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(L("第 %@ 步 · 代码块 %@", String(block.step), String(block.index))).font(.caption).foregroundStyle(.secondary)
                            ScrollView([.horizontal, .vertical]) {
                                Text(block.text).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }.frame(maxHeight: 180)
                        }
                    }
                    if model.controls.codeTruncated { Text(L("界面预览已达上限；完整内容请查看运行记录，不影响执行。")).font(.caption).foregroundStyle(.secondary) }
                }
            }
            if !model.controls.recentActions.isEmpty {
                DisclosureGroup(L("最近工具动作（最多 64 条）")) {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(model.controls.recentActions.reversed()) { action in
                                Label(action.label + " · " + action.id, systemImage: action.symbol)
                                    .font(.caption).textSelection(.enabled)
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }.frame(maxHeight: 160)
                }
            }
            if !model.controls.recoveryActions.isEmpty {
                DisclosureGroup(L("恢复动作快照（只读）")) {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(model.controls.recoveryActions) { action in
                                DisclosureGroup(action.label + " · " + action.id) {
                                    ActionDiagnosticView(action: action)
                                }
                            }
                        }
                    }.frame(maxHeight: 180)
                }
            }
            DisclosureGroup(L("诊断")) {
                TimelineView(.periodic(from: .now, by: 1)) { timeline in
                    VStack(alignment: .leading, spacing: 5) {
                        LabeledContent(L("真实阶段"), value: model.controls.stage.isEmpty ? (model.executionPhase.isEmpty ? model.phase.rawValue : model.executionPhase) : model.controls.stage)
                        if !model.controls.stageText.isEmpty { Text(model.controls.stageText) }
                        if let start = model.controls.turnStartedAt {
                            LabeledContent(L("本轮耗时"), value: L("%@ 秒", String(Int(max(0, (model.controls.turnEndedAt ?? timeline.date).timeIntervalSince(start))))))
                        }
                        if let action = model.controls.recentActions.last {
                            ActionDiagnosticView(action: action)
                        }
                        if !model.controls.lastEnvelope.isEmpty {
                            DisclosureGroup(L("最后事件 envelope 元数据")) {
                                ForEach(model.controls.lastEnvelope.keys.sorted(), id: \.self) { key in
                                    LabeledContent(key, value: model.controls.lastEnvelope[key] ?? "")
                                }
                            }
                        }
                        LabeledContent(L("API 尝试"), value: model.controls.maxAttempts > 0 ? "\(model.controls.attempt) / \(model.controls.maxAttempts)" : L("尚未报告"))
                        if model.controls.timeoutSeconds > 0 { LabeledContent(L("请求超时"), value: L("%@ 秒", String(Int(model.controls.timeoutSeconds)))) }
                        LabeledContent(L("会话 ID"), value: model.controls.sessionID.isEmpty ? L("尚未报告") : model.controls.sessionID)
                        LabeledContent(L("所选模型"), value: (try? model.preferences.taskConfiguration()["model_name"]) as? String ?? L("尚未报告"))
                        LabeledContent(L("实际模型"), value: model.controls.model.isEmpty ? L("尚未报告") : model.controls.model)
                        LabeledContent(L("协议"), value: model.controls.wireProtocol.isEmpty ? L("尚未报告") : model.controls.wireProtocol)
                        LabeledContent("Reasoning effort", value: model.controls.reasoningEffort.isEmpty ? L("尚未报告") : model.controls.reasoningEffort)
                        if let name = model.controls.effectiveSkill { LabeledContent(L("实际 Skill"), value: name) }
                        if !model.controls.skillSource.isEmpty { LabeledContent(L("Skill 来源"), value: model.controls.skillSource) }
                        if !model.controls.skillDigest.isEmpty { LabeledContent(L("Skill 摘要"), value: model.controls.skillDigest) }
                        if model.controls.workerReset { Text(L("执行 worker 已重置")) }
                        if let before = model.controls.budgetBefore, let after = model.controls.budgetAfter {
                            LabeledContent(L("上下文预算"), value: before + " → " + after)
                        }
                    }.textSelection(.enabled).font(.caption).padding(.top, 6)
                }
            }
        }.font(.system(size: 12))
    }
}
