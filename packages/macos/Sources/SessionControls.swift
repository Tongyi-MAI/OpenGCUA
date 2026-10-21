import Foundation
import SwiftUI

struct SkillEntry: Identifiable, Equatable {
    var name: String
    var description: String
    var source: String
    var id: String { name }
}
struct QueuedInput: Identifiable, Equatable {
    var id: String
    var text: String
    var kind: String
}
struct SessionApproval: Identifiable, Equatable {
    var id: String
    var appID: String
    var windowID: String
    var text: String
    var scope: String
}
struct ExecutionCode: Identifiable, Equatable {
    var index: Int
    var step: Int
    var text: String
    var complete = false
    var id: Int { index }
}
struct PendingControl {
    var name: String
    var taskID: String
    var turn: Int
    var approvalID: String?
    var started = Date()
    // 压缩与 Skill 由 host 在轮次之间处理，回执可能晚于本轮；只按 command_id 与 task 匹配。
    var sessionScoped: Bool { ["compaction", "compaction_cancel", "skill_select"].contains(name) }
}
// 启动前的用户选择：unspecified 不下发，交给 host 恢复历史或使用默认值。
enum SkillOverride: Equatable {
    case unspecified, noSkill, named(String)
    var name: String? { if case let .named(name) = self { return name }; return nil }
}

// 只保存本次连接的状态；会话权限和审批绝不写入 Preferences。
struct SessionControlsState {
    var skills: [SkillEntry] = []
    var catalogAvailable = false
    var catalogStatus = ""
    var catalogSequence: Int?
    // selectedSkill 是期望值（界面所选），effectiveSkill 只随 host 的生效状态更新。
    var selectedSkill: String?
    var skillOverride = SkillOverride.unspecified
    var effectiveSkill: String?
    var skillSource = ""
    var skillDigest = ""
    var skillPending = false
    var sessionID = ""
    var model = ""
    var wireProtocol = ""
    var reasoningEffort = ""
    var foregroundAllowed = false
    var askUserEnabled = true
    var queue: [QueuedInput] = []
    var queueAvailable = false
    var queueSuspended = false
    var approvals: [SessionApproval] = []
    var closedApprovals: Set<String> = []
    var pending: [String: PendingControl] = [:]
    var settled: [String: PendingControl] = [:]
    var terminalCommands: Set<String> = []
    var messageStates: [String: String] = [:]
    var result = ""
    var budgetBefore: String?
    var budgetAfter: String?
    var interruptPending = false
    var interrupted = false
    var workerReset = false
    var compacting = false
    var compactionRunning = false
    var stage = ""
    var stageText = ""
    var attempt = 0
    var maxAttempts = 0
    var timeoutSeconds = 0.0
    var turnStartedAt: Date?
    var turnEndedAt: Date?
    static let previewCharacterLimit = 65_536
    static let previewBlockLimit = 32
    var intentPreview = ""
    var intentBlock = 0
    var intentStep: Int?
    var intentComplete = false
    var reasoning = ""
    var reasoningComplete = false
    var reasoningTruncated = false
    var code: [ExecutionCode] = []
    var codeTruncated = false
    var turnIndex = -1
    var sequence: Int?
    var attemptID: String?
    var retiredAttempts: Set<String> = []
    var turnClosed = false
    var hasTurnStarted = false
    var awaitingNextTurn = false

