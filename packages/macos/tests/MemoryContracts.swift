import AppKit
import SwiftUI

// Fixture transport exercises the real AppModel memory routing without keys, network, storage, or desktop actions.
@MainActor final class RuntimeBridge {
    var onEvent: (([String: Any]) -> Void)?
    var onExit: (() -> Void)?
    var running = true
    var commands: [[String: Any]] = []
    var acknowledge = true
    var failSend = false
    func launch(dataDirectory: URL) throws {}
    func shutdown() {}
    func send(_ command: [String: Any]) throws {
        if failSend { throw AppError.message("Fixture transport unavailable") }
        commands.append(command)
        guard acknowledge, let id = command["command_id"] else { return }
        // 与 native_host 一致：ack 只表示受理，带回命令自己的 task_id。
        var event: [String: Any] = ["event": "command_ack", "command_id": id, "command": command["command"] ?? ""]
        if let task = command["task_id"] { event["task_id"] = task }
        Task { @MainActor in onEvent?(event) }
    }
}
@MainActor final class AppDelegate {
    func showSettings() {}
    func collapse(automatically: Bool = false) {}
    func expand() {}
    func languageChanged() {}
    func overlayWouldObstruct(_ event: [String: Any]) -> Bool { false }
    func refreshCompact() {}
    func runtimeExited() {}
    func showMain() {}
    func closeCompact() {}
    func showHistory() {}
    func showProgressBubble() {}
    func hideProgressBubble() {}
    func refreshMainSize() {}
}
@MainActor enum MacPermissions {
    static let accessibility = true, screenRecording = true
    static func showGuide(model: AppModel) {}
}
enum AppError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case let .message(text) = self { return text }; return nil }
}

@main enum MemoryContracts {
    static func item(_ id: String, _ status: String = "candidate", revision: Int = 1, content: String = "回复使用简体中文",
                     app: Any = NSNull(), origin: String = "model") -> [String: Any] {
        ["id": id, "title": "标题 " + id, "content": content, "kind": "preference", "status": status, "revision": revision, "app": app,
         "source": ["session_id": "session-m", "turn": 0, "step": 2, "origin": origin] as [String: Any],
         "validation": status == "approved" ? "user_confirmed" : "pending_user_review",
         "created_at": 1_790_000_000_000, "updated_at": 1_790_000_000_500]
    }
    static func catalog(_ entries: [[String: Any]], generation: Int, source: Any = NSNull(), omitted: Int = 0) -> [String: Any] {
        let candidates = entries.filter { $0["status"] as? String == "candidate" }.count
        return ["entries": entries, "generation": generation, "candidate_count": candidates + omitted,
                "approved_count": entries.count - candidates, "omitted": omitted, "root": "/private/memory",
                "policy": "candidate_review", "available": true, "error_code": NSNull(), "source_command_id": source]
    }
    // 连接级帧：native_host.memory_emit 的 legacy 形状，task 为 null，没有 schema_version。
    static func legacy(_ kind: String, _ fields: [String: Any]) -> [String: Any] {
        var event: [String: Any] = ["event": kind, "kind": kind, "task_id": NSNull()]
        for (key, value) in fields { event[key] = value }
        return event
    }
    @MainActor static func memoryCommands(_ model: AppModel) -> [[String: Any]] {
        model.bridge.commands.filter { ($0["command"] as? String)?.hasPrefix("memory_") == true }
    }
    @MainActor static func snapshot(_ model: AppModel) -> String {
        let m = model.controls.memory
        return "\(m.generation ?? -1)|" + m.entries.map { "\($0.id)@\($0.revision):\($0.status)" }.joined(separator: ",")
            + "|\(m.candidateCount)/\(m.approvedCount)/\(m.omitted)|\(m.available)|\(m.pending.count)/\(m.awaitingCatalog.count)"
    }
    static func drain() async { for _ in 0..<8 { await Task.yield() } }

