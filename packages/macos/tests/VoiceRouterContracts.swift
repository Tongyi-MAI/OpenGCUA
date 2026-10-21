import AppKit
import SwiftUI

// Fixture transport exercises the real AppModel routing without keys, network, or desktop actions.
@MainActor final class RuntimeBridge {
    var onEvent: (([String: Any]) -> Void)?
    var onExit: (() -> Void)?
    var running = true
    var commands: [[String: Any]] = []
    var acknowledge = true
    var beforeSend: (([String: Any]) -> Void)?
    var failSend = false
    func launch(dataDirectory: URL) throws {}
    func shutdown() {}
    func send(_ command: [String: Any]) throws {
        beforeSend?(command)
        if failSend { throw AppError.message("Fixture transport unavailable") }
        commands.append(command)
        guard acknowledge else { return }
        Task { @MainActor in
            if let message = command["message_id"] {
                onEvent?(["event": "message", "task_id": command["task_id"]!, "message_id": message, "state": "queued"])
            }
            if let id = command["command_id"] {
                var event: [String: Any] = ["event": "command_ack", "command_id": id]
                if let task = command["task_id"] { event["task_id"] = task }
                onEvent?(event)
            }
        }
    }
}
@MainActor final class AppDelegate {
    var onRefresh: (() -> Void)?
    func showSettings() {}
    func collapse(automatically: Bool = false) { onRefresh?() }
    func expand() {}
    func languageChanged() {}
    var obstructsOverlay = false
    func overlayWouldObstruct(_ event: [String: Any]) -> Bool { obstructsOverlay }
    func refreshCompact() { onRefresh?() }
    func runtimeExited() {}
    // 仅供生产 UI 闭包 typecheck；这些 fixture 不创建窗口。
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
@main enum VoiceRouterContracts {
    @MainActor static func recordedReplay(_ path: String) throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        let fixture = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let model = AppModel(dataDirectory: directory)
        model.ready = true; model.phase = .running; model.taskID = fixture["native_task_id"] as! String
        for segment in fixture["segments"] as! [[String: Any]] {
            for event in segment["events"] as! [[String: Any]] { model.receive(event) }
            let expected = segment["expected_phase"] as! String
            let actual = model.controls.interrupted ? "aborted"
                : model.phase == .completed ? "completed"
                : model.phase == .failed ? "attention"
                : model.phase == .stopped ? "aborted" : "running"
            precondition(actual == expected, "同一录制流的 Swift 终态必须与 TUI/Hub 一致：\(actual) / \(expected)")
            precondition(model.controls.canonicalTurnID == segment["turn_id"] as? String)
            if let actions = segment["actions"] as? [String: String] {
                for (id, stage) in actions {
                    let action = model.controls.recentActions.last { $0.id == id }
                    precondition(action?.stage == stage && action?.verification == "unverified")
                }
            }
            precondition(model.bridge.commands.isEmpty, "录制回放不得派发动作、审批或重试")
        }
        print("D01 同源录制回放通过：真实 Swift AppModel 与 TUI/Hub 的终态、轮次和动作事实一致；无派发。")
    }

