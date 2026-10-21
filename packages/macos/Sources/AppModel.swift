import AppKit
import SwiftUI

struct TaskRecord: Codable, Identifiable {
    var id: String
    var title: String
    var date = Date()
    var state: String
    var answer: String
    var path: String
    var turns: [ConversationTurn]?
    var messages: [ConversationMessage]?
    var draft: String?
}

struct ConversationTurn: Codable, Identifiable {
    var id: String
    var instruction: String
    var answer: String
}

struct ConversationMessage: Codable, Identifiable {
    var id: String
    var text: String
    var isUser: Bool
}

enum TaskPhase: String {
    case idle, starting, running, pausing, paused, waiting, stopping, completed, stopped, failed
    var active: Bool { [.starting, .running, .pausing, .paused, .waiting, .stopping].contains(self) }
    var label: String {
        switch self {
        case .idle: L("准备就绪")
        case .starting: L("正在准备任务")
        case .running: L("正在执行")
        case .pausing: L("正在暂停，等待当前动作结束")
        case .paused: L("已暂停，随时继续")
        case .waiting: L("需要你的回复")
        case .stopping: L("正在停止任务")
        case .completed: L("任务已完成")
        case .stopped: L("任务已停止")
        case .failed: L("任务未完成")
        }
    }
    var symbol: String {
        switch self {
        case .idle: "circle.dotted"
        case .starting, .running, .pausing, .stopping: "arrow.triangle.2.circlepath"
        case .paused: "pause.circle.fill"
        case .waiting: "bubble.left.and.bubble.right.fill"
        case .completed: "checkmark.circle.fill"
        case .stopped: "stop.circle"
        case .failed: "exclamationmark.circle.fill"
        }
    }
}

@MainActor final class AppModel: ObservableObject {
    @Published var preferences = Preferences.load()
    @Published var controls = SessionControlsState()
    @Published var instruction = ""
    @Published var phase: TaskPhase = .idle
    @Published var ready = false
    @Published var disconnected = false
    @Published var intent = ""
    @Published var step = 0
    @Published var executionPhase = ""
    @Published var recentAction = ""
    private var overlayInteractions: Set<String> = []
    private var overlayOcclusions: Set<String> = []
    var overlayPointerBusy: Bool { !overlayInteractions.isEmpty }
    @Published var question = ""
    @Published var reply = ""
    @Published var answer = ""
    @Published var conversationOpen = false
    @Published var conversationTurns: [ConversationTurn] = []
    @Published var conversationMessages: [ConversationMessage] = []
    @Published var currentTurnInstruction = ""
    @Published var error = ""
    @Published var globalShortcutStatus = L("⌃⌥Space · 呼出 / 收起")
    @Published var history: [TaskRecord] = []
    @Published var selectedRecord: TaskRecord?
    @Published var recording = false
    @Published var speechPending = false
    @Published var speechStatus = ""
    @Published var messageDraft = ""
    @Published var messageStatus = ""
    @Published var sendingMessage = false
    private var sentMessages: [String: String] = [:]
    private var sentMessageOrder: [String] = []
    private var awaitingMessageID = ""
    private var followUpPreviousPhase: TaskPhase?
    private var resetCommandID = ""
    private var pendingHistoryRecord: TaskRecord?
    private var conversationID = ""
    private var resumeRunPath = ""
    private var newConversationDraft = ""
    var currentConversationID: String { conversationID.isEmpty ? taskID : conversationID }
    var restoresHistory: Bool { !resumeRunPath.isEmpty && !conversationOpen }
    @Published var voiceMode = false
    @Published var voiceLevel = 0.0
    @Published var audioInputs: [AudioInputDevice] = []
    @Published var systemInputName = L("无可用设备")
    @Published var activeInputName = ""
    @Published var inputDB = -96.0
    @Published var hasInputFrames = false
    private var activeInputUID = ""
    private var inputMonitor: Timer?
    @Published var voiceOutputLevel = 0.0
    @Published var voiceSpeaking = false
    @Published var voiceCaption = ""
    private var voiceReceipts: [String: CheckedContinuation<[String: Any], Never>] = [:]
    private var voiceReceiptTimeouts: [String: Task<Void, Never>] = [:]
    private var speechGeneration = UUID()
    @Published var accessibilityGranted = false
    @Published var screenRecordingGranted = false
    var taskID = ""
    var questionID = ""
    var runPath = ""
    var taskTitle = ""
    let bridge = RuntimeBridge()
    let speech = SpeechInput()
    let qwenSpeech = QwenSpeechInput()
    weak var delegate: AppDelegate?
    let dataDirectory: URL