    @MainActor static func idleContracts(directory: URL) async {
        let model = AppModel(dataDirectory: directory)
        model.receive(["event": "ready", "protocol": 1])
        precondition(model.ready && memoryCommands(model).isEmpty, "ready 不得在后台读取记忆")
        // ready 既有的 Skill 目录读取属于会话控制；记忆命令不得改变它的 pending 与 result。
        let sessionPending = Set(model.controls.pending.keys), sessionResult = model.controls.result
        model.refreshMemory()
        let list = model.bridge.commands.last!
        precondition(list["command"] as? String == "memory_list" && list["task_id"] == nil && list.count == 2, "空闲读取是连接级命令")
        let listID = list["command_id"] as! String
        precondition(model.controls.memory.status == L("正在读取记忆目录…"))
        model.refreshMemory()
        precondition(memoryCommands(model).count == 1, "读取进行中不重复发送")
        await drain()
        precondition(model.controls.memory.pending[listID] != nil && !model.controls.memory.received && model.error.isEmpty, "ack 只表示受理")
        model.receive(legacy("memory_catalog", catalog([item("c1"), item("a1", "approved", revision: 2)], generation: 3, source: listID)))
        precondition(snapshot(model) == "3|c1@1:candidate,a1@2:approved|1/1/0|true|0/0", snapshot(model))
        precondition(model.controls.memory.status.isEmpty && model.controls.memory.root == "/private/memory")
        model.receive(legacy("memory_catalog", catalog([], generation: 4, source: listID)))
        precondition(snapshot(model).hasPrefix("3|c1@1"), "已完成命令的目录按过期丢弃")
        model.receive(legacy("memory_catalog", catalog([], generation: 4, source: "unknown-command")))
        precondition(snapshot(model).hasPrefix("3|c1@1"), "未知 source_command_id 按过期丢弃")
        model.receive(legacy("memory_catalog", catalog([], generation: 2)))
        precondition(snapshot(model).hasPrefix("3|c1@1"), "旧 generation 不能覆盖目录")

        // 畸形目录整帧不提交。
        let valid = catalog([item("bad")], generation: 50)
        var variants: [(String, [String: Any])] = []
        func variant(_ name: String, _ change: (inout [String: Any]) -> Void) { var value = valid; change(&value); variants.append((name, value)) }
        func entry(_ name: String, _ change: @escaping (inout [String: Any]) -> Void) {
            variant(name) { value in var bad = item("bad"); change(&bad); value["entries"] = [bad] }
        }
        entry("content>2000") { $0["content"] = String(repeating: "长", count: 2001) }
        entry("title>120") { $0["title"] = String(repeating: "t", count: 121) }
        entry("kind") { $0["kind"] = "credential" }
        entry("status") { $0["status"] = "verified" }
        entry("revision0") { $0["revision"] = 0 }
        entry("revisionBool") { $0["revision"] = true }
        entry("revisionFloat") { $0["revision"] = 1.0 }
        entry("emptyID") { $0["id"] = "" }
        entry("missingApp") { $0["app"] = nil }
        entry("appNumber") { $0["app"] = 3 }
        entry("emptyOrigin") { $0["source"] = ["session_id": NSNull(), "turn": NSNull(), "step": NSNull(), "origin": ""] as [String: Any] }
        entry("missingTurn") { $0["source"] = ["session_id": NSNull(), "step": NSNull(), "origin": "model"] as [String: Any] }
        entry("missingValidation") { $0["validation"] = nil }
        entry("createdFloat") { $0["created_at"] = 1.5 }
        variant("duplicateID") { $0["entries"] = [item("dup"), item("dup")]; $0["candidate_count"] = 2 }
        variant("65 entries") { $0["entries"] = (0..<65).map { item("e\($0)") }; $0["candidate_count"] = 65 }
        variant("entriesType") { $0["entries"] = "[]" }
        variant("policy") { $0["policy"] = "auto_approve" }
        variant("generation") { $0["generation"] = -1 }
        variant("generationFloat") { $0["generation"] = 51.0 }
        variant("omitted") { $0["omitted"] = 3 }
        variant("countLow") { $0["candidate_count"] = 0 }
        variant("countOverflow") { $0["candidate_count"] = Int.max; $0["approved_count"] = Int.max }
        variant("availableText") { $0["available"] = "true" }
        variant("availableInt") { $0["available"] = 1 }
        variant("missingRoot") { $0["root"] = nil }
        variant("errorCode") { $0["error_code"] = 7 }
        variant("emptySource") { $0["source_command_id"] = "" }
        variant("missingSource") { $0["source_command_id"] = nil }
        let before = snapshot(model)
        for (name, value) in variants {
            model.receive(legacy("memory_catalog", value))
            precondition(snapshot(model) == before && model.error.isEmpty, "畸形目录不得改变状态：" + name)
        }
        // 64 条上限与 omitted：显示有界列表，计数仍取 host 值。
        model.receive(legacy("memory_catalog", catalog((0..<64).map { item("bulk-\($0)") }, generation: 5, omitted: 6)))
        precondition(model.controls.memory.entries.count == 64 && model.controls.memory.candidateCount == 70 && model.controls.memory.omitted == 6)
        precondition(model.controls.memory.offset == 0 && model.controls.memory.nextOffset == nil, "旧目录无分页字段按首页、无下一页兼容")
        // 模型提案：source_command_id 为 null，只成为候选，不提示、不自动批准。
        let sends = model.bridge.commands.count
        model.receive(legacy("memory_catalog", catalog([item("c1"), item("a1", "approved", revision: 2), item("p1")], generation: 6)))
        precondition(snapshot(model) == "6|c1@1:candidate,a1@2:approved,p1@1:candidate|2/1/0|true|0/0", snapshot(model))
        precondition(model.bridge.commands.count == sends && model.error.isEmpty && model.phase == .idle, "提案不得自动批准或派发")

        // 批准：ack 与 control_result 都不乐观改变 status；目录是唯一权威。
        let c1 = model.controls.memory.entries.first { $0.id == "c1" }!
        model.approveMemory(c1)
        let approve = model.bridge.commands.last!
        let approveID = approve["command_id"] as! String
        precondition(approve["command"] as? String == "memory_approve" && approve["memory_id"] as? String == "c1"
                     && approve["revision"] as? Int == 1 && approve["task_id"] == nil && approve.count == 4)
        precondition(model.controls.memory.busy && model.controls.memory.status == L("正在等待执行端确认…"))
        await drain()
        precondition(model.controls.memory.entries.first { $0.id == "c1" }?.status == "candidate" && model.controls.memory.pending[approveID] != nil,
                     "mutation ack 不是终态")
        let blocked = model.bridge.commands.count
        model.forgetMemory(model.controls.memory.entries.first { $0.id == "a1" }!)
        precondition(model.bridge.commands.count == blocked && model.controls.memory.status == L("上一条记忆操作仍在等待执行端确认。"))
        model.receive(legacy("control_result", ["command_id": approveID, "status": "completed", "memory_id": "c1", "revision": 2,
                                                "generation": 7, "error_code": NSNull(), "text": "记忆已更新"]))
        precondition(model.controls.memory.entries.first { $0.id == "c1" }?.status == "candidate", "control_result 不直接改条目")
        precondition(model.controls.memory.busy && model.controls.memory.awaitingCatalog[approveID]?.generation == 7, "写等待持续到目录回写")
        precondition(model.controls.memory.status == L("记忆已确认；用户确认不代表业务结果已核验。") && model.controls.result == sessionResult)
        model.receive(legacy("control_result", ["command_id": approveID, "status": "failed", "error_code": "MEMORY_NOT_FOUND", "text": "x"]))
        precondition(model.controls.memory.status == L("记忆已确认；用户确认不代表业务结果已核验。"), "重复 control_result 只吞掉")
        model.forgetMemory(model.controls.memory.entries.first { $0.id == "a1" }!)
        precondition(model.bridge.commands.count == blocked, "等待目录期间不得用旧 revision 继续变更")
        // 更老 generation 的同源目录不得结算已成功的变更。
        model.receive(legacy("memory_catalog", catalog([item("c1"), item("a1", "approved", revision: 2), item("p1")], generation: 6, source: approveID)))
        precondition(model.controls.memory.awaitingCatalog[approveID] != nil && model.controls.memory.busy)
        model.receive(legacy("memory_catalog", catalog([item("c1", "approved", revision: 2), item("a1", "approved", revision: 2), item("p1")], generation: 7, source: approveID)))
        precondition(snapshot(model) == "7|c1@2:approved,a1@2:approved,p1@1:candidate|1/2/0|true|0/0", snapshot(model))
        precondition(Set(model.controls.pending.keys) == sessionPending && model.controls.result == sessionResult
                     && model.controls.settled[listID] == nil && model.controls.settled[approveID] == nil, "记忆命令不进入会话控制 pending/result")

        // 旧副本不能被批准或删除：要求重新阅读当前版本。
        let count = model.bridge.commands.count
        model.forgetMemory(c1)
        precondition(model.bridge.commands.count == count && model.controls.memory.status == L("记忆已在执行端变化，请重新阅读当前内容后再操作。"))
        model.approveMemory(model.controls.memory.entries.first { $0.id == "a1" }!)
        precondition(model.bridge.commands.count == count, "已批准条目不能再次批准")

        // 遗忘失败：失败回执附当前快照（同一 generation），条目保留。
        model.forgetMemory(model.controls.memory.entries.first { $0.id == "a1" }!)
        let forgetID = model.bridge.commands.last!["command_id"] as! String
        precondition(model.bridge.commands.last!["command"] as? String == "memory_forget")
        model.receive(legacy("control_result", ["command_id": forgetID, "status": "failed", "memory_id": "a1", "revision": 2,
                                                "generation": 7, "error_code": "MEMORY_REVISION_CONFLICT", "text": "记忆已被修改，请刷新后重试"]))
        precondition(model.controls.memory.status == L("记忆操作失败：%@ 请刷新后核实。", MemoryControlsState.errorText("MEMORY_REVISION_CONFLICT")))
        model.receive(legacy("memory_catalog", catalog([item("c1", "approved", revision: 2), item("a1", "approved", revision: 2), item("p1")], generation: 7, source: forgetID)))
        precondition(snapshot(model) == "7|c1@2:approved,a1@2:approved,p1@1:candidate|1/2/0|true|0/0", snapshot(model))

        // 编辑：只发送改动字段；保存后由 host 退回候选，需要重新确认。
        let approved = model.controls.memory.entries.first { $0.id == "c1" }!
        var sent = model.bridge.commands.count
        model.updateMemory(approved, title: approved.title, content: approved.content, kind: approved.kind, app: "")
        precondition(model.bridge.commands.count == sent && model.controls.memory.status == L("内容没有变化，未发送。"))
        model.updateMemory(approved, title: "  ", content: "x", kind: "fact", app: "")
        precondition(model.bridge.commands.count == sent, "空标题不发送")
        model.updateMemory(approved, title: approved.title, content: String(repeating: "长", count: 2001), kind: approved.kind, app: "")
        precondition(model.bridge.commands.count == sent, "超长正文不发送")
        model.updateMemory(approved, title: approved.title, content: " 回复使用英文 ", kind: "workflow", app: "Safari")
        let update = model.bridge.commands.last!
        let updateID = update["command_id"] as! String
        precondition(update["command"] as? String == "memory_update" && update["content"] as? String == "回复使用英文"
                     && update["kind"] as? String == "workflow" && update["app"] as? String == "Safari" && update["title"] == nil
                     && update["revision"] as? Int == 2 && update["task_id"] == nil)
        model.receive(legacy("control_result", ["command_id": updateID, "status": "completed", "memory_id": "c1", "revision": 3,
                                                "generation": 8, "error_code": NSNull(), "text": "记忆已更新"]))
        precondition(model.controls.memory.status == L("已保存为候选，需要重新确认。"))
        sent = model.bridge.commands.count
        model.receive(legacy("memory_catalog", catalog([item("c1", revision: 3, content: "回复使用英文", app: "Safari", origin: "user_edit"),
                                                         item("a1", "approved", revision: 2), item("p1")], generation: 8, source: updateID)))
        let edited = model.controls.memory.entries.first { $0.id == "c1" }!
        precondition(edited.isCandidate && edited.source.origin == "user_edit" && edited.validation == "pending_user_review" && edited.app == "Safari")
        precondition(model.bridge.commands.count == sent, "编辑后的候选不自动批准")
        let appCleared = model.controls.memory.entries.first { $0.id == "c1" }!
        model.updateMemory(appCleared, title: appCleared.title, content: appCleared.content, kind: appCleared.kind, app: "")
        precondition(model.bridge.commands.last!["app"] is NSNull && model.bridge.commands.last!.count == 5, "清空 app 发送 null")
        let clearID = model.bridge.commands.last!["command_id"] as! String

        // 字段无效或过期 task：host 只回 error，不发 control_result；不污染 model.error/sendingMessage。
        model.sendingMessage = true
        model.receive(["event": "error", "text": "memory_update 字段无效", "command_id": clearID])
        precondition(model.controls.memory.pending.isEmpty && model.error.isEmpty && model.sendingMessage)
        let rejected = model.controls.memory.status
        precondition(rejected == L("执行端拒绝记忆命令：%@ 状态未改变，请刷新后核实。", "memory_update 字段无效"))
        model.receive(legacy("control_result", ["command_id": clearID, "status": "completed", "error_code": NSNull(), "text": "记忆已更新"]))
        precondition(model.controls.memory.awaitingCatalog.isEmpty && model.controls.memory.status == rejected, "error 后迟到的 control_result 不得改写状态")
        model.sendingMessage = false

        // 超时：显示未确认，不重发；迟到的结果与目录按过期丢弃。
        model.rejectMemory(model.controls.memory.entries.first { $0.id == "p1" }!)
        let rejectID = model.bridge.commands.last!["command_id"] as! String
        model.expireMemoryControl(rejectID)
        sent = model.bridge.commands.count
        precondition(model.controls.memory.status == L("未收到记忆操作确认，状态未确认；请刷新后核实，不要自动重发。") && !model.controls.memory.busy)
        model.receive(legacy("control_result", ["command_id": rejectID, "status": "completed", "error_code": NSNull(), "text": "记忆已更新"]))
        model.receive(legacy("memory_catalog", catalog([item("a1", "approved", revision: 2)], generation: 9, source: rejectID)))
        precondition(snapshot(model).hasPrefix("8|c1@3:candidate,a1@2:approved,p1@1:candidate"), "超时后迟到目录不覆盖")
        precondition(model.controls.memory.status == L("未收到记忆操作确认，状态未确认；请刷新后核实，不要自动重发。") && model.bridge.commands.count == sent)

        // 发送失败：不写 pending、不设全局 error，状态未确认。
        model.bridge.failSend = true
        model.refreshMemory()
        precondition(model.controls.memory.pending.isEmpty && model.error.isEmpty && model.controls.memory.status == L("记忆命令未送达：%@ 状态未确认，请刷新后核实。", "Fixture transport unavailable"))
        model.bridge.failSend = false
        print("记忆空闲契约通过：ready 不读取、连接级 list、ack 非终态、目录权威、generation/command_id 防旧、\(variants.count) 个畸形目录、64 条上限、提案不自动批准、批准/遗忘/编辑/拒绝/超时/发送失败。")
    }

