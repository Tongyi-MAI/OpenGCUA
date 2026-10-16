import AppKit
import SwiftUI

// Fixture transport exercises the real AppModel routing without keys, network, or desktop actions.
@MainActor final class RuntimeBridge {
    var onEvent: (([String: Any]) -> Void)?
    var onExit: (() -> Void)?
    var running = true
    var commands: [[String: Any]] = []
    var acknowledge = true
    func launch(dataDirectory: URL) throws {}
    func shutdown() {}
    func send(_ command: [String: Any]) throws {
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
    func showSettings() {}
    func collapse() {}
    func refreshCompact() {}
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
        print("Compact result contracts passed: completed, failed, stopped, full text, voice, stale task fence, fallback and reset.")
        print("Voice router contracts passed: queue receipts, stale task fence, question reply, stop, session survival, disconnect uncertainty.")
    }
}
