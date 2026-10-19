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
                onEvent?(["event": "command_ack", "command_id": id, "task_id": command["task_id"]!])
            }
        }
    }
}
@MainActor final class AppDelegate {
    var onRefresh: (() -> Void)?
    func showSettings() {}
    func collapse() { onRefresh?() }
    func expand() {}
    func languageChanged() {}
    var obstructsOverlay = false
    func overlayWouldObstruct(_ event: [String: Any]) -> Bool { obstructsOverlay }
    func refreshCompact() { onRefresh?() }
    func runtimeExited() {}
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
    @MainActor static func main() async throws {
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
        print("Mini window routing passed: visible across execution and captures, hidden only for guarded overlaps, synchronized acknowledgement, stale fences and reset recovery.")
        print("Historical chat contracts passed: read-only selection, resume request, stable history ID, fresh execution fence, switch acknowledgement, active-task protection, drafts and app restart.")
        print("Conversation contracts passed: text and voice follow-ups, bubble ordering, deduplication, persisted history, legacy decoding and fresh chat fencing.")
        print("Compact result contracts passed: completed, failed, stopped, full text, voice, stale task fence, fallback and reset.")
        print("Voice router contracts passed: queue receipts, stale task fence, question reply, stop, session survival, disconnect uncertainty.")
    }
}