    @MainActor static func liveContracts(directory: URL) async {
        let model = AppModel(dataDirectory: directory)
        model.ready = true
        model.taskID = "task-live"; model.conversationOpen = true; model.phase = .running
        var sequence = 0
        var turnID = "turn-0", turnIndex = 0
        func v1(_ kind: String, _ fields: [String: Any] = [:]) -> [String: Any] {
            sequence += 1
            // TaskControl.emit 形状：v1 信封 + native_task_id；不带 legacy event 字段。
            var event: [String: Any] = ["schema_version": 1, "event_id": "stream-m:\(sequence)", "sequence": sequence,
                "session_id": "session-m", "stream_id": "stream-m", "task_id": "harness-task", "native_task_id": model.taskID,
                "turn_id": turnID, "turn_index": turnIndex, "step_id": NSNull(), "step": NSNull(), "timestamp": "2026-10-21T00:00:00Z",
                "kind": kind, "origin": "native_host", "policy_revision": 3, "text": ""]
            for (key, value) in fields { event[key] = value }
            return event
        }
        model.receive(v1("session_info", ["model": "fixture-model", "foreground_allowed": true, "ask_user_enabled": true]))
        model.receive(v1("turn_started", ["text": "本轮任务"]))
        model.receive(v1("approval", ["approval_id": "approval-1", "app_id": "app", "window_id": "w", "text": "允许前台？", "scope": "session"]))
        model.receive(v1("skill_state", ["name": "single-skill", "digest": "digest", "pending": false]))
        model.sendingMessage = true
        precondition(model.controls.foregroundAllowed && model.controls.canonicalTurnID == "turn-0" && model.controls.approvals.count == 1)
        func baseline() -> String {
            "\(model.phase)|\(model.sendingMessage)|\(model.controls.foregroundAllowed)|\(model.controls.approvals.map(\.id))|\(model.controls.pending.count)|"
            + "\(model.controls.turnIndex)|\(model.controls.canonicalTurnID ?? "")|\(model.controls.turnClosed)|\(model.controls.result)|"
            + "\(model.controls.effectiveSkill ?? "")|\(model.conversationMessages.count)|\(model.error)|\(model.conversationOpen)"
        }
        var expected = baseline()

        model.refreshMemory()
        let list = model.bridge.commands.last!
        let listID = list["command_id"] as! String
        precondition(list["command"] as? String == "memory_list" && list["task_id"] as? String == "task-live", "live 读取带当前 task_id")
        await drain()
        precondition(baseline() == expected && model.controls.memory.pending[listID] != nil, "带 task 的 ack 不影响会话")
        model.receive(v1("memory_catalog", catalog([item("p1")], generation: 1, source: listID)))
        precondition(snapshot(model) == "1|p1@1:candidate|1/0/0|true|0/0" && baseline() == expected, snapshot(model))
        let sends = model.bridge.commands.count
        model.receive(v1("memory_catalog", catalog([item("p1"), item("p2")], generation: 2)))
        precondition(snapshot(model) == "2|p1@1:candidate,p2@1:candidate|2/0/0|true|0/0" && model.bridge.commands.count == sends && baseline() == expected,
                     "live 提案只成为候选，不提示、不自动批准")
        let sequenceBefore = model.controls.envelopeSequence

        // 当前 task/session 围栏与严格信封。
        var foreign = v1("memory_catalog", catalog([], generation: 9)); foreign["native_task_id"] = "old-task"
        model.receive(foreign)
        var otherSession = v1("memory_catalog", catalog([], generation: 9)); otherSession["session_id"] = "old-session"
        model.receive(otherSession)
        var legacyOld = legacy("memory_catalog", catalog([], generation: 9)); legacyOld["task_id"] = "old-task"
        model.receive(legacyOld)
        var duplicate = v1("memory_catalog", catalog([], generation: 9)); duplicate["event_id"] = "stream-m:\(sequence - 3)"
        model.receive(duplicate)
        var replay = v1("memory_catalog", catalog([], generation: 9)); replay["sequence"] = 1
        model.receive(replay)
        var malformedPayload = v1("memory_catalog", catalog([item("x")], generation: 9)); malformedPayload["policy"] = "auto_approve"
        model.receive(malformedPayload)
        precondition(snapshot(model).hasPrefix("2|p1@1:candidate,p2@1"), snapshot(model))
        precondition(model.controls.envelopeSequence == sequenceBefore && baseline() == expected, "被拒记忆帧不消费 sequence、不影响会话")
        var badVersion = v1("memory_catalog", catalog([], generation: 9)); badVersion["schema_version"] = 2
        model.receive(badVersion)
        precondition(snapshot(model).hasPrefix("2|p1@1") && !model.error.isEmpty, "严格信封校验仍然保留")
        model.error = ""
        var missingText = v1("memory_catalog", catalog([], generation: 9)); missingText["text"] = nil
        model.receive(missingText)
        precondition(snapshot(model).hasPrefix("2|p1@1") && !model.error.isEmpty)
        model.error = ""

        // 带 task 的读取被 host 拒绝：error 带回命令自己的 task_id，只退役记忆命令，不设全局 error、不改会话。
        model.refreshMemory()
        let rejectedListID = model.bridge.commands.last!["command_id"] as! String
        await drain()
        model.receive(["event": "error", "task_id": "task-live", "command_id": rejectedListID, "text": "会话标识已过期"])
        precondition(model.controls.memory.pending.isEmpty && baseline() == expected && snapshot(model).hasPrefix("2|p1@1"))
        precondition(model.controls.memory.status == L("执行端拒绝记忆命令：%@ 状态未改变，请刷新后核实。", L("会话标识已过期")))

        // 连接级变更在 live 中也不带 task；跨轮次等待自己的 control_result 与目录。
        model.approveMemory(model.controls.memory.entries.first { $0.id == "p1" }!)
        let approveID = model.bridge.commands.last!["command_id"] as! String
        precondition(model.bridge.commands.last!["task_id"] == nil && baseline() == expected)
        await drain()
        precondition(baseline() == expected && model.controls.memory.pending[approveID] != nil)
        model.receive(v1("turn_finished", ["state": "completed", "text": "完成"]))
        precondition(model.controls.turnClosed && model.controls.memory.pending[approveID] != nil, "轮次结束不丢记忆 pending")
        model.refreshMemory()
        let closedListID = model.bridge.commands.last!["command_id"] as! String
        precondition(model.bridge.commands.last!["task_id"] as? String == "task-live")
        turnID = "turn-1"; turnIndex = 1
        model.receive(v1("turn_started", ["text": "下一轮"]))
        precondition(model.controls.turnIndex == 1 && model.controls.memory.pending.count == 2, "新轮次保留会话级记忆 pending")
        expected = baseline()
        // 连接级命令的结果若带当前 task 也不匹配作用域：吞掉不改状态。
        model.receive(v1("control_result", ["command_id": approveID, "status": "completed", "error_code": NSNull()]))
        precondition(model.controls.memory.pending[approveID] != nil && baseline() == expected)
        model.receive(legacy("control_result", ["command_id": approveID, "status": "completed", "memory_id": "p1", "revision": 2,
                                                "generation": 3, "error_code": NSNull(), "text": "记忆已更新"]))
        precondition(model.controls.memory.entries.first { $0.id == "p1" }?.isCandidate == true && model.controls.memory.awaitingCatalog[approveID] != nil)
        model.receive(legacy("memory_catalog", catalog([item("p1", "approved", revision: 2), item("p2")], generation: 3, source: approveID)))
        model.receive(v1("memory_catalog", catalog([item("p1", "approved", revision: 2), item("p2")], generation: 3, source: closedListID)))
        precondition(snapshot(model) == "3|p1@2:approved,p2@1:candidate|1/1/0|true|0/0" && baseline() == expected, snapshot(model))

        // 轮次终态后仍按 task fence + generation 接收提案，不改轮次身份。
        model.receive(v1("turn_finished", ["state": "completed", "text": "完成"]))
        expected = baseline()
        precondition(model.controls.turnClosed && model.controls.canonicalTurnID == "turn-1")
        model.receive(v1("memory_catalog", catalog([item("p1", "approved", revision: 2), item("p2"), item("p3")], generation: 4)))
        precondition(snapshot(model) == "4|p1@2:approved,p2@1:candidate,p3@1:candidate|2/1/0|true|0/0" && baseline() == expected, snapshot(model))
        model.receive(v1("memory_catalog", catalog([], generation: 3)))
        precondition(snapshot(model).hasPrefix("4|"), "跨轮次旧 generation 仍被拒绝")

        // 会话切换：目录是连接数据可保留；绑定旧 task 的读取按未确认退役。
        model.refreshMemory()
        let staleListID = model.bridge.commands.last!["command_id"] as! String
        model.forgetMemory(model.controls.memory.entries.first { $0.id == "p1" }!)
        let forgetID = model.bridge.commands.last!["command_id"] as! String
        model.resetSessionControls()
        precondition(model.controls.memory.entries.count == 3 && model.controls.memory.pending[staleListID] == nil && model.controls.memory.pending[forgetID] != nil)
        precondition(model.controls.memory.status == L("记忆目录读取未确认；请刷新。"))
        model.receive(legacy("memory_catalog", catalog([], generation: 5, source: staleListID)))
        precondition(snapshot(model).hasPrefix("4|"), "已退役读取的目录按过期丢弃")

        // 断开：清 pending 与可用性，保留只读目录并要求刷新；不乐观删除。
        model.bridge.onExit?()
        precondition(!model.ready && model.disconnected && !model.controls.memory.available && model.controls.memory.generation == nil)
        precondition(model.controls.memory.pending.isEmpty && model.controls.memory.awaitingCatalog.isEmpty && model.controls.memory.entries.count == 3)
        precondition(model.controls.memory.status == L("连接已断开：记忆目录与待确认操作均未确认；重新连接后请刷新。"))
        model.receive(legacy("control_result", ["command_id": forgetID, "status": "completed", "error_code": NSNull(), "text": "记忆已更新"]))
        model.receive(legacy("memory_catalog", catalog([], generation: 5, source: forgetID)))
        precondition(model.controls.memory.entries.count == 3 && model.controls.memory.status == L("连接已断开：记忆目录与待确认操作均未确认；重新连接后请刷新。"))
        let offline = model.bridge.commands.count
        model.refreshMemory()
        model.forgetMemory(model.controls.memory.entries.first { $0.id == "p1" }!)
        precondition(model.bridge.commands.count == offline, "离线不发送、不自动重发")
        print("记忆 live 契约通过：v1 task fence、提案不自动批准、严格信封、终态后接收、跨轮 pending、会话切换保留目录、断开清 pending/可用性；phase/发送/前台授权/审批/Skill/轮次身份不变。")
    }