    init(dataDirectory: URL? = nil) {
        self.dataDirectory = dataDirectory ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Generalist", isDirectory: true)
        AppLanguage.shared.select(preferences.language)
        do {
            try FileManager.default.createDirectory(at: self.dataDirectory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: self.dataDirectory.path)
            let path = self.dataDirectory.appendingPathComponent("history.json")
            if let data = try? Data(contentsOf: path), let records = try? JSONDecoder().decode([TaskRecord].self, from: data) {
                history = records.map { record in
                    var r = record
                    if TaskPhase(rawValue: r.state)?.active == true { r.state = "stopped"; r.answer = L("上次运行意外中断，请确认操作结果后重新发起任务。") }
                    return r
                }
            }
        } catch { self.error = error.localizedDescription }
        bridge.onEvent = { [weak self] in self?.receive($0) }
        bridge.onExit = { [weak self] in
            guard let self else { return }
            self.ready = false
            self.disconnected = true
            self.error = L("运行组件已退出，可以重新连接。")
            if self.phase.active { self.finish(.failed, text: L("运行组件意外退出。部分操作可能已生效，请核实后再试。")) }
            self.conversationOpen = false
            self.resetSessionControls(clearCatalog: true)
            self.taskID = ""
            self.resetCommandID = ""; self.pendingHistoryRecord = nil; self.sendingMessage = false
            self.delegate?.runtimeExited()
        }
        qwenSpeech.context = { [weak self] in VoiceTaskContext(taskID: self?.taskID ?? "", questionID: self?.questionID ?? "", wasActive: self?.phase.active ?? false) }
        qwenSpeech.onTool = { [weak self] call, context in
            guard let self else { return ["accepted": false, "reason": L("应用已关闭")] }
            return await self.handleVoiceTool(call, context: context)
        }
        refreshAudioInputs()
        inputMonitor = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in if self?.voiceMode == true { self?.refreshAudioInputs() } }
        }
        qwenSpeech.onInputDevice = { [weak self] device in self?.activeInputName = device.name; self?.activeInputUID = device.uid }
        qwenSpeech.onInputMeter = { [weak self] meter in self?.inputDB = meter.db; self?.hasInputFrames = true }
        qwenSpeech.onLevel = { [weak self] in self?.voiceLevel = $0 }
        qwenSpeech.onOutputLevel = { [weak self] in self?.voiceOutputLevel = $0 }
        qwenSpeech.onSpeaking = { [weak self] in self?.voiceSpeaking = $0 }
        qwenSpeech.onCaption = { [weak self] in self?.voiceCaption = $0 }
        qwenSpeech.onState = { [weak self] in self?.recording = $0 }
        qwenSpeech.onStatus = { [weak self] text in
            self?.speechStatus = text
            self?.speechPending = [L("正在请求麦克风…"), L("正在连接 Qwen…"), L("正在完成识别…")].contains(text)
        }
        qwenSpeech.onError = { [weak self] in self?.error = $0; self?.speechStatus = L("连接已断开"); self?.speechPending = false }
        speech.onState = { [weak self] in self?.recording = $0 }
        speech.onError = { [weak self] in self?.error = $0 }
    }
    deinit { inputMonitor?.invalidate() }
    func setLanguage(_ selection: String) {
        guard AppLanguage.choices.contains(selection), preferences.language != selection else { return }
        AppLanguage.shared.select(selection)
        preferences.language = selection
        preferences.save()
        delegate?.languageChanged()
    }
    var colorScheme: ColorScheme? { preferences.appearance == "浅色" ? .light : preferences.appearance == "深色" ? .dark : nil }
    var status: String { phase == .running && !intent.isEmpty ? intent : phase.label }
    var tint: Color { phase == .failed || phase == .waiting ? .orange : phase == .completed ? .green : .accentColor }
    var showsVoiceTaskProgress: Bool { phase != .idle }
    var taskProgressTitle: String {
        if disconnected { return L("连接已断开") }
        if [.stopping, .pausing, .paused, .waiting].contains(phase) { return phase.label }
        if controls.compactionRunning { return L("正在整理上下文") }
        if controls.interrupted { return L("本轮已中止") }
        if controls.interruptPending { return L("正在中止本轮…") }
        if !controls.approvals.isEmpty { return L("等待前台审批") }
        if !controls.stageText.isEmpty && phase == .running { return controls.stageText }
        guard phase == .running else { return phase.label }
        if !apiProgressTitle.isEmpty { return apiProgressTitle }
        if let action = controls.activeAction { return action.label }
        switch executionPhase {
        case "planning": return L("思考中")
        case "execution": return L("正在操作")
        case "postprocess": return L("正在检查结果")
        case "compaction": return L("正在整理上下文")
        case "recovery": return L("正在恢复执行")
        case "user_wait": return L("等待你的回复")
        case "complete": return L("正在继续任务")
        default: return L("正在准备")
        }
    }
    private var apiProgressTitle: String {
        switch controls.stage {
        case "waiting", "api_wait": return L("正在等待 API 响应")
        case "generating", "receiving": return L("API 模型正在生成")
        case "retrying", "api_retry": return L("API 模型正在重试")
        case "error": return L("API 模型请求失败")
        case "cancelled": return L("API 模型请求已取消")
        default: return ""
        }
    }
    var displayedAction: NativeActionStatus? {
        guard phase == .running, !disconnected, !controls.compactionRunning,
              !controls.interrupted, !controls.interruptPending, controls.approvals.isEmpty,
              controls.stageText.isEmpty, apiProgressTitle.isEmpty else { return nil }
        return controls.activeAction
    }
    var taskProgressIndeterminate: Bool {
        if let action = displayedAction { return action.progressing }
        return !disconnected && [.starting, .running, .pausing, .stopping].contains(phase)
    }
    var taskProgressSymbol: String { displayedAction?.symbol ?? phase.symbol }
    var recoveryNotice: String {
        [controls.recoverySummary, controls.recoveryText].filter { !$0.isEmpty }.joined(separator: "\n")
    }
    var currentIntent: String {
        let preview = controls.intentPreview.trimmingCharacters(in: .whitespacesAndNewlines)
        return preview.isEmpty ? intent : preview
    }
    var taskProgressDetail: String {
        if phase == .waiting { return question }
        if !phase.active { return answer.isEmpty ? taskTitle : answer }
        if !currentIntent.isEmpty { return currentIntent }
        if !recentAction.isEmpty { return L("最近动作：") + recentAction }
        return taskTitle
    }
    func connect() {
        guard !bridge.running else { return }
        resetSessionControls(clearCatalog: true)
        conversationOpen = false; ready = false; taskID = ""
        disconnected = false; error = ""
        do { try bridge.launch(dataDirectory: dataDirectory) }
        catch { self.error = error.localizedDescription; disconnected = true }
    }
    func start(commandID: String? = nil) {
        guard ready, !phase.active, !instruction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        refreshPermissions()
        guard accessibilityGranted && screenRecordingGranted else {
            error = L("请先为 OpenGCUA 开启辅助功能和屏幕录制权限。授权 CuaDriver 不会替代 OpenGCUA 的权限。")
            MacPermissions.showGuide(model: self)
            return
        }
        let skillOverride = controls.skillOverride
        do {
            var config = try preferences.taskConfiguration()
            let endpoint = config["llm_base_url"] as! String
            let fileKey = (config["api_key"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let key = fileKey.isEmpty ? try CredentialStore.read(endpoint) : fileKey
            guard !key.isEmpty || config["allow_empty_api_key"] as? Bool == true else { delegate?.showSettings(); error = L("请在配置文件或此服务对应的钥匙串中保存 API Key。"); return }
            config["api_key"] = key
            // 只有用户显式选择才覆盖；未指定时由 host 恢复历史 Skill（含摘要校验）或使用默认值。
            config["selected_skill"] = nil; config["selected_skill_override"] = nil
            if skillOverride != .unspecified {
                config["selected_skill_override"] = true
                config["selected_skill"] = skillOverride.name.map { $0 as Any } ?? NSNull()
            }
            config["ask_user_enabled"] = controls.askUserEnabled
            config["foreground_allowed"] = false
            let initialAskUser = controls.askUserEnabled
            resetSessionControls()
            controls.askUserEnabled = initialAskUser
            controls.selectedSkill = skillOverride.name
            controls.turnStartedAt = Date()
            if !voiceMode { stopSpeech() }
            let resuming = !resumeRunPath.isEmpty
            taskID = UUID().uuidString
            currentTurnInstruction = instruction.trimmingCharacters(in: .whitespacesAndNewlines)
            if !resuming {
                conversationID = taskID; taskTitle = currentTurnInstruction
                conversationTurns = []; conversationMessages = []; runPath = ""
            }
            conversationOpen = true
            appendChat(id: taskID + ":initial", text: currentTurnInstruction, isUser: true)
            question = ""; reply = ""; answer = ""; intent = ""; error = ""; step = 0
            executionPhase = ""; recentAction = ""
            overlayInteractions.removeAll(); overlayOcclusions.removeAll()
            phase = .starting
            messageStatus = ""; sentMessages = [:]; sentMessageOrder = []; sendingMessage = false; awaitingMessageID = ""
            var request: [String: Any] = ["command": "start", "task_id": taskID, "text": currentTurnInstruction,
                             "config": config]
            if resuming { request["resume_path"] = resumeRunPath }
            if let commandID { request["command_id"] = commandID }
            // Start in the compact presentation; each pointer operation guards its own footprint.
            delegate?.collapse(automatically: true)
            try bridge.send(request)
            persistCurrent()
        } catch {
            self.error = error.localizedDescription
            if phase == .starting { finish(.failed, text: error.localizedDescription) }
            // 未送达 host 的启动不能吞掉用户的显式 Skill 选择。
            controls.skillOverride = skillOverride; controls.selectedSkill = skillOverride.name
        }
    }
    func command(_ name: String) {
        let previousPhase = phase
        let previousExecutionPhase = executionPhase
        let wasSending = sendingMessage
        do {
            var command: [String: Any] = ["command": name, "task_id": taskID]
            if name == "reply" {
                guard !reply.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, !sendingMessage else { return }
                if !voiceMode { stopSpeech() }; command["question_id"] = questionID; command["text"] = reply
            }
            if name == "resume" { phase = .running; executionPhase = "" }
            if name == "reply" { sendingMessage = true }
            delegate?.refreshCompact()
            try bridge.send(command)
            if name == "stop" { phase = .stopping }
            if name == "pause" { phase = .pausing }
            delegate?.refreshCompact()
        } catch { phase = previousPhase; executionPhase = previousExecutionPhase; sendingMessage = wasSending; self.error = error.localizedDescription; delegate?.refreshCompact() }
    }
    func receive(_ incoming: [String: Any]) {
        let envelope = NativeEventEnvelope(fields: incoming)
        guard envelope.supported else {
            error = L("事件信封无效或版本不支持（schema_version：%@）；已忽略，不改变执行端状态。", NativeEventEnvelope.diagnostic(incoming["schema_version"]))
            delegate?.refreshCompact()
            return
        }
        let aliases = ["phase": "progress", "aborted": "interrupted", "session": "session_info"]
        let kind = aliases[envelope.kind] ?? envelope.kind
        var event = incoming
        if envelope.structured { event["kind"] = kind }
        event["event"] = kind
        if envelope.kind == "phase", let display = incoming["display_step"] { event["step"] = display }
        event["source_kind"] = envelope.kind
        // 前端别名只用于视图路由，source_kind 保留统一事件类型。
        if let id = NativeEventEnvelope.transportID(event) {
            guard !taskID.isEmpty, id == taskID else { return }
            let session = event["session_id"] as? String
            let currentSession = controls.envelopeSessionID.isEmpty ? controls.sessionID : controls.envelopeSessionID
            if let session, !currentSession.isEmpty, session != currentSession {
                guard kind == "session_info" else { return }
                controls.envelopeSessionID = session
            }
            if kind == "recovery_report" {
                guard let session, !session.isEmpty else { return }
                if !currentSession.isEmpty && session != currentSession { return }
            }
            // 记忆帧按 task/session 围栏与 generation 接收，不经过轮次终态过滤，也不改变轮次身份。
            if receiveMemoryFrame(event, kind: kind) { delegate?.refreshCompact(); return }
            let previousTurn = controls.turnIndex
            guard controls.accept(event) else { return }
            if let session, !session.isEmpty, controls.envelopeSessionID.isEmpty { controls.envelopeSessionID = session }
            if controls.turnIndex > previousTurn {
                overlayInteractions.removeAll(); overlayOcclusions.removeAll()
            }
        } else {
            guard ["skills_catalog", "memory_catalog", "control_result", "ready", "command_ack", "error"].contains(kind) else { return }
            if receiveMemoryFrame(event, kind: kind) { delegate?.refreshCompact(); return }
            guard controls.acceptDelivery(event) else { return }
            if kind == "skills_catalog" {
                receiveCatalog(event); delegate?.refreshCompact(); return
            }
            if kind == "control_result" {
                _ = receiveControlEvent(event); delegate?.refreshCompact(); return
            }
        }
        if NativeEventEnvelope.transportID(event) == nil, let id = event["command_id"] as? String,
           ["command_ack", "error"].contains(kind), controls.pending[id] == nil, id != resetCommandID { return }
        if ["command_ack", "error"].contains(kind), receiveControlAcknowledgement(event) {
            delegate?.refreshCompact(); return
        }
        if let id = event["command_id"] as? String, id == resetCommandID,
           kind == "command_ack" || kind == "error" {
            resetCommandID = ""; sendingMessage = false
            if kind == "command_ack" {
                if let record = pendingHistoryRecord { pendingHistoryRecord = nil; loadConversation(record) }
                else { clearConversation(restoreNewDraft: true) }
            }
            else { pendingHistoryRecord = nil; messageStatus = ""; error = event["text"] as? String ?? L("无法结束对话，请重试。") }
            return
        }
        if let id = event["command_id"] as? String, kind == "command_ack" || kind == "error" {
            resolveVoiceReceipt(id, result: kind == "command_ack"
                ? ["accepted": true, "task_id": NativeEventEnvelope.transportID(event) ?? "", "state": phase.rawValue,
                   "meaning": L("执行端已接收，尚不代表任务完成")]
                : ["accepted": false, "reason": event["text"] as? String ?? L("执行端拒绝")])
            if kind == "command_ack" { return }
        }
        if kind == "ready" {
            guard event["protocol"] as? Int == 1 else { error = L("运行组件版本不兼容。"); return }
            ready = true
            refreshSkills()
            return
        }
        if kind == "error" {
            if let id = NativeEventEnvelope.transportID(event), id != taskID { return }
            error = L(event["text"] as? String ?? "操作失败")
            sendingMessage = false
            if let id = event["message_id"] as? String {
                if messageDraft.isEmpty { messageDraft = sentMessages[id] ?? "" }
                sentMessages[id] = nil; sentMessageOrder.removeAll { $0 == id }
                if let previous = followUpPreviousPhase { phase = previous; followUpPreviousPhase = nil }
            }
            delegate?.refreshCompact()
            return
        }
        guard !taskID.isEmpty, NativeEventEnvelope.transportID(event) == taskID else { return }
        if receiveControlEvent(event) {
            if voiceMode && kind == "interrupted" { syncVoiceContext() }
            delegate?.refreshCompact()
            return
        }
        switch kind {
        case "overlay":
            let id = event["guard_id"] as? String ?? ""
            switch event["phase"] as? String {
            case "begin":
                guard !id.isEmpty else { return }
                overlayInteractions.insert(id)
                if delegate?.overlayWouldObstruct(event) == true { overlayOcclusions.insert(id) }
                delegate?.refreshCompact()
                try? bridge.send(["command": "overlay_ack", "task_id": taskID, "guard_id": id])
            case "end":
                overlayInteractions.remove(id); overlayOcclusions.remove(id)
            case "reset":
                overlayInteractions.removeAll(); overlayOcclusions.removeAll()
            default: break
            }
        case "state":
            if let state = event["state"] as? String, let value = TaskPhase(rawValue: state) { phase = value }
        case "message":
            guard let id = event["message_id"] as? String else { return }
            let text = sentMessages[id] ?? event["text"] as? String ?? ""
            let state = event["state"] as? String
            if let previous = controls.messageStates[id], ["consumed", "applied", "cancelled"].contains(previous) { return }
            controls.messageStates[id] = state
            if state != "cancelled" { appendChat(id: id, text: text, isUser: true) }
            if state == "queued", !controls.queue.contains(where: { $0.id == id }) {
                controls.queue.append(QueuedInput(id: id, text: text, kind: event["kind"] as? String ?? "message"))
            }
            if state == "consumed" || state == "applied" || state == "cancelled" { controls.queue.removeAll { $0.id == id } }
            if id == awaitingMessageID {
                sendingMessage = false
                if messageDraft == text { messageDraft = "" }
            }
            if state == "applied" || state == "consumed" || state == "cancelled" {
                sentMessages[id] = nil; sentMessageOrder.removeAll { $0 == id }
                messageStatus = state == "cancelled" ? L("队列消息已取消") : L("补充要求已进入规划")
            } else { messageStatus = phase == .paused || controls.queueSuspended ? L("已收到，继续任务后处理") : L("已收到，将在下一轮规划中处理") }
            if state == "cancelled", controls.awaitingNextTurn, sentMessages.isEmpty {
                // 触发下一轮的消息被取消：恢复发送前的阶段与草稿，不再等待不会开始的轮次。
                controls.awaitingNextTurn = false; controls.turnEndedAt = Date(); sendingMessage = false
                if messageDraft.isEmpty { messageDraft = text }
                if let previous = followUpPreviousPhase { phase = previous; followUpPreviousPhase = nil }
            }
        case "reply":
            executionPhase = ""
            phase = .running
            appendChat(id: "reply:" + questionID, text: event["text"] as? String ?? reply, isUser: true)
            sendingMessage = false; reply = ""; questionID = ""; question = ""; messageStatus = L("回复已发送")
        case "progress":
            if controls.sessionCompaction(event) {
                // 轮次之间的手动压缩只更新压缩显示，不改写会话阶段或步骤。
                controls.compactionRunning = true; controls.compacting = true
                controls.stage = "compaction"; controls.stageText = ""
                break
            }
            step = event["step"] as? Int ?? step
            executionPhase = event["phase"] as? String ?? executionPhase
            controls.stage = executionPhase; controls.stageText = ""
            controls.compacting = controls.compactionRunning || executionPhase == "compaction"
            if executionPhase == "execution", !intent.isEmpty { recentAction = intent }
            if executionPhase == "planning" {
                intent = ""; controls.intentPreview = ""; controls.intentComplete = false
            }
        case "intent":
            intent = event["text"] as? String ?? ""
            controls.intentPreview = intent; controls.intentComplete = true
        case "skill_loaded":
            guard let name = event["name"] as? String, !name.isEmpty else { return }
            appendChat(id: UUID().uuidString, text: L("已加载 Skill：") + name, isUser: false)
            persistCurrent()
        case "skill_warning":
            guard let text = event["text"] as? String, !text.isEmpty else { return }
            appendChat(id: UUID().uuidString, text: L("Skill 警告：") + text, isUser: false)
            persistCurrent()
        case "trajectory":
            runPath = event["path"] as? String ?? ""; resumeRunPath = runPath; persistCurrent()
        case "question":
            if !voiceMode { stopSpeech() }; sendingMessage = false
            questionID = event["question_id"] as? String ?? ""
            question = event["text"] as? String ?? ""; reply = ""; phase = .waiting
            appendChat(id: "question:" + questionID, text: question, isUser: false)
        case "turn_started":
            currentTurnInstruction = event["text"] as? String ?? ""
            sendingMessage = false
            if messageDraft == currentTurnInstruction { messageDraft = "" }
            followUpPreviousPhase = nil
            phase = .running; answer = ""; intent = ""; recentAction = ""; executionPhase = ""
            persistCurrent()
        case "turn_finished":
            if let path = event["path"] as? String, !path.isEmpty { runPath = path }
            let text = event["text"] as? String ?? ""
            let id = taskID + ":" + String(event["turn_index"] as? Int ?? 0)
            if !conversationTurns.contains(where: { $0.id == id }) {
                conversationTurns.append(ConversationTurn(id: id, instruction: currentTurnInstruction, answer: text))
            }
            appendChat(id: id, text: text.isEmpty ? L("本轮已结束。") : text, isUser: false)
            finish(TaskPhase(rawValue: event["state"] as? String ?? "failed") ?? .failed,
                   text: text, keepingConversation: true)
        case "finished":
            if let path = (event["path"] ?? event["trajectory"]) as? String, !path.isEmpty { runPath = path }
            let state = TaskPhase(rawValue: event["state"] as? String ?? "failed") ?? .failed
            let text = event["answer"] as? String ?? event["text"] as? String ?? ""
            finish(state, text: state == .stopped || state == .failed ? L(text) : text)
        default: break
        }
        if voiceMode && ["state", "question", "finished", "turn_started", "turn_finished"].contains(kind) { syncVoiceContext() }
        delegate?.refreshCompact()
    }
    func appendChat(id: String, text: String, isUser: Bool) {
        guard !text.isEmpty, !conversationMessages.contains(where: { $0.id == id }) else { return }
        conversationMessages.append(ConversationMessage(id: id, text: text, isUser: isUser))
    }
    func finish(_ state: TaskPhase, text: String, keepingConversation: Bool = false) {
        overlayInteractions.removeAll(); overlayOcclusions.removeAll()
        conversationOpen = keepingConversation
        completeControlTurn(keepingSession: keepingConversation)
        followUpPreviousPhase = nil
        if !keepingConversation && conversationMessages.last?.text != text {
            appendChat(id: UUID().uuidString, text: text, isUser: false)
        }
        if !voiceMode { stopSpeech() }
        if !keepingConversation && !sentMessages.isEmpty {
            // Never lose a message queued just before completion or cancellation.
            let queued = sentMessageOrder.compactMap { sentMessages[$0] }
            let remainingDraft = messageDraft.trimmingCharacters(in: .whitespacesAndNewlines)
            messageDraft = (queued + (remainingDraft.isEmpty || queued.contains(remainingDraft) ? [] : [remainingDraft])).joined(separator: "\n")
            messageStatus = L("任务已结束，有补充尚未进入规划，请确认后重新发送")
        }
        if !keepingConversation { sentMessages = [:]; sentMessageOrder = []; sendingMessage = false }
        phase = state; answer = text; question = ""; questionID = ""
        persistCurrent(); delegate?.refreshCompact()
    }
    func persistCurrent() {
        guard !currentConversationID.isEmpty else { return }
        let id = currentConversationID
        let date = history.first(where: { $0.id == id })?.date ?? Date()
        history.removeAll { $0.id == id }
        history.insert(TaskRecord(id: id, title: taskTitle, date: date, state: phase.rawValue, answer: answer, path: runPath, turns: conversationTurns, messages: conversationMessages, draft: messageDraft), at: 0)
        history = Array(history.prefix(100))
        do { try JSONEncoder().encode(history).write(to: dataDirectory.appendingPathComponent("history.json"), options: .atomic) }
        catch { self.error = L("任务记录保存失败：") + error.localizedDescription }
    }
    func canOpenConversation(_ record: TaskRecord) -> Bool {
        resetCommandID.isEmpty && !sendingMessage && (record.id == currentConversationID || canSwitchSession)
    }
    @discardableResult func openConversation(_ record: TaskRecord) -> Bool {
        guard canOpenConversation(record) else { error = L("请先停止当前任务，再切换对话。"); return false }
        if record.id == currentConversationID { return true }
        let root = dataDirectory.appendingPathComponent("runs").resolvingSymlinksInPath().standardizedFileURL.path + "/"
        let path = URL(fileURLWithPath: record.path).resolvingSymlinksInPath().standardizedFileURL
        guard !record.path.isEmpty, path.path.hasPrefix(root),
              FileManager.default.fileExists(atPath: path.appendingPathComponent("traj.json").path) else {
            error = L("历史对话的运行记录已丢失，无法继续。"); return false
        }
        if currentConversationID.isEmpty { newConversationDraft = messageDraft }
        persistCurrent()
        if conversationOpen {
            pendingHistoryRecord = record
            reset()
            if resetCommandID.isEmpty { pendingHistoryRecord = nil; return false }
        } else { loadConversation(record) }
        return true
    }
    private func loadConversation(_ record: TaskRecord) {
        clearConversation()
        conversationID = record.id; taskTitle = record.title; runPath = record.path; resumeRunPath = record.path
        phase = TaskPhase(rawValue: record.state) ?? .completed
        if phase.active { phase = .stopped }
        answer = record.answer; messageDraft = record.draft ?? ""; conversationTurns = record.turns ?? []
        conversationMessages = record.messages ?? []
        if conversationMessages.isEmpty {
            if let turns = record.turns, !turns.isEmpty {
                for turn in turns {
                    appendChat(id: turn.id + ":user", text: turn.instruction, isUser: true)
                    appendChat(id: turn.id, text: turn.answer, isUser: false)
                }
            } else {
                appendChat(id: record.id + ":initial", text: record.title, isUser: true)
                appendChat(id: record.id + ":answer", text: record.answer, isUser: false)
            }
        }
        if voiceMode { syncVoiceContext() }
        delegate?.refreshCompact()
    }
    func reset() {
        guard canSwitchSession, resetCommandID.isEmpty else { return }
        guard conversationOpen else { clearConversation(restoreNewDraft: true); return }
        let id = UUID().uuidString
        do {
            try bridge.send(["command": "end", "task_id": taskID, "command_id": id])
            resetCommandID = id; sendingMessage = true; messageStatus = L("正在开启新对话…")
        } catch { self.error = error.localizedDescription }
    }
    private func clearConversation(restoreNewDraft: Bool = false) {
        resetSessionControls()
        conversationID = ""; resumeRunPath = ""; pendingHistoryRecord = nil
        conversationOpen = false; taskID = ""; conversationTurns = []; conversationMessages = []; currentTurnInstruction = ""
        messageDraft = restoreNewDraft ? newConversationDraft : ""
        if restoreNewDraft { newConversationDraft = "" }
        messageStatus = ""; sentMessages = [:]; sentMessageOrder = []; sendingMessage = false; awaitingMessageID = ""
        followUpPreviousPhase = nil
        phase = .idle; answer = ""; error = ""; instruction = ""
        if voiceMode { syncVoiceContext() }
        delegate?.refreshCompact()
    }
    var showsCompactResult: Bool { [.completed, .stopped, .failed].contains(phase) }
    var compactResultText: String {
        if !answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return answer }
        switch phase {
        case .completed: return L("任务已完成，未返回文字结果。")
        case .stopped: return L("任务已停止，未返回文字结果。")
        case .failed: return error.isEmpty ? L("任务未完成，未返回文字结果。") : error
        default: return ""
        }
    }
    var compactYieldsToAutomation: Bool {
        [.starting, .running, .pausing, .stopping].contains(phase) || (phase == .waiting && sendingMessage)
    }
    var compactHidesForComputerAction: Bool {
        !overlayOcclusions.isEmpty
    }
    var foregroundWarning: String {
        controls.foregroundAllowed ? L("前台授权中：请勿操作电脑，以免打断 Agent 当前行为。") : ""
    }
    var progressBubbleHeight: CGFloat { foregroundWarning.isEmpty ? 44 : 72 }
    var compactPanelHeight: CGFloat {
        let content: CGFloat
        if voiceMode { content = showsCompactResult ? 334 : showsVoiceTaskProgress ? 274 : 208 }
        else { content = showsCompactResult || phase == .waiting ? 248 : phase == .paused ? 204 : phase == .idle ? 156 : 164 }
        let intentHeight: CGFloat = !voiceMode && phase.active && !currentIntent.isEmpty ? 28 : 0
        return content + intentHeight + (foregroundWarning.isEmpty ? 0 : 32) + (error.isEmpty ? 0 : 32) + (controls.approvals.isEmpty ? 0 : 24) + (controls.result.isEmpty ? 0 : 18) + (recoveryNotice.isEmpty ? 0 : 40)
    }
    var inputText: String {
        get { phase == .waiting ? reply : messageDraft }
        set { if phase == .waiting { reply = newValue } else { messageDraft = newValue } }
    }
    var inputPlaceholder: String { phase == .waiting ? L("回复这个问题…") : phase.active ? L("补充消息…") : conversationOpen || restoresHistory ? L("继续对话…") : L("在此输入…") }
    var canSendInput: Bool { ready && !sendingMessage && !recording && !speechPending && phase != .stopping && !inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    func sendInput() {
        guard canSendInput else { return }
        error = ""
        let text = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard text.count <= 8000 else { error = L("请将输入控制在 8,000 字以内。"); return }
        if phase == .waiting { command("reply"); return }
        if phase.active || conversationOpen {
            let id = UUID().uuidString
            let previousPhase = phase
            let previousControls = controls
            do {
                if !phase.active { prepareControlTurn(); followUpPreviousPhase = phase; phase = .starting }
                delegate?.refreshCompact()
                try bridge.send(["command": "message", "task_id": taskID, "message_id": id, "text": text])
                sentMessages[id] = text; sentMessageOrder.append(id); awaitingMessageID = id; sendingMessage = true
                messageDraft = text; messageStatus = L("正在发送…")
            } catch { phase = previousPhase; controls = previousControls; followUpPreviousPhase = nil; self.error = error.localizedDescription; delegate?.refreshCompact() }
        } else {
            instruction = text; start()
            if phase == .starting { messageDraft = "" }
        }
    }
    var selectedInputName: String {
        if preferences.audioInputUID.isEmpty { return L("系统默认 · ") + systemInputName }
        return audioInputs.first { $0.uid == preferences.audioInputUID }?.name ?? L("所选麦克风已断开")
    }
    var inputSignalText: String {
        if !recording { return L("麦克风未开启") }
        if !hasInputFrames { return L("等待音频数据…") }
        return inputDB > -50 ? L("收到声音") : inputDB > -75 ? L("声音较轻") : L("未检测到声音")
    }
    func refreshAudioInputs() {
        let devices = AudioInputDevices.available(), defaultID = AudioInputDevices.defaultID
        if audioInputs != devices { audioInputs = devices }
        systemInputName = devices.first { $0.deviceID == defaultID }?.name ?? L("无可用设备")
        guard voiceMode, recording else { return }
        let desired = AudioInputDevices.resolve(uid: preferences.audioInputUID, devices: devices, defaultID: defaultID)
        guard let desired else {
            qwenSpeech.stop(); error = L("麦克风已断开，请切换输入音源。"); speechStatus = L("音源不可用"); hasInputFrames = false
            return
        }
        if desired.uid != activeInputUID { retryVoice() }
    }
    func selectAudioInput(_ uid: String) {
        guard uid != preferences.audioInputUID else { return }
        preferences.audioInputUID = uid; preferences.save()
        if voiceMode { retryVoice() } else { refreshAudioInputs() }
    }
    func toggleSpeech(interaction: Bool = false) {
        if voiceMode { stopSpeech(); return }
        stopSpeech(); error = ""; voiceMode = true
        delegate?.collapse()
        beginVoiceConnection()
    }
    func retryVoice() { stopSpeech(); voiceMode = true; error = ""; delegate?.refreshCompact(); beginVoiceConnection() }
    private func beginVoiceConnection() {
        let token = speechGeneration
        speechPending = true; hasInputFrames = false; inputDB = -96; activeInputName = ""
        refreshAudioInputs()
        Task {
            do {
                _ = try AudioInputDevices.resolve(uid: preferences.audioInputUID)
                let url = try preferences.validatedSpeechURL()
                let key = try CredentialStore.read(preferences.speechCredentialAccount)
                guard speechGeneration == token else { return }
                guard !key.isEmpty else { throw AppError.message(L("请在设置中保存 Qwen 语音 API Key。")) }
                var configuration = preferences.speechSession
                configuration["instructions"] = preferences.speechSession["instructions"] as! String + L("\n当前电脑任务：") + voiceContextDescription
                await qwenSpeech.start(url: url, key: key, configuration: configuration, inputDeviceUID: preferences.audioInputUID)
            } catch { self.error = error.localizedDescription; speechPending = false; speechStatus = L("连接已断开") }
        }
    }
    private var voiceContextDescription: String { L("状态：%@；任务：%@；问题：%@；结果：%@", String(describing: phase.label), String(describing: taskTitle), String(describing: question), String(describing: String(answer.prefix(1200)))) }
    private func syncVoiceContext() { qwenSpeech.updateTaskContext(voiceContextDescription) }
    private func resolveVoiceReceipt(_ id: String, result: [String: Any]) {
        voiceReceiptTimeouts.removeValue(forKey: id)?.cancel()
        voiceReceipts.removeValue(forKey: id)?.resume(returning: result)
    }
    func handleVoiceTool(_ call: VoiceTaskCall, context: VoiceTaskContext) async -> [String: Any] {
        guard voiceMode, ready, !disconnected else { return ["accepted": false, "reason": L("语音或执行端未连接")] }
        // Bind a response to the task that existed when its generation began.
        guard context.taskID == taskID, context.questionID == questionID, context.wasActive == phase.active else {
            return ["accepted": false, "reason": L("任务状态已变化，请根据当前任务重新确认")]
        }
        guard phase != .stopping else { return ["accepted": false, "reason": L("当前任务正在停止，请等待停止完成")] }
        if call.name == "stop_task" && !phase.active { return ["accepted": false, "reason": L("当前没有执行中的任务")] }
        guard !sendingMessage else { return ["accepted": false, "reason": L("上一条要求仍在发送，请稍后重试")] }
        let id = UUID().uuidString
        let previousPhase = phase
        let wasSending = sendingMessage
        let previousControls = controls
        return await withCheckedContinuation { continuation in
            voiceReceipts[id] = continuation
            voiceReceiptTimeouts[id] = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 8_000_000_000)
                guard !Task.isCancelled else { return }
                self?.resolveVoiceReceipt(id, result: ["accepted": false, "reason": L("未收到执行端确认，状态未知；请核实当前任务，不要自动重发")])
            }
            do {
                if call.name == "stop_task" {
                    try bridge.send(["command": "stop", "task_id": taskID, "command_id": id]); phase = .stopping
                } else if phase == .waiting {
                    sendingMessage = true
                    delegate?.refreshCompact()
                    try bridge.send(["command": "reply", "task_id": taskID, "question_id": questionID, "text": call.text, "command_id": id])
                } else if phase.active || conversationOpen {
                    let messageID = UUID().uuidString
                    if !phase.active { prepareControlTurn(); followUpPreviousPhase = phase; phase = .starting }
                    delegate?.refreshCompact()
                    try bridge.send(["command": "message", "task_id": taskID, "message_id": messageID, "text": call.text, "command_id": id])
                    sentMessages[messageID] = call.text; sentMessageOrder.append(messageID)
                    awaitingMessageID = messageID; sendingMessage = true; messageStatus = L("语音要求正在发送…")
                } else {
                    instruction = call.text; start(commandID: id)
                    if phase != .starting { resolveVoiceReceipt(id, result: ["accepted": false, "reason": error.isEmpty ? L("任务未启动，请检查应用配置") : error]) }
                }
                delegate?.refreshCompact()
            } catch {
                phase = previousPhase; controls = previousControls; sendingMessage = wasSending; followUpPreviousPhase = nil
                delegate?.refreshCompact()
                resolveVoiceReceipt(id, result: ["accepted": false, "reason": error.localizedDescription])
            }
        }
    }
    func checkSpeechConnection(_ settings: Preferences, key: String) {
        stopSpeech(); error = ""
        do {
            let url = try settings.validatedSpeechURL()
            guard !key.isEmpty else { throw AppError.message(L("请填写所选地区的语音 API Key。")) }
            speechPending = true
            Task { await qwenSpeech.start(url: url, key: key, configuration: settings.speechSession, captureAudio: false) }
        } catch { self.error = error.localizedDescription }
    }
    func refreshPermissions() {
        accessibilityGranted = MacPermissions.accessibility
        screenRecordingGranted = MacPermissions.screenRecording
    }
    func stopSpeech() {
        speechGeneration = UUID(); voiceMode = false; speech.stop(); qwenSpeech.stop(); speechPending = false; speechStatus = ""; hasInputFrames = false; inputDB = -96; activeInputUID = ""; activeInputName = ""
        for id in Array(voiceReceipts.keys) { resolveVoiceReceipt(id, result: ["accepted": false, "reason": L("语音已断开；已发送的电脑指令仍可能执行，请查看任务状态")]) }
        delegate?.refreshCompact()
    }
    func revealRun(_ path: String) {
        guard !path.isEmpty else { return }
        let url = URL(fileURLWithPath: path).standardizedFileURL
        guard url.path.hasPrefix(dataDirectory.appendingPathComponent("runs").path + "/") else { return }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }
}