    @MainActor static func main() async throws {
        if CommandLine.arguments.count == 2 {
            try recordedReplay(CommandLine.arguments[1])
            return
        }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let model = AppModel(dataDirectory: directory)
        model.ready = true; model.voiceMode = true; model.phase = .running; model.taskID = "task-a"
        model.receive(["event": "progress", "task_id": "task-a", "phase": "planning", "step": 1])
        precondition(model.taskProgressTitle == "思考中" && model.step == 1)
        model.receive(["event": "intent", "task_id": "task-a", "text": "打开 Chrome"])
        precondition(model.recentAction.isEmpty, "Planned actions must not be reported as dispatched")
        model.receive(["event": "progress", "task_id": "task-a", "phase": "execution", "step": 1])
        precondition(model.taskProgressTitle == "正在操作" && model.taskProgressDetail == "打开 Chrome")
        model.receive(["event": "progress", "task_id": "task-a", "phase": "postprocess", "step": 1])
        precondition(model.taskProgressTitle == "正在检查结果")
        model.receive(["event": "progress", "task_id": "task-a", "phase": "planning", "step": 2])
        precondition(model.taskProgressTitle == "思考中" && model.intent.isEmpty && model.taskProgressDetail == "最近动作：打开 Chrome")
        model.receive(["event": "progress", "task_id": "old-task", "phase": "execution", "step": 99])
        precondition(model.step == 2 && model.taskProgressTitle == "思考中")
        model.receive(["event": "skill_loaded", "task_id": "task-a", "name": "pdf-form", "source": "bundled"])
        precondition(model.conversationMessages.last?.text.contains("pdf-form") == true,
                     "Skill activation must be visible in the native conversation")
        model.receive(["event": "skill_warning", "task_id": "task-a", "text": "Invalid Skill metadata"])
        precondition(model.conversationMessages.last?.text.contains("Invalid Skill metadata") == true,
                     "Skill validation warnings must be visible in the native conversation")
        let context = VoiceTaskContext(taskID: "task-a", questionID: "")
        let result = await model.handleVoiceTool(VoiceTaskCall(id: "call-a", name: "queue_task", text: "Only use Chrome"), context: context)
        precondition(result["accepted"] as? Bool == true)
        precondition(model.bridge.commands.last?["command"] as? String == "message")
        precondition(model.bridge.commands.last?["text"] as? String == "Only use Chrome")
        precondition(model.voiceMode && !model.sendingMessage && model.messageDraft.isEmpty)
        let count = model.bridge.commands.count
        let stale = await model.handleVoiceTool(VoiceTaskCall(id: "stale", name: "stop_task", text: ""), context: VoiceTaskContext(taskID: "old-task", questionID: ""))
        precondition(stale["accepted"] as? Bool == false && model.bridge.commands.count == count)
        model.receive(["event": "question", "task_id": "task-a", "question_id": "q1", "text": "Which browser?"])
        precondition(model.voiceMode && model.phase == .waiting)
        precondition(model.taskProgressTitle == "需要你的回复" && model.taskProgressDetail == "Which browser?")
        let reply = await model.handleVoiceTool(VoiceTaskCall(id: "reply", name: "queue_task", text: "Chrome"), context: VoiceTaskContext(taskID: "task-a", questionID: "q1"))
        precondition(reply["accepted"] as? Bool == true)
        precondition(model.bridge.commands.last?["command"] as? String == "reply" && model.bridge.commands.last?["question_id"] as? String == "q1")
        model.receive(["event": "reply", "task_id": "task-a"])
        model.receive(["event": "state", "task_id": "task-a", "state": "running"])
        let stopped = await model.handleVoiceTool(VoiceTaskCall(id: "stop", name: "stop_task", text: ""), context: VoiceTaskContext(taskID: "task-a", questionID: ""))
        precondition(stopped["accepted"] as? Bool == true && model.voiceMode && model.phase == .stopping)
        model.receive(["event": "finished", "task_id": "task-a", "state": "stopped", "text": "Stopped"])
        precondition(model.voiceMode && model.phase == .stopped)
        precondition(model.taskProgressTitle == "任务已停止" && model.taskProgressDetail == "Stopped")
        // Disconnecting voice resolves outstanding waits, without sending stop to the computer.
        model.phase = .running; model.bridge.acknowledge = false
        let pending = Task { await model.handleVoiceTool(VoiceTaskCall(id: "pending", name: "queue_task", text: "Read only"), context: VoiceTaskContext(taskID: "task-a", questionID: "")) }
        await Task.yield()
        let beforeClose = model.bridge.commands.count
        model.stopSpeech()
        let closed = await pending.value
        precondition(closed["accepted"] as? Bool == false)
        precondition(!model.voiceMode && model.phase == .running && model.bridge.commands.count == beforeClose)
        // Completion text must be immediately available to either compact presentation.
        let fullAnswer = (1...40).map { "结果第 \($0) 行：保留完整文本。" }.joined(separator: "\n")
        for state in [TaskPhase.completed, .failed, .stopped] {
            model.reset(); model.phase = .running; model.taskID = "terminal-" + state.rawValue
            let activeHeight = model.compactPanelHeight
            model.receive(["event": "finished", "task_id": model.taskID, "state": state.rawValue, "text": fullAnswer])
            precondition(model.showsCompactResult && model.compactResultText == fullAnswer)
            precondition(model.compactPanelHeight > activeHeight)
            model.receive(["event": "finished", "task_id": "stale-task", "state": "completed", "text": "Wrong result"])
            precondition(model.compactResultText == fullAnswer, "Stale completion must not replace the displayed result")
            model.voiceMode = true
            precondition(model.showsCompactResult && model.compactResultText == fullAnswer)
            let resultHeight = model.compactPanelHeight
            model.phase = .running
            precondition(!model.showsCompactResult && model.compactPanelHeight < resultHeight)
            model.voiceMode = false; model.finish(state, text: "")
            precondition(!model.compactResultText.isEmpty, "Empty terminal payload needs a visible fallback")
            model.reset()
            precondition(!model.showsCompactResult && model.compactResultText.isEmpty && model.answer.isEmpty)
        }
        // Idle agent turns accept text/voice follow-ups without starting another worker.
        model.bridge.acknowledge = true; model.ready = true; model.voiceMode = false
        model.taskID = "chat"; model.taskTitle = "Remember 42"; model.currentTurnInstruction = "Remember 42"
        model.conversationOpen = true; model.phase = .running
        model.conversationMessages = [ConversationMessage(id: "initial", text: "Remember 42", isUser: true)]
        model.receive(["event": "turn_finished", "task_id": "chat", "turn_index": 0, "state": "completed", "text": "Saved 42"])
        precondition(model.conversationOpen && !model.phase.active && model.conversationTurns.count == 1)
        model.messageDraft = "What was the number?"; model.sendInput()
        precondition(model.phase == .starting && model.bridge.commands.last?["command"] as? String == "message")
        await Task.yield()
        precondition(model.conversationMessages.last?.text == "What was the number?" && model.conversationMessages.last?.isUser == true)
        model.receive(["event": "turn_started", "task_id": "chat", "turn_index": 1, "text": "What was the number?"])
        model.receive(["event": "turn_finished", "task_id": "chat", "turn_index": 1, "state": "completed", "text": "42"])
        model.receive(["event": "turn_finished", "task_id": "chat", "turn_index": 1, "state": "completed", "text": "42"])
        precondition(model.conversationTurns.count == 2 && model.conversationMessages.count == 4)
        model.voiceMode = true
        let continued = await model.handleVoiceTool(VoiceTaskCall(id: "follow", name: "queue_task", text: "Remember another number"), context: VoiceTaskContext(taskID: "chat", questionID: "", wasActive: false))
        precondition(continued["accepted"] as? Bool == true && model.bridge.commands.last?["command"] as? String == "message")
        model.receive(["event": "turn_started", "task_id": "chat", "turn_index": 2, "text": "Remember another number"])
        model.receive(["event": "question", "task_id": "chat", "question_id": "number", "text": "Which number?"])
        model.receive(["event": "reply", "task_id": "chat", "question_id": "number", "text": "84"])
        precondition(model.conversationMessages.suffix(2).map(\.text) == ["Which number?", "84"])
        model.receive(["event": "turn_finished", "task_id": "chat", "turn_index": 2, "state": "completed", "text": "Saved 84"])
        let persisted = try JSONDecoder().decode([TaskRecord].self, from: Data(contentsOf: directory.appendingPathComponent("history.json")))
        precondition(persisted.first?.turns?.count == 3 && persisted.first?.messages?.last?.text == "Saved 84")
        // Older histories remain readable, with no live session resurrected.
        let legacy = Data("[{\"id\":\"old\",\"title\":\"Old task\",\"date\":0,\"state\":\"completed\",\"answer\":\"Done\",\"path\":\"\"}]".utf8)
        let legacyRecords = try JSONDecoder().decode([TaskRecord].self, from: legacy)
        precondition(legacyRecords.first?.messages == nil)
        model.reset()
        precondition(model.phase == .completed && model.sendingMessage, "Keep the conversation visible until end is acknowledged")
        await Task.yield()
        precondition(model.bridge.commands.last?["command"] as? String == "end" && model.phase == .idle && model.conversationMessages.isEmpty)
        model.receive(["event": "finished", "task_id": "chat", "state": "completed", "text": "Old session closed"])
        precondition(model.phase == .idle && model.answer.isEmpty && !model.conversationOpen)
        model.taskID = "racing"; model.phase = .completed; model.conversationOpen = true
        model.bridge.acknowledge = false; model.reset()
        let resetID = model.bridge.commands.last?["command_id"] as! String
        model.receive(["event": "turn_started", "task_id": "racing", "text": "Already queued"])
        model.receive(["event": "error", "task_id": "racing", "command_id": resetID, "text": "Task is now running"])
        precondition(model.phase == .running && model.taskID == "racing" && model.conversationOpen && !model.sendingMessage)
        let restoredDirectory = directory.appendingPathComponent("history-tests")
        let runA = restoredDirectory.appendingPathComponent("runs/a")
        let runB = restoredDirectory.appendingPathComponent("runs/b")
        for path in [runA, runB] {
            try FileManager.default.createDirectory(at: path, withIntermediateDirectories: true)
            try Data("{}".utf8).write(to: path.appendingPathComponent("traj.json"))
        }
        let restored = AppModel(dataDirectory: restoredDirectory)
        restored.ready = true
        let config = restoredDirectory.appendingPathComponent("fixture.json")
        try Data(#"{"llm_base_url":"http://127.0.0.1:9999/v1","model_name":"fixture","api_key":"fixture-only"}"#.utf8).write(to: config)
        restored.preferences.configFilePath = config.path
        let a = TaskRecord(id: "saved-a", title: "Remember 42", state: "completed", answer: "42", path: runA.path)
        let b = TaskRecord(id: "saved-b", title: "Another chat", state: "completed", answer: "Done", path: runB.path)
        restored.history = [a, b]
        restored.messageDraft = "Unsent new chat draft"
        precondition(restored.openConversation(a))
        precondition(restored.restoresHistory && !restored.conversationOpen && restored.bridge.commands.isEmpty)
        precondition(restored.conversationMessages.map(\.text) == ["Remember 42", "42"])
        restored.messageDraft = "What was the number?"; restored.sendInput()
        let executionID = restored.taskID
        precondition(executionID != a.id && restored.currentConversationID == a.id)
        precondition(restored.bridge.commands.last?["resume_path"] as? String == runA.path)
        precondition(restored.bridge.commands.last?["text"] as? String == "What was the number?")
        precondition(restored.history.count == 2 && restored.history.first?.id == a.id)
        precondition(!restored.openConversation(b), "Switching must not abandon an active task")
        restored.receive(["event":"turn_finished", "task_id":executionID, "turn_index":1, "state":"completed", "text":"Still 42", "path":runA.path])
        let beforeReturn = restored.bridge.commands.count
        precondition(restored.openConversation(restored.history.first!))
        precondition(restored.bridge.commands.count == beforeReturn, "Returning to the live chat must retain its worker")
        restored.bridge.acknowledge = false
        restored.messageDraft = "Keep this draft"
        precondition(restored.openConversation(b))
        precondition(restored.currentConversationID == a.id && restored.sendingMessage)
        let switchID = restored.bridge.commands.last?["command_id"] as! String
        restored.receive(["event":"command_ack", "command_id":switchID, "task_id":executionID])
        precondition(restored.currentConversationID == b.id && restored.taskID.isEmpty && restored.restoresHistory)
        restored.receive(["event":"finished", "task_id":executionID, "state":"completed", "text":"Stale result"])
        precondition(restored.answer == "Done")
        let savedA = restored.history.first(where: { $0.id == a.id })!
        precondition(savedA.draft == "Keep this draft")
        precondition(restored.openConversation(savedA) && restored.messageDraft == "Keep this draft")
        let restart = AppModel(dataDirectory: restoredDirectory)
        precondition(restart.openConversation(restart.history.first(where: { $0.id == a.id })!))
        precondition(restart.restoresHistory && restart.bridge.commands.isEmpty && restart.messageDraft == "Keep this draft")
        let invalid = TaskRecord(id: "missing", title: "Missing", state: "completed", answer: "", path: directory.appendingPathComponent("outside").path)
        precondition(!restart.openConversation(invalid) && restart.currentConversationID == a.id)
        restored.reset()
        precondition(restored.phase == .idle && restored.messageDraft == "Unsent new chat draft")
        let mini = AppModel(dataDirectory: directory.appendingPathComponent("mini-tests"))
        let presentation = AppDelegate()
        mini.delegate = presentation; mini.ready = true; mini.bridge.acknowledge = false
        mini.preferences.configFilePath = config.path
        var visible = true
        presentation.onRefresh = { visible = !mini.compactHidesForComputerAction }
        mini.bridge.beforeSend = { command in
            if ["start", "message", "reply", "resume"].contains(command["command"] as? String ?? ""), mini.phase != .paused {
                precondition(visible, "Dispatching a task must not hide a non-obstructing badge")
            }
        }
        mini.messageDraft = "New task"; mini.sendInput()
        precondition(mini.phase == .starting && visible && mini.compactPanelHeight == 164)
        mini.receive(["event": "state", "task_id": mini.taskID, "state": "running"])
        mini.receive(["event": "progress", "task_id": mini.taskID, "phase": "planning"])
        precondition(visible, "Planning keeps the animated badge available")
        mini.receive(["event": "progress", "task_id": mini.taskID, "phase": "execution"])
        precondition(visible, "Execution alone must not hide the badge")
        mini.receive(["event": "progress", "task_id": mini.taskID, "phase": "postprocess"])
        precondition(visible, "Captures and postprocessing keep the badge visible")
        mini.receive(["event": "progress", "task_id": mini.taskID, "phase": "planning"])
        precondition(visible)
        mini.receive(["event": "overlay", "task_id": mini.taskID, "phase": "begin", "guard_id": "safe", "impact": "pointer"])
        precondition(visible && mini.overlayPointerBusy)
        precondition(mini.bridge.commands.last?["command"] as? String == "overlay_ack")
        mini.receive(["event": "overlay", "task_id": mini.taskID, "phase": "end", "guard_id": "safe"])
        presentation.obstructsOverlay = true
        mini.receive(["event": "overlay", "task_id": "stale-task", "phase": "begin", "guard_id": "stale"])
        precondition(visible && !mini.overlayPointerBusy)
        mini.receive(["event": "overlay", "task_id": mini.taskID, "phase": "begin", "guard_id": "hit", "impact": "pointer"])
        precondition(!visible && mini.overlayPointerBusy)
        mini.receive(["event": "progress", "task_id": mini.taskID, "phase": "planning"])
        precondition(!visible, "Progress cannot reveal an overlay before a conflicting action finishes")
        mini.receive(["event": "overlay", "task_id": mini.taskID, "phase": "end", "guard_id": "hit"])
        precondition(visible && !mini.overlayPointerBusy)
        mini.receive(["event": "overlay", "task_id": mini.taskID, "phase": "begin", "guard_id": "cancelled"])
        precondition(!visible)
        mini.receive(["event": "overlay", "task_id": mini.taskID, "phase": "reset"])
        precondition(visible, "Cancellation and worker reset restore the badge")
        mini.receive(["event": "turn_finished", "task_id": mini.taskID, "state": "completed", "text": "Done"])
        precondition(visible && mini.compactPanelHeight == 248)
        mini.messageDraft = "Continue"; mini.sendInput()
        precondition(visible)
        mini.receive(["event": "question", "task_id": mini.taskID, "question_id": "mini-q", "text": "Which app?"])
        precondition(visible)
        mini.reply = "Finder"; mini.command("reply")
        precondition(visible && mini.phase == .waiting)
        mini.receive(["event": "error", "task_id": mini.taskID, "text": "Reply rejected"])
        precondition(visible && mini.phase == .waiting && !mini.sendingMessage)
        mini.reply = "Finder"; mini.command("reply")
        mini.receive(["event": "reply", "task_id": mini.taskID, "text": "Finder"])
        precondition(visible && mini.phase == .running)
        mini.receive(["event": "state", "task_id": mini.taskID, "state": "paused"])
        precondition(visible)
        mini.messageDraft = "Stay paused"; mini.sendInput()
        precondition(visible && mini.phase == .paused)
        mini.receive(["event": "message", "task_id": mini.taskID,
                      "message_id": mini.bridge.commands.last!["message_id"]!, "state": "applied"])
        mini.executionPhase = "planning"
        mini.command("resume")
        precondition(visible && mini.phase == .running)
        mini.receive(["event": "turn_finished", "task_id": mini.taskID, "state": "completed", "text": "Done"])
        mini.bridge.failSend = true; mini.messageDraft = "Retry"; mini.sendInput()
        precondition(visible && mini.phase == .completed, "Transport failures must restore the panel and completed turn")
        mini.bridge.failSend = false; mini.bridge.acknowledge = true; mini.voiceMode = true; mini.error = ""
        let voiceFollowUp = await mini.handleVoiceTool(VoiceTaskCall(id: "mini-voice", name: "queue_task", text: "Continue by voice"),
                                                     context: VoiceTaskContext(taskID: mini.taskID, questionID: "", wasActive: false))
        // A voice follow-up keeps its connection and unobstructing badge visible.
        precondition(voiceFollowUp["accepted"] as? Bool == true && mini.voiceMode && visible)
        try controlContracts(directory: directory.appendingPathComponent("controls"), config: config)
        try sessionContracts(directory: directory.appendingPathComponent("sessions"), config: config)
        previewContracts(directory: directory.appendingPathComponent("previews"))
        envelopeActionContracts(directory: directory.appendingPathComponent("d01"))
        try envelopeValidationContracts(directory: directory.appendingPathComponent("d01-strict"))
        print("Mini window routing passed: visible across execution and captures, hidden only for guarded overlaps, synchronized acknowledgement, stale fences and reset recovery.")
        print("Historical chat contracts passed: read-only selection, resume request, stable history ID, fresh execution fence, switch acknowledgement, active-task protection, drafts and app restart.")
        print("Conversation contracts passed: text and voice follow-ups, bubble ordering, deduplication, persisted history, legacy decoding and fresh chat fencing.")
        print("Compact result contracts passed: completed, failed, stopped, full text, voice, stale task fence, fallback and reset.")
        let progress = AppModel(dataDirectory: directory.appendingPathComponent("progress"))
        progress.taskID = "progress"; progress.phase = .running
        for (stage, title) in [("waiting", "fixture API wait"), ("generating", "fixture thinking"), ("retrying", "fixture retry") ] {
            progress.receive(["event": "model_status", "task_id": "progress", "stage": stage, "text": title])
            precondition(progress.taskProgressTitle == title, "气泡必须使用实时 API 阶段，而非旧 intent")
        }
        progress.receive(["event": "progress", "task_id": "progress", "phase": "execution", "step": 4])
        precondition(progress.taskProgressTitle == L("正在操作") && progress.step == 4)
        progress.receive(["event": "intent_delta", "task_id": "progress", "text": "", "start": true, "block_index": 1, "step": 5])
        progress.receive(["event": "intent_delta", "task_id": "progress", "text": "打开会议室", "block_index": 1, "step": 5])
        progress.receive(["event": "intent_delta", "task_id": "progress", "text": "预定", "block_index": 1, "step": 5])
        precondition(progress.currentIntent == "打开会议室预定" && progress.taskProgressDetail == "打开会议室预定",
                     "流式 intent 必须在气泡和详情中与 API 状态同时显示")
        progress.receive(["event": "code_reset", "task_id": "progress", "step": 5])
        precondition(progress.currentIntent.isEmpty, "重试不能显示旧 attempt 的 intent")
        progress.receive(["event": "intent", "task_id": "progress", "text": "点击空闲会议室"])
        progress.receive(["event": "progress", "task_id": "progress", "phase": "execution", "step": 6])
        precondition(progress.currentIntent == "点击空闲会议室")
        progress.receive(["event": "question", "task_id": "progress", "question_id": "question", "text": "请确认开始时间"])
        precondition(progress.phase == .waiting && progress.currentIntent == "点击空闲会议室",
                     "等待回复时，气泡仍须显示当前 intent 而不是将其替换为问题或步骤")
        progress.phase = .running
        progress.receive(["event": "model_status", "task_id": "progress", "stage": "waiting", "attempt_id": "intent-a", "attempt": 1])
        progress.receive(["event": "intent_delta", "task_id": "progress", "text": "第一组", "block_index": 1, "step": 7, "attempt_id": "intent-a"])
        progress.receive(["event": "intent_end", "task_id": "progress", "block_index": 1, "step": 7, "attempt_id": "intent-a"])
        progress.receive(["event": "intent_delta", "task_id": "progress", "text": "第二组", "start": true, "block_index": 2, "step": 7, "attempt_id": "intent-a"])
        precondition(progress.currentIntent == "第二组")
        progress.receive(["event": "model_status", "task_id": "progress", "stage": "waiting", "attempt_id": "intent-b", "attempt": 2])
        progress.receive(["event": "intent_delta", "task_id": "progress", "text": "旧片段", "block_index": 2, "step": 7, "attempt_id": "intent-a"])
        precondition(progress.currentIntent.isEmpty, "过期 attempt 的 intent 不得污染重试")
        progress.phase = .completed; progress.controls.compactionRunning = true
        precondition(progress.taskProgressTitle == L("正在整理上下文"), "空闲轮次间的压缩也要显示真实状态")
        progress.disconnected = true
        precondition(progress.taskProgressTitle == L("连接已断开"))
        let interactive = AppModel(dataDirectory: directory.appendingPathComponent("interactive"))
        precondition(interactive.inputPlaceholder == L("在此输入…") && interactive.foregroundWarning.isEmpty)
        precondition(interactive.progressBubbleHeight == 44)
        interactive.taskID = "interactive"; interactive.phase = .running; interactive.conversationOpen = true
        precondition(interactive.inputPlaceholder == L("补充消息…"))
        interactive.receive(["event": "policy", "task_id": "interactive", "foreground_allowed": true, "ask_user_enabled": true])
        precondition(interactive.foregroundWarning == L("前台授权中：请勿操作电脑，以免打断 Agent 当前行为。"))
        precondition(interactive.progressBubbleHeight == 72)
        interactive.finish(.completed, text: "next message", keepingConversation: true)
        precondition(!interactive.foregroundWarning.isEmpty && interactive.inputPlaceholder == L("继续对话…"))
        interactive.receive(["event": "policy", "task_id": "interactive", "foreground_allowed": false, "ask_user_enabled": true])
        precondition(interactive.foregroundWarning.isEmpty && interactive.progressBubbleHeight == 44)
        interactive.controls.foregroundAllowed = true
        interactive.finish(.stopped, text: "closed")
        precondition(interactive.foregroundWarning.isEmpty, "会话结束时必须移除前台授权提醒")
        print("Foreground warning follows session approval/revocation; interactive input wording passed.")
        print("Progress bubble live API/execution/compaction/disconnect titles passed.")
        print("Voice router contracts passed: queue receipts, stale task fence, question reply, stop, session survival, disconnect uncertainty.")
    }
    // 与现有 fixture 一起编译真实 AppModel 和 SessionControls；不启动 host。
    @MainActor static func controlContracts(directory: URL, config: URL) throws {
        let model = AppModel(dataDirectory: directory)
        model.bridge.acknowledge = false
        model.preferences = Preferences(); model.preferences.configFilePath = config.path
        model.receive(["event": "ready", "protocol": 2])
        precondition(!model.ready)
        model.receive(["event": "ready", "protocol": 1])
        precondition(model.ready && model.bridge.commands.last?["command"] as? String == "skills_list")
        precondition(model.bridge.commands.last?["task_id"] == nil)
        let idleCatalogID = model.bridge.commands.last!["command_id"] as! String
        model.resetSessionControls()
        precondition(model.controls.pending[idleCatalogID] != nil, "同一连接的空闲目录请求不能被会话重置吞掉")
        model.receive(["event": "skills_catalog", "sequence": 1, "skills": [
            ["name": "frontmatter-name", "description": "From host", "source": "bundled"],
            ["name": "frontmatter-name", "description": "Duplicate", "source": "local"]], "selected_name": NSNull()])
        precondition(model.controls.catalogAvailable && model.controls.skills.count == 1)
        let localCount = model.bridge.commands.count
        model.selectSkill("frontmatter-name")
        precondition(model.bridge.commands.count == localCount && model.controls.selectedSkill == "frontmatter-name")
        model.receive(["event": "skills_catalog", "sequence": 0, "skills": []])
        precondition(model.controls.skills.count == 1)
        model.selectSkill("not-a-directory-name")
        precondition(model.controls.selectedSkill == "frontmatter-name" && !model.controls.catalogStatus.isEmpty)
        model.taskID = "historical-task-without-session"
        model.refreshSkills(reload: true)
        let reloadID = model.bridge.commands.last!["command_id"] as! String
        precondition(model.bridge.commands.last?["command"] as? String == "skills_reload" && model.bridge.commands.last?["task_id"] == nil)
        model.receive(["event": "control_result", "command_id": reloadID, "status": "ok", "text": "Reloaded"])
        precondition(!model.controlPending("skills_reload") && model.controls.result == "Reloaded")
        let untrusted = directory.appendingPathComponent("untrusted.json")
        try Data(#"{"llm_base_url":"http://127.0.0.1:9999/v1","model_name":"fixture","api_key":"fixture-only","foreground_allowed":true,"ask_user_enabled":false,"selected_skill":"file-choice"}"#.utf8).write(to: untrusted)
        model.preferences.configFilePath = untrusted.path
        model.preferences.showReasoning = true; model.preferences.showCode = true
        model.instruction = "New task"; model.start()
        let task = model.taskID
        let start = model.bridge.commands.last!
        let configuration = start["config"] as! [String: Any]
        precondition(start["command"] as? String == "start")
        precondition(configuration["selected_skill"] as? String == "frontmatter-name" && configuration["selected_skill_override"] as? Bool == true)
        precondition(configuration["foreground_allowed"] as? Bool == false && configuration["ask_user_enabled"] as? Bool == true)
        precondition(configuration["show_reasoning"] == nil && configuration["show_code"] == nil)
        var sequence = 0
        func emit(_ kind: String, _ fields: [String: Any] = [:], turn: Int = 0) {
            sequence += 1
            var event: [String: Any] = ["event": kind, "task_id": task, "turn_index": turn, "sequence": sequence]
            for (key, value) in fields { event[key] = value }
            model.receive(event)
        }
        emit("session_info", ["session_id": "session-1", "model": "actual", "protocol": "openai", "reasoning_effort": "high", "foreground_allowed": false, "ask_user_enabled": true])
        emit("turn_started", ["text": "New task"])
        precondition(model.phase == .running && model.controls.sessionID == "session-1" && model.controls.model == "actual")
        precondition(!model.canSwitchSession)
        emit("queue_state", ["items": [], "suspended": false])
        emit("skill_state", ["name": "frontmatter-name", "source": "bundled", "digest": "abc", "pending": false])
        model.selectSkill(nil)
        precondition(model.bridge.commands.last?["command"] as? String == "skill_select" && model.bridge.commands.last?["name"] is NSNull)
        precondition(model.controls.skillPending && model.controls.effectiveSkill == "frontmatter-name")
        emit("skill_state", ["name": NSNull(), "pending": true, "source": "", "digest": ""])
        precondition(model.controls.skillPending && model.controls.selectedSkill == nil)
        precondition(model.controls.effectiveSkill == "frontmatter-name" && model.controls.skillSource == "bundled" && model.controls.skillDigest == "abc")
        let skillSelectID = model.bridge.commands.last!["command_id"] as! String
        precondition(model.controlPending("skill_select"), "pending skill_state 只是期望值，不能消掉 skill_select 等待")
        emit("skill_state", ["name": "model-loaded", "source": "local", "digest": "def", "pending": false])
        precondition(model.controls.effectiveSkill == "model-loaded" && model.controls.selectedSkill == nil && model.controls.skillPending,
                     "模型本轮自行加载只同步生效值，不覆盖待生效期望")
        emit("skill_state", ["name": NSNull(), "source": "", "digest": "", "pending": false, "command_id": skillSelectID])
        precondition(model.controls.effectiveSkill == nil && model.controls.selectedSkill == nil && !model.controls.skillPending && !model.controlPending("skill_select"))
        emit("control_result", ["command_id": skillSelectID, "status": "completed", "text": "Skill 选择已生效"])
        precondition(model.controls.selectedSkill == model.controls.effectiveSkill && !model.controls.skillPending)
        model.setForegroundAllowed(true)
        let policyID = model.bridge.commands.last!["command_id"] as! String
        precondition(!model.controls.foregroundAllowed)
        emit("command_ack", ["command_id": policyID])
        precondition(!model.controls.foregroundAllowed && model.controlPending("set_policy"))
        emit("control_result", ["command_id": policyID, "status": "accepted", "text": "Accepted"])
        precondition(model.controlPending("set_policy") && !model.controls.foregroundAllowed)
        precondition(model.bridge.commands.last?["ask_user_enabled"] == nil)
        emit("policy", ["foreground_allowed": true, "ask_user_enabled": true])
        precondition(model.controls.foregroundAllowed)
        model.setAskUserEnabled(false)
        precondition(model.bridge.commands.last?["ask_user_enabled"] as? Bool == false && model.controls.askUserEnabled)
        precondition(model.bridge.commands.last?["foreground_allowed"] == nil)
        emit("policy", ["foreground_allowed": false, "ask_user_enabled": false])
        emit("approval", ["approval_id": "approval-1", "app_id": "app", "window_id": "window", "text": "Bring app forward", "scope": "session"])
        precondition(!model.controls.askUserEnabled && model.controls.approvals.count == 1)
        model.replyToApproval("approval-1")
        precondition(model.bridge.commands.last?["choice"] as? String == "reject")
        precondition(model.bridge.commands.last?["approval_id"] as? String == "approval-1" && model.approvalPending("approval-1"))
        emit("approval_closed", ["approval_id": "approval-1", "choice": "allow"])
        precondition(model.controls.approvals.isEmpty && !model.controls.foregroundAllowed, "关闭审批卡不能自行批准权限")
        emit("approval", ["approval_id": "approval-1", "text": "Late duplicate"])
        precondition(model.controls.approvals.isEmpty)
        for choice in ["allow", "cancel"] {
            emit("approval", ["approval_id": choice, "text": choice, "scope": "session"])
            model.replyToApproval(choice, choice: choice)
            precondition(model.bridge.commands.last?["choice"] as? String == choice)
            emit("approval_closed", ["approval_id": choice, "choice": choice])
        }
        emit("queue_state", ["items": [["id": "q1", "text": "Queued correction", "kind": "message"], ["id": "q1", "text": "Duplicate", "kind": "message"]], "suspended": true])
        precondition(model.controls.queue.count == 1)
        model.resumeQueue()
        precondition(model.bridge.commands.last?["command"] as? String == "queue_resume" && model.bridge.commands.last?["task_id"] as? String == task)
        emit("queue_state", ["items": [["id": "q1", "text": "Queued correction", "kind": "message"]], "suspended": false])
        model.clearQueue()
        precondition(model.bridge.commands.last?["command"] as? String == "queue_clear" && model.bridge.commands.last?["command_id"] is String)
        emit("message", ["message_id": "q1", "text": "Queued correction", "state": "cancelled"])
        emit("message", ["message_id": "q1", "text": "Late queued", "state": "queued"])
        precondition(model.controls.queue.isEmpty, "取消后不能被迟到的 queued 重新加入队列")
        emit("queue_state", ["items": [], "suspended": false])
        emit("message", ["message_id": "q2", "text": "Consumed", "state": "queued"])
        emit("message", ["message_id": "q2", "state": "consumed"])
        emit("message", ["message_id": "q3", "text": "Legacy", "state": "queued"])
        emit("message", ["message_id": "q3", "state": "applied"])
        precondition(model.controls.queue.isEmpty)
        emit("model_status", ["stage": "api_wait", "text": "Waiting", "attempt": 1, "max_attempts": 3, "timeout_seconds": 60, "attempt_id": "attempt-a"])
        emit("reasoning_delta", ["text": "old", "start": true, "attempt_id": "attempt-a"])
        emit("code_start", ["text": "old", "block_index": 0, "step": 1, "attempt_id": "attempt-a"])
        emit("model_status", ["stage": "api_retry", "text": "Retrying", "attempt": 2, "max_attempts": 3, "timeout_seconds": 30, "attempt_id": "attempt-b"])
        emit("reasoning_delta", ["text": "stale", "attempt_id": "attempt-a"])
        emit("model_status", ["attempt": 1, "attempt_id": "unseen-old", "text": "Stale"])
        emit("model_status", ["attempt": 1, "text": "Missing attempt ID"])
        emit("code_reset", ["attempt_id": "attempt-a"])
        precondition(model.controls.reasoning.isEmpty && model.controls.code.isEmpty && model.controls.attempt == 2)
        model.preferences.showReasoning = false; model.preferences.showCode = false
        emit("code_reset", ["attempt_id": "attempt-b"])
        emit("reasoning_delta", ["text": "new", "start": true, "attempt_id": "attempt-b"])
        emit("reasoning_end", ["attempt_id": "attempt-b"])
        emit("reasoning_delta", ["text": "late", "attempt_id": "attempt-b"])
        emit("code_start", ["text": "", "block_index": 0, "step": 2, "attempt_id": "attempt-b"])
        emit("code_delta", ["text": "print(", "block_index": 0, "attempt_id": "attempt-b"])
        emit("code_end", ["text": "print(2)", "block_index": 0, "attempt_id": "attempt-b"])
        emit("code_delta", ["text": "late", "block_index": 0, "attempt_id": "attempt-b"])
        precondition(model.controls.reasoning == "new" && model.controls.code.first?.text == "print(2)")
        emit("code_start", ["text": "second", "block_index": 1, "step": 3, "attempt_id": "attempt-b"])
        emit("code_delta", ["text": " block", "block_index": 1, "attempt_id": "attempt-b"])
        emit("code_end", ["text": "", "block_index": 1, "attempt_id": "attempt-b"])
        emit("code_start", ["text": "Late restart", "block_index": 1, "attempt_id": "attempt-b"])
        precondition(model.controls.code.count == 2 && model.controls.code.last?.text == "second block" && model.controls.code.last?.complete == true)
        // 下一步新请求从 attempt 1 重新计数；不能被上一步的重试次数拦截，并清空旧预览。
        emit("model_status", ["stage": "waiting", "text": "正在等待 API 响应", "attempt": 1, "max_attempts": 3, "attempt_id": "step-3"])
        precondition(model.controls.attempt == 1 && model.controls.attemptID == "step-3" && model.controls.code.isEmpty)
        precondition(model.controls.reasoning.isEmpty && !model.controls.reasoningComplete)
        emit("reasoning_delta", ["text": "step 3", "start": true, "attempt_id": "step-3"])
        emit("code_start", ["text": "x", "block_index": 0, "step": 3, "attempt_id": "step-3"])
        precondition(model.controls.reasoning == "step 3" && model.controls.code.count == 1)
        emit("code_reset", ["attempt_id": "step-3"])
        precondition(model.controls.reasoning.isEmpty && model.controls.code.isEmpty, "code_reset 同时撤销推理与代码预览")
        emit("model_status", ["stage": "waiting", "attempt": 1, "attempt_id": "attempt-b"])
        precondition(model.controls.attemptID == "step-3", "已退役的 attempt 不能借新请求阶段回来")
        model.compactContext()
        let compactID = model.bridge.commands.last!["command_id"] as! String
        precondition(model.bridge.commands.last?["command"] as? String == "compaction")
        emit("progress", ["phase": "compaction", "step": 2])
        model.cancelCompaction()
        let rejectedCancelID = model.bridge.commands.last!["command_id"] as! String
        emit("control_result", ["command_id": rejectedCancelID, "status": "failed", "text": "Not cancelled"])
        precondition(model.controls.compacting && !model.controlPending("compaction_cancel"), "取消请求失败不能伪造压缩结束")
        model.cancelCompaction()
        let cancelID = model.bridge.commands.last!["command_id"] as! String
        precondition(model.bridge.commands.last?["command"] as? String == "compaction_cancel")
        emit("control_result", ["command_id": cancelID, "status": "ok", "text": "Cancelled"])
        emit("control_result", ["command_id": compactID, "status": "ok", "text": "Compacted", "budget_before": 100, "budget_after": 40])
        precondition(!model.controls.compacting && model.controls.budgetBefore == "100" && model.controls.budgetAfter == "40")
        emit("control_result", ["command_id": compactID, "status": "running", "text": "Late intermediate", "budget_after": 99])
        precondition(!model.controls.compacting && model.controls.budgetAfter == "40")
        emit("policy", ["foreground_allowed": true, "ask_user_enabled": false])
        model.interruptTurn()
        let interruptID = model.bridge.commands.last!["command_id"] as! String
        precondition(model.bridge.commands.last?["command"] as? String == "interrupt" && model.bridge.commands.last?["force"] as? Bool == false)
        model.interruptTurn(force: true)
        precondition(model.bridge.commands.last?["force"] as? Bool == true && model.controls.interruptPending)
        emit("control_result", ["command_id": interruptID, "status": "ok", "text": "Old confirmation"])
        precondition(model.controls.interruptPending, "旧的普通中止回执不能清除强制中止 pending")
        emit("interrupted", ["text": "Interrupted", "worker_reset": true])
        precondition(model.conversationOpen && model.phase == .stopped && model.controls.workerReset)
        precondition(model.controls.foregroundAllowed && !model.controls.askUserEnabled)
        emit("state", ["state": "running"])
        precondition(model.phase == .stopped)
        model.messageDraft = "Correction"; model.sendInput()
        precondition(model.bridge.commands.last?["command"] as? String == "message" && model.taskID == task)
        emit("turn_finished", ["state": "completed", "text": "Late result"])
        precondition(model.phase == .starting)
        model.refreshQueue()
        let oldQueueID = model.bridge.commands.last!["command_id"] as! String
        sequence = 0
        emit("turn_started", ["text": "Correction"], turn: 1)
        precondition(model.controls.sequence == 1 && !model.controlPending("queue_list") && model.controls.result.isEmpty)
        model.expireControl(oldQueueID)
        precondition(model.controls.result.isEmpty, "旧轮超时不能恢复旧等待文案")
        emit("skill_state", ["name": NSNull(), "source": "", "digest": "", "pending": false], turn: 1)
        let phase = model.phase
        for event in ["state", "question", "progress", "policy", "queue_state", "approval", "interrupted", "turn_finished", "finished"] {
            emit(event, ["state": "failed", "text": "Stale", "phase": "execution", "foreground_allowed": false])
        }
        precondition(model.phase == phase && model.question.isEmpty && model.controls.foregroundAllowed)
        precondition(!model.controls.skillPending && model.controls.effectiveSkill == nil)
        let currentSequence = model.controls.sequence!
        model.receive(["event": "policy", "task_id": task, "turn_index": 1, "sequence": currentSequence - 1, "foreground_allowed": false])
        precondition(model.controls.foregroundAllowed)
        model.bridge.failSend = true; model.setForegroundAllowed(false)
        precondition(model.controls.foregroundAllowed && !model.controlPending("set_policy"))
        model.bridge.failSend = false
        model.setAskUserEnabled(true)
        let timedPolicyID = model.bridge.commands.last!["command_id"] as! String
        let beforeTimeout = model.bridge.commands.count
        model.expireControl(timedPolicyID)
        precondition(!model.controlPending("set_policy") && !model.controls.askUserEnabled && !model.controls.result.isEmpty)
        precondition(model.bridge.commands.count == beforeTimeout, "超时不能自动重发或乐观修改策略")
        let presentation = AppDelegate(); presentation.obstructsOverlay = true; model.delegate = presentation
        emit("overlay", ["phase": "begin", "guard_id": "old-guard", "impact": "pointer"], turn: 1)
        precondition(model.compactHidesForComputerAction)
        sequence = 0
        emit("model_status", ["stage": "api_wait", "attempt": 1, "attempt_id": "new-round"], turn: 2)
        emit("reasoning_delta", ["text": "Before turn_started", "start": true, "attempt_id": "new-round"], turn: 2)
        emit("turn_started", ["text": "Queued follow-up"], turn: 2)
        precondition(model.controls.stage == "api_wait" && model.controls.reasoning == "Before turn_started")
        precondition(!model.compactHidesForComputerAction && !model.overlayPointerBusy)
        emit("overlay", ["phase": "begin", "guard_id": "new-guard", "impact": "pointer"], turn: 2)
        emit("overlay", ["phase": "end", "guard_id": "new-guard"], turn: 1)
        precondition(model.compactHidesForComputerAction, "旧轮 overlay end 不能清除新 guard")
        emit("overlay", ["phase": "reset"], turn: 2)
        precondition(!model.compactHidesForComputerAction)
        model.bridge.onExit?()
        precondition(!model.controls.foregroundAllowed && model.controls.askUserEnabled && !model.conversationOpen)
        model.receive(["event": "state", "task_id": task, "turn_index": 2, "state": "running"])
        precondition(!model.phase.active, "断开连接后旧 task 不能恢复活动状态")
        let fresh = AppModel(dataDirectory: directory.appendingPathComponent("fresh"))
        fresh.preferences = Preferences(); fresh.preferences.configFilePath = untrusted.path
        fresh.ready = true; fresh.bridge.acknowledge = false; fresh.setAskUserEnabled(false)
        fresh.instruction = "No Skill"; fresh.start()
        let freshConfig = fresh.bridge.commands.last!["config"] as! [String: Any]
        precondition(freshConfig["selected_skill"] == nil && freshConfig["selected_skill_override"] == nil && freshConfig["foreground_allowed"] as? Bool == false)
        precondition(freshConfig["ask_user_enabled"] as? Bool == false && !fresh.controls.askUserEnabled)
        print("会话控制契约覆盖：协议 1、空闲目录、本地与 pending Skill、策略安全默认值、独立审批、队列、中止/强制中止、压缩预算、turn/attempt/sequence 防护和断开重置。")
    }
    // 会话级契约：finished 终态、Skill 显式覆盖不跨会话、轮次之间的手动压缩、取消触发下一轮的消息。
    @MainActor static func sessionContracts(directory: URL, config: URL) throws {
        let model = AppModel(dataDirectory: directory)
        model.bridge.acknowledge = false
        model.preferences = Preferences(); model.preferences.configFilePath = config.path
        model.ready = true
        model.receive(["event": "skills_catalog", "sequence": 1, "skills": [["name": "alpha", "description": "A", "source": "bundled"]]])
        var sequence = 0
        func emit(_ kind: String, _ fields: [String: Any] = [:], turn: Int = 0) {
            sequence += 1
            var event: [String: Any] = ["event": kind, "task_id": model.taskID, "turn_index": turn, "sequence": sequence]
            for (key, value) in fields { event[key] = value }
            model.receive(event)
        }
        func startConfig(_ text: String) -> [String: Any] {
            model.instruction = text; model.start()
            precondition(model.bridge.commands.last?["command"] as? String == "start")
            return model.bridge.commands.last!["config"] as! [String: Any]
        }
        model.selectSkill("alpha")
        precondition(model.controls.skillOverride == .named("alpha") && model.bridge.commands.isEmpty)
        var started = startConfig("With alpha")
        precondition(started["selected_skill_override"] as? Bool == true && started["selected_skill"] as? String == "alpha")
        precondition(model.controls.skillOverride == .unspecified && model.controls.selectedSkill == "alpha" && model.controls.effectiveSkill == nil)
        emit("turn_started", ["text": "With alpha"])
        emit("skill_state", ["name": "alpha", "source": "bundled", "digest": "d1", "pending": false])
        precondition(model.controls.effectiveSkill == "alpha" && model.controls.skillDigest == "d1")
        emit("turn_finished", ["state": "completed", "text": "Done"])
        precondition(model.conversationOpen && model.phase == .completed && model.canCompact)
        // 空闲手动压缩：轮次已关闭也要处理压缩专属事件，但不改写会话阶段和本轮 attempt。
        model.compactContext()
        let compactID = model.bridge.commands.last!["command_id"] as! String
        emit("control_result", ["command_id": compactID, "status": "started", "text": "正在压缩上下文"])
        emit("progress", ["phase": "compaction", "step": 2, "manual": true])
        emit("model_status", ["stage": "waiting", "text": "上下文压缩：正在等待 API 响应", "attempt": 1, "attempt_id": "summary", "purpose": "compaction"])
        precondition(model.controls.compactionRunning && model.controls.compacting && model.controlPending("compaction"))
        precondition(model.phase == .completed && model.executionPhase.isEmpty && model.controls.attemptID != "summary")
        precondition(model.controls.stage == "waiting" && model.controls.stageText.hasPrefix("上下文压缩"))
        precondition(!model.canSwitchSession && !model.canCompact)
        emit("progress", ["phase": "planning", "step": 3])
        precondition(model.executionPhase.isEmpty && model.controls.compacting, "轮次关闭后普通 progress 仍被拒绝")
        emit("turn_started", ["text": "Queued"], turn: 1)
        precondition(model.controlPending("compaction"), "新轮不能丢弃会话压缩的等待")
        emit("control_result", ["command_id": compactID, "status": "completed", "text": "Compacted", "budget_before": 90, "budget_after": 30])
        precondition(!model.controlPending("compaction") && model.controls.budgetAfter == "30" && !model.controls.compacting,
                     "比当前轮旧的会话命令结果按 command_id 匹配")
        model.compactContext()
        let queuedID = model.bridge.commands.last!["command_id"] as! String
        emit("turn_finished", ["state": "completed", "text": "Done again"], turn: 1)
        precondition(model.controlPending("compaction") && !model.controls.compacting, "轮次结束不能删除会话压缩")
        emit("control_result", ["command_id": queuedID, "status": "started"], turn: 1)
        precondition(model.controls.compactionRunning)
        model.cancelCompaction()
        let cancelID = model.bridge.commands.last!["command_id"] as! String
        emit("control_result", ["command_id": cancelID, "status": "completed", "text": "控制已应用"], turn: 1)
        precondition(model.controls.compactionRunning, "取消回执不等于压缩已结束")
        emit("control_result", ["command_id": queuedID, "status": "cancelled", "text": "上下文压缩已取消"], turn: 1)
        precondition(!model.controls.compactionRunning && !model.controls.compacting && model.canSwitchSession)
        // 取消触发下一轮的消息：恢复发送前阶段与草稿。
        model.messageDraft = "Follow up"; model.sendInput()
        let messageID = model.bridge.commands.last!["message_id"] as! String
        precondition(model.phase == .starting && model.controls.awaitingNextTurn)
        emit("message", ["message_id": messageID, "state": "queued"], turn: 1)
        precondition(model.messageDraft.isEmpty)
        emit("message", ["message_id": messageID, "state": "cancelled"], turn: 1)
        precondition(model.phase == .completed && model.messageDraft == "Follow up" && !model.controls.awaitingNextTurn && !model.sendingMessage)
        // finished 是权威终态：等待下一轮时也要接受。
        model.messageDraft = "Another"; model.sendInput()
        precondition(model.controls.awaitingNextTurn)
        emit("finished", ["state": "completed", "text": "Session closed"], turn: 1)
        precondition(!model.conversationOpen && model.phase == .completed && model.answer == "Session closed")
        model.reset()
        precondition(model.controls.skillOverride == .unspecified && model.controls.selectedSkill == nil && model.controls.effectiveSkill == nil)
        started = startConfig("Default skill")
        precondition(started["selected_skill"] == nil && started["selected_skill_override"] == nil, "未显式选择时不能跨会话沿用旧 Skill")
        emit("turn_started", ["text": "Default skill"])
        emit("interrupted", ["text": "Interrupted", "worker_reset": true])
        precondition(model.phase == .stopped && model.conversationOpen && model.controls.interrupted)
        emit("finished", ["state": "stopped", "text": "任务已停止。"])
        precondition(!model.conversationOpen && model.phase == .stopped, "中止后的 finished 不能被丢弃")
        model.reset(); model.selectSkill(nil)
        precondition(model.controls.skillOverride == .noSkill)
        started = startConfig("No skill")
        precondition(started["selected_skill_override"] as? Bool == true && started["selected_skill"] is NSNull)
        emit("finished", ["state": "completed", "text": "Done"])
        let run = directory.appendingPathComponent("runs/history")
        try FileManager.default.createDirectory(at: run, withIntermediateDirectories: true)
        try Data("{}".utf8).write(to: run.appendingPathComponent("traj.json"))
        let record = TaskRecord(id: "history-skill", title: "History", state: "completed", answer: "Old", path: run.path)
        model.history.insert(record, at: 0)
        precondition(model.openConversation(record) && model.restoresHistory)
        precondition(model.controls.skillOverride == .unspecified && model.controls.selectedSkill == nil)
        started = startConfig("Resume history")
        precondition(started["selected_skill"] == nil && started["selected_skill_override"] == nil, "历史恢复未指定时交给 host 恢复")
        emit("finished", ["state": "completed", "text": "Done"])
        precondition(model.openConversation(record))
        model.selectSkill("alpha")
        started = startConfig("Resume with alpha")
        precondition(model.bridge.commands.last?["resume_path"] as? String == run.path)
        precondition(started["selected_skill_override"] as? Bool == true && started["selected_skill"] as? String == "alpha")
        print("会话级契约覆盖：finished 终态、Skill 三态覆盖与跨会话清理、历史恢复、轮次之间的手动压缩与取消、消息取消恢复。")
    }
    // 真实 AppModel 入口：flat v1、native alias、技术回执与恢复快照均只更新 UI。
    @MainActor static func envelopeActionContracts(directory: URL) {
        let model = AppModel(dataDirectory: directory)
        model.ready = true; model.bridge.acknowledge = false
        model.taskID = "native-transport"; model.conversationOpen = true; model.phase = .running
        var sequence = 0
        var turn = "z-opaque-turn"
        var index = 0
        func envelope(_ kind: String, _ fields: [String: Any] = [:]) -> [String: Any] {
            sequence += 1
            var event: [String: Any] = ["schema_version": 1, "event_id": "d01-\(sequence)", "sequence": sequence,
                "session_id": "session-d01", "stream_id": "stream-d01", "task_id": "harness-task", "native_task_id": model.taskID,
                "turn_id": turn, "turn_index": index, "step_id": "step-d01", "step": 1, "timestamp": "2026-10-21T00:00:00Z",
                "kind": kind, "event": kind, "origin": "sdk", "policy_revision": 7, "text": ""]
            for (key, value) in fields { event[key] = value }
            return event
        }
        func emit(_ kind: String, _ fields: [String: Any] = [:]) { model.receive(envelope(kind, fields)) }
        emit("session_info", ["model": "snapshot-model", "foreground_allowed": true, "ask_user_enabled": false])
        emit("turn_started", ["text": "本轮任务"])
        emit("skill_state", ["name": "single-skill", "digest": "digest", "pending": false])
        emit("intent", ["text": "当前动作意图"])
        precondition(model.controls.sessionID == "session-d01" && model.taskID == "native-transport")
        precondition(model.controls.canonicalTurnID == turn && model.controls.lastEnvelope["task_id"] == "harness-task")
        let sends = model.bridge.commands.count
        let turns = model.conversationTurns.count
        let messages = model.conversationMessages.count
        let history = model.history.count
        emit("action_status", ["action_id": "action-1", "stage": "waiting_resource", "sdk_session_id": "sdk-session",
            "resource_id": "desktop", "observation_id": "obs", "journal_sequence": 11, "record_sequence": 12, "cell_request_id": "cell-7",
            "model": "不能污染快照", "foreground_allowed": false, "ask_user_enabled": true])
        precondition(model.taskProgressTitle == L("工具排队中") && model.taskProgressIndeterminate)
        precondition(model.controls.stage.isEmpty && model.phase == .running && model.currentIntent == "当前动作意图")
        precondition(model.controls.foregroundAllowed && !model.controls.askUserEnabled && model.controls.model == "snapshot-model")
        precondition(model.controls.effectiveSkill == "single-skill")
        let action = model.controls.recentActions.last!
        precondition(action.metadata["sdk_session_id"] == "sdk-session" && action.metadata["cell_request_id"] == "cell-7")
        precondition(action.metadata["policy_revision"] == "7" && action.metadata["step_id"] == "step-d01")
        precondition(action.metadata["record_sequence"] == "12" && action.metadata["journal_sequence"] == "11" && action.metadata["stream_id"] == "stream-d01",
                     "诊断元数据保留 stream_id/record_sequence，且不丢旧 journal_sequence")
        precondition(model.controls.lastEnvelope["stream_id"] == "stream-d01" && model.controls.lastEnvelope["record_sequence"] == "12")
        for (stage, label) in [("intent_recorded", "工具意图已记录，尚未派发"), ("dispatching", "工具派发中"),
                               ("running", "工具执行中"), ("rejected", "动作被拒绝"),
                               ("cancelled_before_dispatch", "动作在派发前已取消"), ("verification_failed", "动作核验失败"),
                               ("verified", "动作已核验（不代表任务完成）"), ("outcome_unknown", "动作结果未知，请核实")] {
            emit("action_status", ["action_id": "action-1", "stage": stage])
            precondition(model.taskProgressTitle == L(label) && model.phase == .running)
            precondition(model.controls.recentActions.count == 1)
        }
        precondition(!model.taskProgressIndeterminate, "结果未知不能由动画假装正在执行")
        var receipt = envelope("action_status", ["event": "finished", "state": "completed", "action_id": "action-1",
            "stage": "receipt_received", "execution": ["status": "error", "code": 500], "effect": "unknown"])
        model.receive(receipt)
        precondition(model.phase == .running && model.taskProgressTitle == L("已收到技术回执，尚未核验"))
        precondition(model.controls.recentActions.last?.execution.contains("error") == true)
        precondition(model.controls.recentActions.last?.effect == "unknown" && model.controls.recentActions.last?.verification == "unverified")
        precondition(model.controls.recentActions.last?.metadata["sdk_session_id"] == "sdk-session" && model.controls.recentActions.last?.metadata["cell_request_id"] == "cell-7",
                     "只带 stage 的增量不能丢掉先前 SDK 诊断身份")
        precondition(!model.taskProgressIndeterminate && model.conversationTurns.count == turns && model.history.count == history)
        precondition(model.conversationMessages.count == messages && model.bridge.commands.count == sends)
        let receiptID = receipt["event_id"] as! String
        receipt["sequence"] = sequence + 100; receipt["stage"] = "running"
        model.receive(receipt)
        precondition(model.controls.envelopeSequence == sequence && model.controls.activeAction?.stage == "receipt_received",
                     "同 event_id 即使携带更大 sequence 也不能再处理")
        emit("action_status", ["event_id": "same-sequence", "sequence": sequence, "action_id": "action-1", "stage": "running"])
        precondition(model.controls.activeAction?.stage == "receipt_received", "相同 sequence 必须拒绝")
        emit("action_status", ["sequence": 1, "action_id": "action-1", "stage": "running"])
        precondition(model.controls.activeAction?.stage == "receipt_received", "迟到 sequence 必须拒绝")
        var unknown = envelope("state", ["schema_version": 2, "state": "completed"])
        model.sendingMessage = true
        let lastSequence = model.controls.envelopeSequence
        let lastMetadata = model.controls.lastEnvelope
        model.receive(unknown)
        precondition(!model.error.isEmpty && model.phase == .running && model.sendingMessage)
        precondition(model.controls.envelopeSequence == lastSequence && model.controls.lastEnvelope == lastMetadata)
        precondition(!model.controls.seenEventIDs.contains(unknown["event_id"] as! String) && model.bridge.commands.count == sends)
        unknown["schema_version"] = 1; unknown["state"] = "running"
        model.receive(unknown)
        precondition(model.controls.seenEventIDs.contains(unknown["event_id"] as! String), "拒绝 unknown version 不应消费身份")
        model.sendingMessage = false
        for version: Any in [true, "1", 1.5, 1.0, 0, NSNull()] {
            emit("policy", ["schema_version": version, "foreground_allowed": false])
            precondition(model.controls.foregroundAllowed)
        }
        model.error = ""
        var legacy = envelope("action_status", ["action_id": "legacy-action", "stage": "running"])
        legacy["schema_version"] = nil; legacy["event_id"] = nil; legacy["kind"] = nil
        model.receive(legacy)
        precondition(model.controls.activeAction?.id == "legacy-action", "缺 version 的 event alias 保持兼容")
        legacy = envelope("action_status", ["event": "finished", "state": "completed", "action_id": "legacy-canonical", "stage": "running"])
        // legacy 不做 v1 完整校验：缺 text/step/policy_revision/timestamp 仍保持既有兼容。
        legacy["schema_version"] = nil; legacy["text"] = nil; legacy["step"] = nil; legacy["policy_revision"] = nil; legacy["timestamp"] = nil
        model.receive(legacy)
        precondition(model.controls.activeAction?.id == "legacy-canonical" && model.phase == .running,
                     "无 version 的 structured envelope 也必须 kind 优先")
        model.receive(["event": "message", "task_id": model.taskID, "message_id": "legacy-correction", "kind": "correction", "state": "queued", "text": "旧消息类型"])
        precondition(model.controls.queue.last?.kind == "correction", "legacy payload kind 不能误作事件名")
        emit("action_status", ["action_id": "action-1", "stage": "outcome_unknown"])
        precondition(model.controls.activeAction?.execution.contains("error") == true && model.controls.activeAction?.effect == "unknown",
                     "stage 增量不能覆盖原始失败回执为成功或空结果")
        let beforeWrong = model.controls.recentActions
        var wrong = envelope("action_status", ["action_id": model.taskID, "cell_request_id": model.taskID, "stage": "running"])
        wrong["native_task_id"] = nil; wrong["task_id"] = "other-harness-task"
        model.receive(wrong)
        wrong["task_id"] = NSNull(); model.receive(wrong)
        wrong["task_id"] = model.taskID; wrong["native_task_id"] = "old-native"; model.receive(wrong)
        emit("action_status", ["session_id": "old-session", "action_id": "stale", "stage": "running"])
        precondition(model.controls.recentActions == beforeWrong, "action/cell ID 不得充当 transport task ID；旧 session 必须拒绝")
        emit("model_status", ["stage": "waiting", "text": "真实 API 等待", "attempt": 1, "attempt_id": "attempt-a"])
        precondition(model.controls.activeAction == nil && model.controls.recentActions == beforeWrong)
        emit("action_status", ["action_id": "action-2", "stage": "running", "attempt_id": "attempt-a"])
        precondition(model.taskProgressTitle == "真实 API 等待", "API 阶段不被工具状态覆盖")
        emit("progress", ["phase": "execution"])
        precondition(model.taskProgressTitle == L("工具执行中"))
        for phase in [TaskPhase.stopping, .pausing, .paused, .waiting] {
            model.phase = phase
            precondition(model.taskProgressTitle == phase.label, "停止/暂停/问题优先级高于动作")
        }
        model.phase = .running
        emit("approval", ["approval_id": "approval-d01", "text": "确认授权"])
        precondition(model.taskProgressTitle == L("等待前台审批"))
        emit("approval_closed", ["approval_id": "approval-d01"])
        emit("model_status", ["stage": "waiting", "text": "新 API 请求", "attempt": 2, "attempt_id": "attempt-b"])
        let beforeAttempt = model.controls.recentActions
        emit("action_status", ["action_id": "old-attempt", "stage": "running", "attempt_id": "attempt-a"])
        emit("action_status", ["action_id": "old-attempt-no-id", "stage": "running", "attempt": 1])
        precondition(model.controls.activeAction == nil && model.controls.recentActions == beforeAttempt && model.controls.attemptID == "attempt-b")
        emit("progress", ["phase": "execution"])
        emit("action_status", ["action_id": "action-2", "stage": "receipt_received", "execution": "failed", "effect": "none", "attempt_id": "attempt-b"])
        emit("turn_finished", ["state": "completed", "text": "仅业务终态结束轮次"])
        precondition(model.phase == .completed && model.controls.activeAction == nil)
        let recoveredSends = model.bridge.commands.count
        let recoveredTurns = model.conversationTurns.count
        let recoveredHistory = model.history.count
        let recovery = envelope("recovery_report", ["text": "请核查桌面，不要重发", "actions": [
            ["action_id": "r-unknown", "status": "outcome_unknown", "execution": "unknown"],
            ["action_id": "r-receipt", "status": "receipt_unverified", "effect": "unknown"],
            ["action_id": "r-rejected", "status": "rejected"], ["action_id": "r-cancelled", "status": "cancelled_before_dispatch"],
            ["action_id": "r-not-sent", "status": "not_dispatched"]]])
        model.receive(recovery)
        precondition(model.controls.recoveryUnknownCount == 1 && model.controls.recoveryUnverifiedCount == 1)
        precondition(model.recoveryNotice.contains("请核查桌面") && model.controls.recoveryActions.count == 5)
        precondition(model.phase == .completed && model.controls.activeAction == nil && !model.taskProgressIndeterminate)
        precondition(model.conversationTurns.count == recoveredTurns && model.history.count == recoveredHistory && model.bridge.commands.count == recoveredSends)
        let notice = model.recoveryNotice
        var oldRecovery = envelope("recovery_report", ["session_id": "old-session", "text": "旧恢复", "actions": []])
        model.receive(oldRecovery)
        oldRecovery["session_id"] = nil; model.receive(oldRecovery)
        // v1 缺 session_id 已被信封拒绝；legacy 形态仍须由恢复快照自身的 session 要求拒绝。
        oldRecovery["schema_version"] = nil; model.receive(oldRecovery)
        oldRecovery["schema_version"] = 1
        oldRecovery["session_id"] = "session-d01"; oldRecovery["native_task_id"] = "old-native"; model.receive(oldRecovery)
        precondition(model.recoveryNotice == notice && model.phase == .completed)
        emit("recovery_report", ["actions": (0..<70).map { ["action_id": "recovered-\($0)", "status": "outcome_unknown"] }])
        precondition(model.controls.recoveryActions.count == 64 && model.controls.recoveryUnknownCount == 70,
                     "恢复快照有界，但提示数量必须包括全部未知动作")
        precondition(model.phase == .completed && model.bridge.commands.count == recoveredSends)
        turn = "a-opaque-turn"; index = 1
        emit("turn_started", ["text": "新轮次"])
        emit("model_status", ["stage": "waiting", "text": "新轮请求", "attempt": 1, "attempt_id": "new-turn-attempt"])
        let newTurnSequence = model.controls.envelopeSequence
        let newTurnHistory = model.controls.recentActions
        emit("action_status", ["turn_id": "z-opaque-turn", "turn_index": 0, "action_id": "late-turn", "stage": "running"])
        emit("action_status", ["turn_id": "unseen-older", "turn_index": 0, "action_id": "late-numeric", "stage": "running"])
        emit("action_status", ["turn_id": "future-turn", "turn_index": 99, "action_id": "not-a-turn-start", "stage": "running"])
        emit("action_status", ["attempt_id": "attempt-b", "action_id": "late-attempt", "stage": "running"])
        precondition(model.controls.canonicalTurnID == turn && model.controls.turnIndex == index)
        precondition(model.controls.envelopeSequence == newTurnSequence && model.controls.recentActions == newTurnHistory && model.controls.activeAction == nil)
        precondition(model.taskProgressTitle == "新轮请求" && model.phase == .running)
        emit("progress", ["phase": "execution"])
        for number in 0..<300 { emit("action_status", ["action_id": "bounded-\(number)", "stage": "receipt_received"]) }
        precondition(model.controls.recentActions.count == 64 && model.controls.recentActions.first?.id == "bounded-236")
        precondition(model.controls.seenEventIDs.count == 256 && model.controls.seenEventOrder.count == 256)
        precondition(!model.controls.seenEventIDs.contains(receiptID))
        let boundedSequence = model.controls.envelopeSequence
        model.receive(receipt.merging(["sequence": 1]) { _, value in value })
        precondition(model.controls.envelopeSequence == boundedSequence, "event ID 淘汰后仍有 sequence 防迟到")
        model.resetSessionControls(); model.taskID = "new-native"
        precondition(model.controls.seenEventIDs.isEmpty && model.controls.envelopeSequence == nil && model.controls.recentActions.isEmpty)
        precondition(model.recoveryNotice.isEmpty && model.controls.canonicalTurnID == nil && model.controls.envelopeSessionID.isEmpty)
        emit("turn_started", ["event_id": receiptID, "sequence": 1, "session_id": "new-session", "turn_index": 0])
        precondition(model.controls.seenEventIDs.contains(receiptID), "新 task 必须清除旧去重缓存")
        let opaque = AppModel(dataDirectory: directory.appendingPathComponent("opaque"))
        opaque.taskID = "opaque-native"; opaque.phase = .running
        func canonical(_ kind: String, _ id: String, _ sequence: Int, _ fields: [String: Any] = [:]) {
            var event: [String: Any] = ["schema_version": 1, "kind": kind, "event_id": "opaque-\(sequence)", "sequence": sequence,
                "session_id": "opaque-session", "stream_id": "opaque-stream", "native_task_id": "opaque-native", "task_id": "harness-opaque",
                "turn_id": id, "step_id": NSNull(), "step": NSNull(), "timestamp": "2026-10-21T00:00:00Z", "origin": "harness",
                "policy_revision": 0, "text": ""]
            for (key, value) in fields { event[key] = value }
            opaque.receive(event)
        }
        canonical("turn_started", "z", 1)
        canonical("turn_started", "a", 2)
        canonical("action_status", "z", 3, ["action_id": "late-z", "stage": "running"])
        precondition(opaque.controls.canonicalTurnID == "a" && opaque.controls.turnIndex == 1 && opaque.controls.recentActions.isEmpty)
        canonical("action_status", "a", 4, ["action_id": "actual-a", "stage": "running"])
        precondition(opaque.controls.activeAction?.id == "actual-a", "canonical turn_id 不按字典序判断先后")
        canonical("model_status", "a", 5, ["stage": "waiting", "attempt_id": "opaque-a"])
        canonical("turn_started", "b", 6)
        canonical("action_status", "b", 7, ["action_id": "old-attempt-gap", "stage": "running", "attempt_id": "opaque-a"])
        precondition(opaque.controls.activeAction == nil && opaque.controls.attemptID == nil && opaque.controls.envelopeSequence == 6,
                     "新轮模型请求尚未到达时，旧 attempt 也不能恢复动作状态")
        canonical("model_status", "c", 8, ["stage": "waiting", "attempt_id": "opaque-c"])
        canonical("intent_delta", "c", 9, ["text": "轮次启动前的预览", "attempt_id": "opaque-c"])
        canonical("turn_started", "c", 10)
        canonical("turn_started", "c", 11)
        precondition(opaque.controls.envelopeSequence == 10, "同一 canonical turn 的重复启动也须拒绝")
        precondition(opaque.controls.turnIndex == 3 && opaque.currentIntent == "轮次启动前的预览",
                     "canonical 预先到达的模型快照不能被 turn_started 重置两次")
        print("D01 路由契约通过：canonical/native 身份、kind 优先、legacy alias、版本拒绝、去重/迟到、有界动作、回执非业务完成、恢复只读、旧 turn/attempt 与 API/审批优先级。")
    }
    // 真实 AppModel 入口：带 schema_version 的畸形 flat v1 帧整帧忽略，不改变授权/phase/sequence/缓存，也不发送请求。
    @MainActor static func envelopeValidationContracts(directory: URL) throws {
        let model = AppModel(dataDirectory: directory)
        model.ready = true; model.bridge.acknowledge = false
        model.taskID = "strict-native"; model.conversationOpen = true; model.phase = .running
        var sequence = 0
        // 与 Python EventClock.make 同形：无 event alias，step/step_id 可为 null。
        func frame(_ kind: String, _ fields: [String: Any] = [:]) -> [String: Any] {
            sequence += 1
            var event: [String: Any] = ["schema_version": 1, "event_id": "strict-\(sequence)", "sequence": sequence,
                "session_id": "strict-session", "stream_id": "strict-stream", "task_id": "harness-strict", "native_task_id": model.taskID,
                "turn_id": "strict-turn", "turn_index": 0, "step_id": NSNull(), "step": NSNull(), "timestamp": "2026-10-21T00:00:00Z",
                "kind": kind, "origin": "harness", "policy_revision": 0, "text": ""]
            for (key, value) in fields { event[key] = value }
            return event
        }
        model.receive(frame("turn_started", ["text": "严格信封"]))
        model.receive(frame("policy", ["foreground_allowed": false, "ask_user_enabled": true]))
        model.receive(frame("action_status", ["action_id": "strict-action", "stage": "running", "step": 0, "step_id": "strict-session/step/0"]))
        precondition(model.controls.activeAction?.id == "strict-action" && model.controls.turnIndex == 0 && model.error.isEmpty,
                     "完整生产形状的 v1 帧必须接受")
        precondition(model.controls.lastEnvelope["stream_id"] == "strict-stream" && model.controls.envelopeSequence == sequence)
        struct Snapshot: Equatable {
            let foreground: Bool, askUser: Bool, approvals: [SessionApproval], closedApprovals: Set<String>
            let phase: TaskPhase, sending: Bool, envelopeSequence: Int?, turnSequence: Int?, turnIndex: Int, turnID: String?
            let seen: Set<String>, seenOrder: [String], lastEnvelope: [String: String], actions: [NativeActionStatus], activeAction: String?
            let skills: [SkillEntry], catalogSequence: Int?, session: String, commands: Int, turns: Int
            @MainActor init(_ model: AppModel) {
                let c = model.controls
                foreground = c.foregroundAllowed; askUser = c.askUserEnabled; approvals = c.approvals; closedApprovals = c.closedApprovals
                phase = model.phase; sending = model.sendingMessage; envelopeSequence = c.envelopeSequence; turnSequence = c.sequence
                turnIndex = c.turnIndex; turnID = c.canonicalTurnID; seen = c.seenEventIDs; seenOrder = c.seenEventOrder
                lastEnvelope = c.lastEnvelope; actions = c.recentActions; activeAction = c.activeActionID
                skills = c.skills; catalogSequence = c.catalogSequence; session = c.envelopeSessionID
                commands = model.bridge.commands.count; turns = model.conversationTurns.count
            }
        }
        // 每个探针若被接受都会改变可见状态；最后逐一用合法帧做正向对照。
        let probes: [(String, [String: Any])] = [
            ("policy", ["foreground_allowed": true, "ask_user_enabled": false]),
            ("approval", ["approval_id": "strict-approval", "text": "伪造授权"]),
            ("action_status", ["action_id": "strict-forged", "stage": "verified"]),
            ("skills_catalog", ["native_task_id": NSNull(), "task_id": NSNull(), "skills": [["name": "forged-skill"]]]),
            ("state", ["state": "completed"]),
            ("turn_started", ["turn_id": "strict-next", "turn_index": 1, "text": "伪造新轮"]),
        ]
        var mutations: [(String, (inout [String: Any]) -> Void)] = [
            ("schema_version=true", { $0["schema_version"] = true }), ("schema_version=\"1\"", { $0["schema_version"] = "1" }),
            ("schema_version=1.0", { $0["schema_version"] = 1.0 }), ("schema_version=2", { $0["schema_version"] = 2 }),
            ("schema_version=0", { $0["schema_version"] = 0 }), ("schema_version=null", { $0["schema_version"] = NSNull() }),
            ("sequence=0", { $0["sequence"] = 0 }), ("sequence=-1", { $0["sequence"] = -1 }), ("sequence=true", { $0["sequence"] = true }),
            ("sequence=float", { $0["sequence"] = Double($0["sequence"] as! Int) }), ("sequence=string", { $0["sequence"] = String($0["sequence"] as! Int) }),
            ("policy_revision=-1", { $0["policy_revision"] = -1 }), ("policy_revision=false", { $0["policy_revision"] = false }),
            ("policy_revision=0.0", { $0["policy_revision"] = 0.0 }), ("policy_revision=\"0\"", { $0["policy_revision"] = "0" }),
            ("text=null", { $0["text"] = NSNull() }), ("text=1", { $0["text"] = 1 }),
            ("step=-1", { $0["step"] = -1 }), ("step=true", { $0["step"] = true }), ("step=1.0", { $0["step"] = 1.0 }), ("step=\"1\"", { $0["step"] = "1" }),
            ("stream_id=\"\"", { $0["stream_id"] = "" }), ("stream_id=null", { $0["stream_id"] = NSNull() }), ("stream_id=1", { $0["stream_id"] = 1 }),
            // v1 帧不能退回 event alias 取事件名。
            ("kind missing with event alias", { $0["event"] = $0["kind"]; $0["kind"] = nil }),
        ]
        for key in ["sequence", "policy_revision", "text", "step", "task_id", "turn_id", "step_id"] {
            mutations.append(("\(key) missing", { $0[key] = nil }))
        }
        for key in ["sequence", "policy_revision"] { mutations.append(("\(key)=null", { $0[key] = NSNull() })) }
        for key in ["event_id", "session_id", "kind", "origin", "timestamp"] {
            mutations += [("\(key) missing", { $0[key] = nil }), ("\(key)=\"\"", { $0[key] = "" }),
                          ("\(key)=1", { $0[key] = 1 }), ("\(key)=null", { $0[key] = NSNull() })]
        }
        for key in ["task_id", "turn_id", "step_id"] {
            mutations += [("\(key)=1", { $0[key] = 1 }), ("\(key)=false", { $0[key] = false })]
        }
        // JSON 解码后的 NSNumber 同样区分整数、浮点与 bool。
        func decoded(_ event: [String: Any], replacing token: String = "", with value: String = "") throws -> [String: Any] {
            var text = String(data: try JSONSerialization.data(withJSONObject: event, options: [.sortedKeys]), encoding: .utf8)!
            if !token.isEmpty {
                precondition(text.contains(token), "JSON fixture 未命中 \(token)")
                text = text.replacingOccurrences(of: token, with: value)
            }
            return try JSONSerialization.jsonObject(with: Data(text.utf8)) as! [String: Any]
        }
        let baseline = Snapshot(model)
        var rejected = 0
        for (kind, fields) in probes {
            for (label, mutate) in mutations {
                var event = frame(kind, fields); mutate(&event)
                model.error = ""
                model.receive(event)
                precondition(!model.error.isEmpty && Snapshot(model) == baseline, "畸形 v1 帧必须整帧忽略：\(kind) \(label)")
                rejected += 1
            }
            let json = try decoded(frame(kind, fields))
            let number = json["sequence"] as! Int
            for (token, value) in [("\"schema_version\":1,", "\"schema_version\":1.0,"), ("\"schema_version\":1,", "\"schema_version\":1e0,"),
                                   ("\"schema_version\":1,", "\"schema_version\":true,"), ("\"sequence\":\(number),", "\"sequence\":\(number).0,"),
                                   ("\"policy_revision\":0,", "\"policy_revision\":0.0,"), ("\"policy_revision\":0,", "\"policy_revision\":false,"),
                                   ("\"step\":null,", "\"step\":0.0,"), ("\"step\":null,", "\"step\":true,")] {
                model.error = ""
                model.receive(try decoded(json, replacing: token, with: value))
                precondition(!model.error.isEmpty && Snapshot(model) == baseline, "JSON 浮点/bool 必须拒绝：\(kind) \(value)")
                rejected += 1
            }
        }
        precondition(model.controls.foregroundAllowed == false && model.controls.approvals.isEmpty && model.phase == .running)
        model.error = ""
        // 正向对照：同一探针的合法帧（含 JSON 解码 NSNumber）确实会生效，证明上面是信封校验而非其他路由拦截。
        model.receive(try decoded(frame("policy", probes[0].1)))
        precondition(model.controls.foregroundAllowed && !model.controls.askUserEnabled && model.error.isEmpty)
        model.receive(frame("approval", probes[1].1))
        precondition(model.controls.approvals.map(\.id) == ["strict-approval"])
        model.receive(try decoded(frame("action_status", probes[2].1.merging(["step": 0, "step_id": "strict-session/step/0"]) { _, value in value })))
        precondition(model.controls.activeAction?.id == "strict-forged" && model.controls.activeAction?.metadata["stream_id"] == "strict-stream")
        model.receive(frame("skills_catalog", probes[3].1))
        precondition(model.controls.skills.map(\.name) == ["forged-skill"] && model.controls.catalogSequence == sequence)
        model.receive(frame("state", probes[4].1))
        precondition(model.phase == .completed)
        model.receive(frame("turn_started", probes[5].1))
        precondition(model.controls.turnIndex == 1 && model.controls.canonicalTurnID == "strict-next" && model.phase == .running)
        precondition(model.controls.envelopeSequence == sequence && model.error.isEmpty && model.bridge.commands.count == baseline.commands)
        print("D01 严格信封契约通过：\(rejected) 个畸形 v1 帧（含 JSON 1.0/bool）未改变授权/phase/sequence/缓存且未发请求；合法对照帧生效。")
    }
    @MainActor static func previewContracts(directory: URL) {
        let model = AppModel(dataDirectory: directory)
        model.ready = true; model.taskID = "preview"; model.conversationOpen = true; model.phase = .running
        model.bridge.acknowledge = false
        func emit(_ kind: String, _ fields: [String: Any] = [:]) {
            var event: [String: Any] = ["event": kind, "task_id": "preview", "turn_index": 0, "attempt_id": "preview-a"]
            for (key, value) in fields { event[key] = value }
            model.receive(event)
        }
        let limit = SessionControlsState.previewCharacterLimit
        let large = String(repeating: "中", count: limit + 1)
        let commands = model.bridge.commands.count
        emit("model_status", ["attempt": 1, "stage": "receiving"])
        emit("reasoning_delta", ["text": large, "start": true])
        emit("reasoning_delta", ["text": "继续"])
        emit("reasoning_end")
        precondition(model.controls.reasoning.count == limit && model.controls.reasoningTruncated && model.controls.reasoningComplete)
        emit("code_start", ["block_index": 0, "text": large, "step": 1])
        emit("code_end", ["block_index": 0, "text": large + "末尾"])
        precondition(model.controls.code.first?.text.count == limit && model.controls.code.first?.complete == true && model.controls.codeTruncated)
        for index in 1...SessionControlsState.previewBlockLimit {
            emit("code_start", ["block_index": index, "text": "print(1)", "step": index + 1])
            emit("code_end", ["block_index": index])
        }
        precondition(model.controls.code.count == SessionControlsState.previewBlockLimit && model.controls.codeTruncated)
        precondition(model.bridge.commands.count == commands, "裁剪界面预览不能改变执行或发送控制指令")
        model.receive(["event": "model_status", "task_id": "preview", "turn_index": 0, "attempt_id": "preview-b", "attempt": 2])
        precondition(model.controls.reasoning.isEmpty && model.controls.code.isEmpty && !model.controls.reasoningTruncated && !model.controls.codeTruncated)
        model.controls.reasoningTruncated = true; model.controls.codeTruncated = true
        model.controls.beginTurn(1)
        precondition(!model.controls.reasoningTruncated && !model.controls.codeTruncated && !model.controls.reasoningComplete)
        model.phase = .completed; model.controls.compacting = true
        precondition(!model.canSwitchSession, "空闲压缩未结束时不能切换会话")
        model.controls.compacting = false
        precondition(model.canSwitchSession)
        print("执行预览契约覆盖：推理与代码字符上限、代码块上限、Unicode、重试/新轮重置、空闲压缩切换保护；不改变执行。")
    }
}