    // memory_store 分页形状：候选在前、已批准在后；每页 64 条，omitted = 总数 - 本页长度。
    static func page(_ offset: Int, generation: Int, source: Any = NSNull(), candidates: Int = 128, approved: Int = 256) -> [String: Any] {
        let total = candidates + approved
        let entries = (offset..<max(offset, min(offset + 64, total))).map { $0 < candidates ? item("c\($0)") : item("a\($0)", "approved", revision: 2) }
        let end = offset + entries.count
        return ["entries": entries, "generation": generation, "candidate_count": candidates, "approved_count": approved,
                "omitted": total - entries.count, "offset": offset, "next_offset": !entries.isEmpty && end < total ? end as Any : NSNull(),
                "root": "/private/memory", "policy": "candidate_review", "available": true, "error_code": NSNull(), "source_command_id": source]
    }
    @MainActor static func pageState(_ model: AppModel) -> String {
        let m = model.controls.memory
        return "\(m.generation ?? -1)@\(m.offset)->\(m.nextOffset.map(String.init) ?? "nil")|\(m.entries.first?.id ?? "-")..\(m.entries.last?.id ?? "-")|"
            + "\(m.entries.count)/\(m.omitted)|\(m.pending.count)/\(m.awaitingCatalog.count)|\(m.catalogUnknown)"
    }