    // 仅裁剪界面缓存，不改变 host 的请求、代码或轨迹。
    static func preview(_ text: String, truncated: inout Bool) -> String {
        if text.count > previewCharacterLimit {
            truncated = true
            return String(text.prefix(previewCharacterLimit))
        }
        return text
    }
    mutating func resetExecutionPreview() {
        intentPreview = ""; intentBlock = 0; intentStep = nil; intentComplete = false
        reasoning = ""; reasoningComplete = false; reasoningTruncated = false
        code = []; codeTruncated = false
    }
    func control(for event: [String: Any]) -> PendingControl? {
        (event["command_id"] as? String).flatMap { pending[$0] ?? settled[$0] }
    }
    // 手动压缩在轮次之间运行：command_id/manual 可识别；轮次关闭时 purpose=compaction 也只能来自它。
    func sessionCompaction(_ event: [String: Any]) -> Bool {
        let kind = event["event"] as? String ?? ""
        guard kind == "progress" || kind == "model_status" else { return false }
        if control(for: event)?.name == "compaction" { return true }
        if kind == "progress" && event["phase"] as? String != "compaction" { return false }
        return event["manual"] as? Bool == true || (event["purpose"] as? String == "compaction" && (turnClosed || awaitingNextTurn))
    }
    // 压缩事件只更新压缩显示，不改变本轮 attempt、预览或会话阶段。
    func compactionEvent(_ event: [String: Any]) -> Bool {
        sessionCompaction(event) || (event["event"] as? String == "model_status" && event["purpose"] as? String == "compaction")
    }
    mutating func beginTurn(_ index: Int) {
        if index > turnIndex { sequence = nil }
        turnIndex = index; turnClosed = false; hasTurnStarted = false; awaitingNextTurn = false
        for (id, operation) in pending where !operation.taskID.isEmpty && !operation.sessionScoped && operation.turn < index {
            pending[id] = nil
        }
        if pending.isEmpty && L(result) == L("正在等待执行端确认…") { result = "" }
        attemptID = nil; retiredAttempts = []; resetExecutionPreview()
        stage = ""; stageText = ""; attempt = 0; maxAttempts = 0; timeoutSeconds = 0
        interrupted = false; interruptPending = false; workerReset = false; compacting = false; compactionRunning = false
        approvals = []
        turnStartedAt = Date(); turnEndedAt = nil
    }
    mutating func accept(_ event: [String: Any]) -> Bool {
        let kind = event["event"] as? String ?? ""
        let sessionEvents = ["skills_catalog", "skill_state", "session_info", "policy", "queue_state", "message", "control_result", "command_ack", "error", "approval_closed", "trajectory", "finished", "skill_loaded", "skill_warning"]
        let index = event["turn_index"] as? Int
        if let index, index < turnIndex {
            // 会话级命令可能比当前轮旧；匹配 pending 后交给 command_id 去重，不推进本轮 sequence。
            return kind == "control_result" && control(for: event)?.sessionScoped == true
        }
        if index == nil || index == turnIndex {
            if let sequence = event["sequence"] as? Int, let last = self.sequence, sequence <= last { return false }
        }
        if kind == "turn_started" {
            if let index {
                guard index > turnIndex || (index == turnIndex && !hasTurnStarted && !turnClosed) else { return false }
                if index > turnIndex { beginTurn(index) }
            } else { beginTurn(turnIndex + 1) }
            hasTurnStarted = true
        } else if let index, index > turnIndex {
            // 新轮可能先发送 session_info/progress；仍不接受旧轮的迟到事件。
            beginTurn(index)
        }
        let compaction = compactionEvent(event)
        if !compaction && kind != "turn_started" {
            if index != nil && (turnClosed || awaitingNextTurn), !sessionEvents.contains(kind), kind != "turn_finished" { return false }
            if interrupted && !awaitingNextTurn && !sessionEvents.contains(kind) { return false }
        }
        if kind == "turn_finished", index != nil && (turnClosed || awaitingNextTurn) { return false }
        // finished 是会话终态；task/sequence 已防旧，中止或等待下一轮时也必须接受。
        // 每步新请求（stage=waiting）从 attempt 1 重新计数，不能被上一步的重试次数拦截。
        let newRequest = !compaction && kind == "model_status" && event["stage"] as? String == "waiting"
        if !compaction && kind == "model_status" && !newRequest, let number = event["attempt"] as? Int, number < attempt { return false }
        if !compaction, let raw = event["attempt_id"], !(raw is NSNull) {
            let id = String(describing: raw)
            guard !retiredAttempts.contains(id) else { return false }
            if let current = attemptID, current != id {
                guard kind == "model_status", newRequest || (event["attempt"] as? Int ?? attempt) >= attempt else { return false }
                retiredAttempts.insert(current)
                resetExecutionPreview()
            }
            attemptID = id
        }
        if newRequest { resetExecutionPreview() }
        if let sequence = event["sequence"] as? Int { self.sequence = sequence }
        return true
    }
}

