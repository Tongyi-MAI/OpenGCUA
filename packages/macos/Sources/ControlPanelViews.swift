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

struct SessionControlPanel: View {
    @ObservedObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SkillPicker(model: model)
            SessionPolicyView(model: model)
            QueueControlsView(model: model)
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
            DisclosureGroup(L("诊断")) {
                TimelineView(.periodic(from: .now, by: 1)) { timeline in
                    VStack(alignment: .leading, spacing: 5) {
                        LabeledContent(L("真实阶段"), value: model.controls.stage.isEmpty ? (model.executionPhase.isEmpty ? model.phase.rawValue : model.executionPhase) : model.controls.stage)
                        if !model.controls.stageText.isEmpty { Text(model.controls.stageText) }
                        if let start = model.controls.turnStartedAt {
                            LabeledContent(L("本轮耗时"), value: L("%@ 秒", String(Int(max(0, (model.controls.turnEndedAt ?? timeline.date).timeIntervalSince(start))))))
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