    @MainActor static func pagingContracts(directory: URL) async {
        let model = AppModel(dataDirectory: directory)
        model.receive(["event": "ready", "protocol": 1])
        func lastList() -> [String: Any] { memoryCommands(model).last! }
        model.refreshMemory()
        var list = lastList()
        precondition(list["offset"] == nil && list.count == 2, "首页读取不带 offset，与旧 host 兼容")
        model.receive(legacy("memory_catalog", page(0, generation: 10, source: list["command_id"]!)))
        precondition(pageState(model) == "10@0->64|c0..c63|64/320|0/0|false", pageState(model))

        // 非法分页字段整帧忽略（用更新 generation，否则会被接受）。
        let valid = page(0, generation: 99)
        var invalid: [(String, [String: Any])] = []
        func variant(_ name: String, _ change: (inout [String: Any]) -> Void) { var value = valid; change(&value); invalid.append((name, value)) }
        variant("offset-1") { $0["offset"] = -1 }
        variant("offset385") { $0["offset"] = 385 }
        variant("offsetFloat") { $0["offset"] = 0.0 }
        variant("offsetBool") { $0["offset"] = false }
        variant("offsetText") { $0["offset"] = "0" }
        variant("offsetNull") { $0["offset"] = NSNull() }
        variant("nextMismatch") { $0["next_offset"] = 63 }
        variant("nextFloat") { $0["next_offset"] = 64.0 }
        variant("nextBool") { $0["next_offset"] = true }
        variant("nextText") { $0["next_offset"] = "64" }
        variant("nextNullButMore") { $0["next_offset"] = NSNull() }
        variant("nextBeyondTotal") { value in value = page(0, generation: 99, candidates: 64, approved: 0); value["next_offset"] = 64 }
        variant("nextEmptyPage") { value in value = page(384, generation: 99); value["source_command_id"] = NSNull(); value["offset"] = 0; value["next_offset"] = 0 }
        variant("proposalNotFirstPage") { $0 = page(64, generation: 99) }
        let before = pageState(model)
        for (name, value) in invalid {
            model.receive(legacy("memory_catalog", value))
            precondition(pageState(model) == before && model.error.isEmpty, "非法分页不得改变状态：" + name)
        }

        // 翻页不乐观切换：只发请求，offset/条目等待目录。
        model.nextMemoryPage()
        list = lastList()
        var listID = list["command_id"] as! String
        precondition(list["offset"] as? Int == 64 && list.count == 3 && pageState(model).hasPrefix("10@0->64|c0..c63"), pageState(model))
        model.nextMemoryPage(); model.refreshMemory(offset: 128)
        precondition(memoryCommands(model).count == 2, "读取进行中不重复翻页")
        // 回复页与请求页不符、或为更老 generation：整帧忽略，pending 保留。
        model.receive(legacy("memory_catalog", page(128, generation: 10, source: listID)))
        model.receive(legacy("memory_catalog", page(64, generation: 9, source: listID)))
        var badNext = page(64, generation: 10, source: listID); badNext["next_offset"] = 127
        model.receive(legacy("memory_catalog", badNext))
        precondition(pageState(model).hasPrefix("10@0->64|c0..c63") && model.controls.memory.pending[listID] != nil, pageState(model))
        model.receive(legacy("memory_catalog", page(64, generation: 10, source: listID)))
        precondition(pageState(model) == "10@64->128|c64..c127|64/320|0/0|false", pageState(model))

        // 旧分页回复：超时退役后迟到的页不覆盖，不自动重发。
        model.nextMemoryPage()
        let staleID = lastList()["command_id"] as! String
        model.expireMemoryControl(staleID)
        precondition(!model.controls.memory.catalogUnknown, "读取超时不使目录只读")
        let sent = memoryCommands(model).count
        model.receive(legacy("memory_catalog", page(128, generation: 10, source: staleID)))
        precondition(pageState(model).hasPrefix("10@64->128|c64") && memoryCommands(model).count == sent, pageState(model))

        // 非首页条目可审阅：批准数量已满时先拒绝第 2 页候选 c100；变更目录为首页。
        model.rejectMemory(model.controls.memory.entries.first { $0.id == "c100" }!)
        let approve = lastList()
        let approveID = approve["command_id"] as! String
        precondition(approve["command"] as? String == "memory_reject" && approve["memory_id"] as? String == "c100" && approve["revision"] as? Int == 1)
        model.refreshMemory(offset: 128)
        listID = lastList()["command_id"] as! String
        precondition(lastList()["offset"] as? Int == 128, "变更等待中仍可翻页阅读")
        // control_result 的 generation 无效时整帧忽略。
        for bad in [nil, -1, true, 11.0, "11"] as [Any?] {
            var result: [String: Any] = ["command_id": approveID, "status": "completed", "memory_id": "c100", "revision": 2, "error_code": NSNull()]
            result["generation"] = bad
            model.receive(legacy("control_result", result))
            precondition(model.controls.memory.pending[approveID] != nil && model.controls.memory.awaitingCatalog.isEmpty, "无效 generation 的 control_result 不结算")
        }
        model.receive(legacy("control_result", ["command_id": approveID, "status": "completed", "memory_id": "c100", "revision": 2,
                                                "generation": 11, "error_code": NSNull(), "text": "记忆已更新"]))
        precondition(model.controls.memory.busy && model.controls.memory.awaitingCatalog[approveID]?.generation == 11)
        // 较老 generation 的翻页回复可作为阅读数据，但不能结算 generation 11 的变更。
        model.receive(legacy("memory_catalog", page(128, generation: 10, source: listID)))
        precondition(pageState(model) == "10@128->192|a128..a191|64/320|0/1|false" && model.controls.memory.busy, pageState(model))
        // 变更目录非首页或早于结果 generation：忽略。
        model.receive(legacy("memory_catalog", page(64, generation: 11, source: approveID, candidates: 127, approved: 256)))
        model.receive(legacy("memory_catalog", page(0, generation: 10, source: approveID, candidates: 127, approved: 256)))
        precondition(pageState(model).hasPrefix("10@128") && model.controls.memory.busy, pageState(model))
        model.receive(legacy("memory_catalog", page(0, generation: 11, source: approveID, candidates: 127, approved: 256)))
        precondition(pageState(model) == "11@0->64|c0..c63|64/319|0/0|false" && !model.controls.memory.busy, pageState(model))

        // 逐页到最后一页（拒绝后共 383 条），每页只在收到目录后前进。
        for offset in stride(from: 64, through: 320, by: 64) {
            model.nextMemoryPage()
            precondition(lastList()["offset"] as? Int == offset && model.controls.memory.offset == offset - 64)
            model.receive(legacy("memory_catalog", page(offset, generation: 11, source: lastList()["command_id"]!, candidates: 127, approved: 256)))
        }
        precondition(pageState(model) == "11@320->nil|a320..a382|63/320|0/0|false", pageState(model))
        let atEnd = memoryCommands(model).count
        model.nextMemoryPage(); model.refreshMemory(offset: 385)
        precondition(memoryCommands(model).count == atEnd, "最后一页或越界 offset 不发送")
        // 编辑最后一条（非前 64 条）。
        let last = model.controls.memory.entries.last!
        model.updateMemory(last, title: last.title, content: "改后的正文", kind: last.kind, app: "")
        let update = lastList()
        precondition(update["command"] as? String == "memory_update" && update["memory_id"] as? String == "a382" && update["revision"] as? Int == 2)
        model.receive(legacy("control_result", ["command_id": update["command_id"]!, "status": "completed", "memory_id": "a382", "revision": 3,
                                                "generation": 12, "error_code": NSNull(), "text": "记忆已更新"]))
        model.receive(legacy("memory_catalog", page(0, generation: 12, source: update["command_id"]!, candidates: 128, approved: 255)))
        precondition(pageState(model).hasPrefix("12@0->64") && !model.controls.memory.busy, pageState(model))
        // 自动提案目录（首页）可在翻页后接受，但旧 generation 不覆盖。
        model.nextMemoryPage()
        model.receive(legacy("memory_catalog", page(64, generation: 12, source: lastList()["command_id"]!, candidates: 128, approved: 255)))
        model.receive(legacy("memory_catalog", page(0, generation: 11)))
        precondition(pageState(model).hasPrefix("12@64->128"), pageState(model))
        model.receive(legacy("memory_catalog", page(0, generation: 13)))
        precondition(pageState(model).hasPrefix("13@0->64"), pageState(model))
        // 返回首页不带 offset；首页没有上一页。
        model.nextMemoryPage()
        model.receive(legacy("memory_catalog", page(64, generation: 13, source: lastList()["command_id"]!)))
        model.previousMemoryPage()
        precondition(lastList()["command"] as? String == "memory_list" && lastList()["offset"] == nil && lastList().count == 2)
        model.receive(legacy("memory_catalog", page(0, generation: 13, source: lastList()["command_id"]!)))
        precondition(pageState(model) == "13@0->64|c0..c63|64/320|0/0|false", pageState(model))
        let first = memoryCommands(model).count
        model.previousMemoryPage()
        precondition(memoryCommands(model).count == first, "首页无上一页")
        print("记忆分页契约通过：首页兼容、\(invalid.count) 个非法分页帧、翻页不乐观、错页/旧页/旧 generation 回复忽略、非首页审核与末页条目编辑、变更目录首页、提案不被旧 generation 覆盖。")
    }