@MainActor extension AppModel {
    var liveSession: Bool { ready && !disconnected && conversationOpen && !taskID.isEmpty }
    var canSwitchSession: Bool {
        !phase.active && !sendingMessage && !controls.compacting && controls.pending.values.allSatisfy { ["skills_list", "skills_reload"].contains($0.name) }
    }
    var canInterrupt: Bool { liveSession && phase.active && phase != .stopping && !controls.interrupted }
    var canCompact: Bool { liveSession && phase != .stopping && !controls.compacting && !controls.interruptPending }
    func controlPending(_ name: String) -> Bool { controls.pending.values.contains { $0.name == name } }
    func approvalPending(_ id: String) -> Bool { controls.pending.values.contains { $0.approvalID == id } }

    func resetSessionControls(clearCatalog: Bool = false) {
        var next = SessionControlsState()
        if !clearCatalog {
            next.skills = controls.skills; next.catalogAvailable = controls.catalogAvailable
            next.catalogStatus = controls.catalogStatus; next.catalogSequence = controls.catalogSequence
            // Skill 选择与生效状态属于会话；跨会话沿用会绕过历史 Skill 的摘要校验。
            // 空闲目录请求属于连接而非会话；切换会话后仍等待同一回执与超时。
            next.pending = controls.pending.filter { $0.value.taskID.isEmpty }
            next.settled = controls.settled.filter { $0.value.taskID.isEmpty }
            next.terminalCommands = controls.terminalCommands.intersection(Set(next.settled.keys))
            if !next.pending.isEmpty { next.result = L("正在等待执行端确认…") }
            if next.pending.isEmpty && L(next.catalogStatus) == L("正在读取 Skill 目录…") { next.catalogStatus = "" }
        }
        controls = next
    }
    func prepareControlTurn() {
        controls.awaitingNextTurn = true
        controls.interrupted = false; controls.interruptPending = false
        controls.resetExecutionPreview(); controls.stage = ""; controls.stageText = ""
        controls.turnStartedAt = Date(); controls.turnEndedAt = nil
    }
    func completeControlTurn(keepingSession: Bool) {
        // 压缩是会话命令，host 在轮次结束后才执行；只有会话结束才放弃等待。
        removePendingControls(keepingSession ? ["interrupt", "approval_reply"] : ["interrupt", "approval_reply", "compaction", "compaction_cancel"])
        controls.turnClosed = true; controls.awaitingNextTurn = false; controls.turnEndedAt = Date()
        if !keepingSession { controls.compactionRunning = false }
        controls.interruptPending = false; controls.compacting = controls.compactionRunning; controls.approvals = []
        if !keepingSession {
            controls.foregroundAllowed = false; controls.askUserEnabled = true
            controls.queue = []; controls.queueSuspended = false; controls.queueAvailable = false
        }
    }
    @discardableResult func sendControl(_ name: String, fields: [String: Any] = [:], idle: Bool = false, approvalID: String? = nil) -> String? {
        guard ready, !disconnected, idle || liveSession else {
            controls.result = L("此操作需要已连接的实时会话。")
            return nil
        }
        guard !controlPending(name) || name == "approval_reply" else { controls.result = L("正在等待执行端确认…"); return nil }
        let id = UUID().uuidString
        var request = fields
        request["command"] = name; request["command_id"] = id
        let scopeTask = liveSession ? taskID : (idle ? "" : taskID)
        if !scopeTask.isEmpty { request["task_id"] = scopeTask }
        controls.pending[id] = PendingControl(name: name, taskID: scopeTask, turn: controls.turnIndex, approvalID: approvalID)
        controls.result = L("正在等待执行端确认…")
        do { try bridge.send(request) }
        catch {
            controls.pending[id] = nil; controls.result = error.localizedDescription
            self.error = error.localizedDescription
            return nil
        }
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 15_000_000_000)
            self?.expireControl(id)
        }
        return id
    }
    func expireControl(_ id: String) {
        guard let pending = controls.pending.removeValue(forKey: id) else { return }
        guard pending.taskID.isEmpty || (pending.taskID == taskID && (pending.turn == controls.turnIndex || pending.sessionScoped)) else { return }
        controls.result = L("未收到确认，状态未知；请刷新后核实，不要自动重发。")
        if pending.name == "interrupt" { controls.interruptPending = false }
        if pending.name == "skill_select" {
            controls.skillPending = false; controls.selectedSkill = controls.effectiveSkill
        }
        if pending.name == "skills_list" || pending.name == "skills_reload" { controls.catalogStatus = controls.result }
        delegate?.refreshCompact()
    }
    func refreshSkills(reload: Bool = false) {
        controls.catalogStatus = L("正在读取 Skill 目录…")
        if sendControl(reload ? "skills_reload" : "skills_list", idle: true) == nil {
            controls.catalogStatus = controls.result
        }
    }
    func selectSkill(_ name: String?) {
        guard name == nil || (controls.catalogAvailable && controls.skills.contains { $0.name == name }) else {
            controls.catalogStatus = L("Skill 目录尚不可用，请刷新。")
            return
        }
        if !conversationOpen {
            // 未启动的新会话或历史会话：只记录显式覆盖，start 时再下发。
            let choice = name.map { SkillOverride.named($0) } ?? .noSkill
            guard controls.skillOverride != choice else { return }
            controls.skillOverride = choice; controls.selectedSkill = name; controls.skillPending = false
            controls.catalogStatus = L("将在新会话启动时使用所选 Skill。")
            return
        }
        guard controls.selectedSkill != name else { return }
        if sendControl("skill_select", fields: ["name": name.map { $0 as Any } ?? NSNull()]) != nil {
            controls.selectedSkill = name; controls.skillPending = phase.active
        }
    }
    func setForegroundAllowed(_ value: Bool) {
        _ = sendControl("set_policy", fields: ["foreground_allowed": value])
    }
    func setAskUserEnabled(_ value: Bool) {
        if !conversationOpen { controls.askUserEnabled = value; return }
        _ = sendControl("set_policy", fields: ["ask_user_enabled": value])
    }
    func interruptTurn(force: Bool = false) {
        guard canInterrupt else { controls.result = L("当前没有可中止的轮次。"); return }
        if controls.interruptPending && !force { return }
        if force {
            // 强制请求可以替代尚未确认的普通中止，但旧回执不能清除新 pending。
            for (id, pending) in controls.pending where pending.name == "interrupt" { controls.pending[id] = nil }
        }
        if sendControl("interrupt", fields: ["force": force]) != nil { controls.interruptPending = true }
        delegate?.refreshCompact()
    }
    func refreshQueue() { _ = sendControl("queue_list") }
    func resumeQueue() { _ = sendControl("queue_resume") }
    func clearQueue() { _ = sendControl("queue_clear") }
    func compactContext() { if canCompact { _ = sendControl("compaction") } }
    func cancelCompaction() { _ = sendControl("compaction_cancel") }
    // 取消已确认但压缩回执已丢失时，结束本地 running，避免会话切换被锁死。
    private func settleCompactionCancel() {
        if !controlPending("compaction") { controls.compactionRunning = false; controls.compacting = false }
    }
    func replyToApproval(_ id: String, choice: String = "reject") {
        guard ["allow", "reject", "cancel"].contains(choice),
              controls.approvals.contains(where: { $0.id == id }), !approvalPending(id) else { return }
        _ = sendControl("approval_reply", fields: ["approval_id": id, "choice": choice], approvalID: id)
    }
    func removePendingControls(_ names: [String], approvalID: String? = nil) {
        for (id, pending) in controls.pending where names.contains(pending.name) && (approvalID == nil || pending.approvalID == approvalID) {
            controls.settled[id] = pending
            controls.pending[id] = nil
        }
        if controls.pending.isEmpty && L(controls.result) == L("正在等待执行端确认…") { controls.result = "" }
        if controls.settled.count > 64 { controls.settled = [:]; controls.terminalCommands = [] }
    }
    func receiveCatalog(_ event: [String: Any]) {
        if event["task_id"] == nil, let sequence = event["sequence"] as? Int {
            if let last = controls.catalogSequence, sequence <= last { return }
            controls.catalogSequence = sequence
        }
        var names: Set<String> = []
        controls.skills = (event["skills"] as? [[String: Any]] ?? []).compactMap { item in
            guard let name = item["name"] as? String, !name.isEmpty, names.insert(name).inserted else { return nil }
            return SkillEntry(name: name, description: item["description"] as? String ?? "", source: item["source"] as? String ?? "")
        }
        controls.catalogAvailable = true
        // 无会话时保留用户本地选择；host 的目录默认项不能覆盖它。
        if conversationOpen && event["task_id"] != nil && !controlPending("skill_select") && !controls.skillPending {
            controls.selectedSkill = event["selected_name"] as? String
        } else if !conversationOpen, let selected = controls.skillOverride.name, !names.contains(selected) {
            controls.skillOverride = .unspecified; controls.selectedSkill = nil
        }
        controls.catalogStatus = controls.skills.isEmpty ? L("没有可用 Skill；可选择不选。") : ""
        removePendingControls(["skills_list", "skills_reload"])
    }
    @discardableResult func receiveControlEvent(_ event: [String: Any]) -> Bool {
        let kind = event["event"] as? String ?? ""
        switch kind {
        case "skills_catalog": receiveCatalog(event)
        case "skill_state":
            let name = event["name"] as? String
            let command = controls.control(for: event)
            if event["pending"] as? Bool ?? false {
                // 待生效只更新期望值；skill_select 仍等待 applied 或终态回执。
                controls.selectedSkill = name; controls.skillPending = true
                return true
            }
            controls.effectiveSkill = name
            controls.skillSource = event["source"] as? String ?? ""
            controls.skillDigest = event["digest"] as? String ?? ""
            if command?.name == "skill_select", let id = event["command_id"] as? String {
                controls.selectedSkill = name; controls.skillPending = false
                if controls.pending[id] != nil { controls.settled[id] = controls.pending.removeValue(forKey: id) }
                if controls.pending.isEmpty && L(controls.result) == L("正在等待执行端确认…") { controls.result = "" }
            } else if !controls.skillPending && !controlPending("skill_select") {
                // 模型本轮自行加载只同步生效值；有待生效选择时不覆盖用户期望。
                controls.selectedSkill = name
            }
        case "session_info":
            controls.sessionID = event["session_id"] as? String ?? ""
            controls.model = event["model"] as? String ?? ""
            controls.wireProtocol = event["protocol"].map { String(describing: $0) } ?? ""
            controls.reasoningEffort = event["reasoning_effort"] as? String ?? ""
            controls.foregroundAllowed = event["foreground_allowed"] as? Bool ?? false
            controls.askUserEnabled = event["ask_user_enabled"] as? Bool ?? true
            removePendingControls(["set_policy"])
            if !controlPending("queue_list") { refreshQueue() }
        case "policy":
            controls.foregroundAllowed = event["foreground_allowed"] as? Bool ?? false
            controls.askUserEnabled = event["ask_user_enabled"] as? Bool ?? true
            removePendingControls(["set_policy"])
        case "queue_state":
            var ids: Set<String> = []
            controls.queue = (event["items"] as? [[String: Any]] ?? []).compactMap { item in
                guard let id = item["id"] as? String, !id.isEmpty, ids.insert(id).inserted else { return nil }
                return QueuedInput(id: id, text: item["text"] as? String ?? "", kind: item["kind"] as? String ?? "")
            }
            controls.queueAvailable = true; controls.queueSuspended = event["suspended"] as? Bool ?? false
            removePendingControls(["queue_list", "queue_resume", "queue_clear"])
        case "approval":
            guard let id = event["approval_id"] as? String, !controls.closedApprovals.contains(id) else { return true }
            let approval = SessionApproval(id: id, appID: String(describing: event["app_id"] ?? ""),
                                           windowID: String(describing: event["window_id"] ?? ""),
                                           text: event["text"] as? String ?? "", scope: event["scope"] as? String ?? "session")
            if !controls.approvals.contains(where: { $0.id == id }) { controls.approvals.append(approval) }
        case "approval_closed":
            guard let id = event["approval_id"] as? String else { return true }
            controls.closedApprovals.insert(id); controls.approvals.removeAll { $0.id == id }
            removePendingControls(["approval_reply"], approvalID: id)
            // allow 的真实 policy 由 host 发出；关闭卡片不授予任何本地权限。
        case "interrupted":
            controls.interrupted = true; controls.interruptPending = false
            controls.workerReset = event["worker_reset"] as? Bool ?? false
            sendingMessage = false
            controls.stage = "interrupted"; controls.stageText = event["text"] as? String ?? L("本轮已中止，可在同一会话发送纠正。")
            controls.turnClosed = true; controls.turnEndedAt = Date()
            // 中止只结束本轮；已排队的会话压缩仍等待自己的 control_result。
            controls.approvals = []; controls.compacting = controls.compactionRunning
            removePendingControls(["interrupt"])
            finish(.stopped, text: controls.stageText, keepingConversation: true)
        case "model_status":
            controls.stage = event["stage"] as? String ?? ""
            controls.stageText = event["text"] as? String ?? ""
            if controls.compactionEvent(event) {
                if controls.sessionCompaction(event) { controls.compactionRunning = true; controls.compacting = true }
                return true
            }
            controls.attempt = event["attempt"] as? Int ?? 0; controls.maxAttempts = event["max_attempts"] as? Int ?? 0
            controls.timeoutSeconds = (event["timeout_seconds"] as? NSNumber)?.doubleValue ?? 0
            if controls.stage == "waiting" || controls.stage == "retrying" || controls.stage == "cancelled" || controls.stage == "error" {
                intent = ""
            }
        case "intent_delta":
            let block = event["block_index"] as? Int ?? controls.intentBlock
            let sourceStep = event["step"] as? Int
            if event["start"] as? Bool == true || block != controls.intentBlock || sourceStep != controls.intentStep {
                controls.intentPreview = ""; controls.intentComplete = false
                controls.intentBlock = block; controls.intentStep = sourceStep
            }
            guard !controls.intentComplete else { return true }
            controls.intentPreview = String((controls.intentPreview + (event["text"] as? String ?? "")).prefix(4096))
        case "intent_end": controls.intentComplete = true
        case "reasoning_delta":
            guard !controls.reasoningComplete else { return true }
            if event["start"] as? Bool == true { controls.reasoning = ""; controls.reasoningTruncated = false }
            let text = controls.reasoning + (event["text"] as? String ?? "")
            controls.reasoning = SessionControlsState.preview(text, truncated: &controls.reasoningTruncated)
        case "reasoning_end": controls.reasoningComplete = true
        case "code_reset": controls.resetExecutionPreview(); intent = ""
        case "code_start", "code_delta", "code_end":
            let index = event["block_index"] as? Int ?? 0
            let text = event["text"] as? String ?? ""
            if kind == "code_start" {
                if controls.code.contains(where: { $0.index == index && $0.complete }) { return true }
                controls.code.removeAll { $0.index == index }
                guard controls.code.count < SessionControlsState.previewBlockLimit else { controls.codeTruncated = true; return true }
                let preview = SessionControlsState.preview(text, truncated: &controls.codeTruncated)
                controls.code.append(ExecutionCode(index: index, step: event["step"] as? Int ?? step, text: preview))
            } else if let position = controls.code.firstIndex(where: { $0.index == index }), !controls.code[position].complete {
                if kind == "code_end" {
                    if !text.isEmpty { controls.code[position].text = SessionControlsState.preview(text, truncated: &controls.codeTruncated) }
                    controls.code[position].complete = true
                } else {
                    let combined = controls.code[position].text + text
                    controls.code[position].text = SessionControlsState.preview(combined, truncated: &controls.codeTruncated)
                }
            }
        case "control_result":
            guard let id = event["command_id"] as? String, !controls.terminalCommands.contains(id),
                  let pending = controls.pending[id] ?? controls.settled[id] else { return true }
            let matchesScope = pending.taskID.isEmpty ? event["task_id"] == nil : (pending.taskID == taskID && event["task_id"] as? String == taskID)
            guard matchesScope, pending.turn == controls.turnIndex || pending.taskID.isEmpty || pending.sessionScoped else { return true }
            let status = event["status"] as? String ?? ""
            let inProgress = ["pending", "started", "accepted", "running"].contains(status)
            // 中间回执不能解除操作等待或丢掉超时；状态事件先到时也不能重新打开旧 pending。
            if !inProgress {
                controls.pending[id] = nil; controls.settled[id] = pending
                controls.terminalCommands.insert(id)
            }
            controls.result = (event["text"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? L("控制结果：%@", status)
            if let before = event["budget_before"] { controls.budgetBefore = String(describing: before) }
            if let after = event["budget_after"] { controls.budgetAfter = String(describing: after) }
            if ["error", "failed", "rejected", "cancelled"].contains(status) {
                if pending.name == "interrupt" { controls.interruptPending = false }
                if pending.name == "compaction" { controls.compactionRunning = false; controls.compacting = false }
                if pending.name == "skills_list" || pending.name == "skills_reload" { controls.catalogStatus = controls.result }
                if pending.name == "skill_select" { controls.skillPending = false; controls.selectedSkill = controls.effectiveSkill }
            } else if pending.name == "compaction", controls.pending[id] != nil || !inProgress {
                controls.compactionRunning = inProgress; controls.compacting = inProgress
            } else if pending.name == "compaction_cancel", !inProgress { settleCompactionCancel() }
            else if pending.name == "skill_select", !inProgress {
                // 终态回执后期望值收敛到 host 报告的生效值。
                controls.skillPending = false; controls.selectedSkill = controls.effectiveSkill
            }
            if pending.name == "interrupt", controls.terminalCommands.contains(id) { controls.interruptPending = false }
        default: return false
        }
        return true
    }
    func receiveControlAcknowledgement(_ event: [String: Any]) -> Bool {
        guard let id = event["command_id"] as? String, let pending = controls.pending[id] else { return false }
        if let task = event["task_id"] as? String, task != pending.taskID { return true }
        guard pending.taskID.isEmpty || pending.taskID == taskID, pending.turn == controls.turnIndex || pending.taskID.isEmpty || pending.sessionScoped else { return true }
        if event["event"] as? String == "error" {
            controls.pending[id] = nil; controls.result = event["text"] as? String ?? L("执行端拒绝")
            if pending.name == "interrupt" { controls.interruptPending = false }
            if pending.name == "compaction_cancel" { settleCompactionCancel() }
            if pending.name == "skill_select" { controls.skillPending = false; controls.selectedSkill = controls.effectiveSkill }
            if pending.name == "skills_list" || pending.name == "skills_reload" { controls.catalogStatus = controls.result }
        }
        // command_ack 只表示接收；等待状态事件/control_result 或超时，不能虚构成功。
        return true
    }
}