    // 收到 control_result 后目录丢失：等待目录也会超时，目录只读保留，刷新恢复，不自动重发。
    @MainActor static func catalogLossContracts(directory: URL) async {
        let model = AppModel(dataDirectory: directory)
        model.receive(["event": "ready", "protocol": 1])
        model.refreshMemory(offset: 320)
        model.receive(legacy("memory_catalog", page(320, generation: 20, source: memoryCommands(model).last!["command_id"]!)))
        precondition(pageState(model) == "20@320->nil|a320..a383|64/320|0/0|false", pageState(model))
        model.forgetMemory(model.controls.memory.entries.first { $0.id == "a383" }!)
        let forgetID = memoryCommands(model).last!["command_id"] as! String
        let sendIssued = model.controls.memory.pending[forgetID]!.started
        model.receive(legacy("control_result", ["command_id": forgetID, "status": "completed", "memory_id": "a383", "revision": NSNull(),
                                                "generation": 21, "error_code": NSNull(), "text": "记忆已更新"]))
        precondition(model.controls.memory.status == L("记忆已遗忘。") && model.controls.memory.busy && model.controls.memory.isPending(memory: "a383"))
        // 发送阶段的旧定时器不得提前结束等待目录阶段。
        model.expireMemoryControl(forgetID, issued: sendIssued)
        precondition(model.controls.memory.awaitingCatalog[forgetID] != nil && !model.controls.memory.catalogUnknown)
        let sent = memoryCommands(model).count
        model.forgetMemory(model.controls.memory.entries.first { $0.id == "a382" }!)
        precondition(memoryCommands(model).count == sent && model.controls.memory.status == L("上一条记忆操作仍在等待执行端确认。"), "等待目录期间不重复变更")
        // 等待目录超时：退役并提示刷新；条目不乐观删除，目录只读保留，不自动重发。
        model.expireMemoryControl(forgetID)
        let lost = L("已收到记忆操作结果，但未收到更新后的目录；目录状态未确认，仅可阅读。请刷新后核实，不要自动重发。")
        precondition(model.controls.memory.status == lost && !model.controls.memory.busy && memoryCommands(model).count == sent)
        precondition(pageState(model) == "20@320->nil|a320..a383|64/320|0/0|true", pageState(model))
        model.forgetMemory(model.controls.memory.entries.first { $0.id == "a382" }!)
        model.approveMemory(model.controls.memory.entries.first { $0.id == "a382" }!)
        precondition(memoryCommands(model).count == sent && model.controls.memory.status == L("记忆目录状态未确认，仅可阅读；请刷新后再操作。"), "目录未知时只读")
        // 迟到的同源目录按过期丢弃。
        model.receive(legacy("memory_catalog", page(0, generation: 21, source: forgetID, approved: 255)))
        precondition(pageState(model).hasPrefix("20@320") && model.controls.memory.catalogUnknown, pageState(model))
        // 刷新当前页恢复：只在收到目录后生效。
        model.refreshMemory()
        let refresh = memoryCommands(model).last!
        precondition(refresh["command"] as? String == "memory_list" && refresh["offset"] as? Int == 320)
        model.receive(legacy("memory_catalog", page(320, generation: 21, source: refresh["command_id"]!, approved: 255)))
        precondition(pageState(model) == "21@320->nil|a320..a382|63/320|0/0|false" && model.controls.memory.status.isEmpty, pageState(model))

        // 刷新也可在超时前结算等待：只接受不早于结果 generation 的目录。
        model.forgetMemory(model.controls.memory.entries.first { $0.id == "a382" }!)
        let secondID = memoryCommands(model).last!["command_id"] as! String
        model.receive(legacy("control_result", ["command_id": secondID, "status": "completed", "memory_id": "a382", "revision": NSNull(),
                                                "generation": 23, "error_code": NSNull(), "text": "记忆已更新"]))
        model.refreshMemory()
        model.receive(legacy("memory_catalog", page(320, generation: 22, source: memoryCommands(model).last!["command_id"]!, approved: 255)))
        precondition(model.controls.memory.generation == 22 && model.controls.memory.awaitingCatalog[secondID] != nil && model.controls.memory.busy,
                     "更老 generation 的目录不结算已成功变更")
        model.refreshMemory()
        model.receive(legacy("memory_catalog", page(320, generation: 23, source: memoryCommands(model).last!["command_id"]!, approved: 254)))
        precondition(pageState(model) == "23@320->nil|a320..a381|62/320|0/0|false" && !model.controls.memory.busy, pageState(model))
        precondition(model.controls.memory.retired.contains(secondID)
                     && memoryCommands(model).filter { $0["command"] as? String == "memory_forget" }.count == 2, "不自动重发")
        print("记忆目录丢失契约通过：写等待持续到目录、等待目录独立超时、旧定时器失效、未知目录只读、不自动重发、迟到目录丢弃、刷新恢复、旧 generation 不结算。")
    }

    @MainActor static func main() async {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        await idleContracts(directory: root.appendingPathComponent("idle"))
        await liveContracts(directory: root.appendingPathComponent("live"))
        await pagingContracts(directory: root.appendingPathComponent("paging"))
        await catalogLossContracts(directory: root.appendingPathComponent("loss"))
    }
}
