import AppKit
import SwiftUI

struct TaskRecord: Codable, Identifiable {
    var id: String
    var title: String
    var date = Date()
    var state: String
    var answer: String
    var path: String
}

enum TaskPhase: String {
    case idle, starting, running, pausing, paused, waiting, stopping, completed, stopped, failed
    var active: Bool { [.starting, .running, .pausing, .paused, .waiting, .stopping].contains(self) }
    var label: String {
        switch self {
        case .idle: "准备就绪"
        case .starting: "正在准备任务"
        case .running: "正在执行"
        case .pausing: "正在暂停，等待当前动作结束"
        case .paused: "已暂停，随时继续"
        case .waiting: "需要你的回复"
        case .stopping: "正在停止任务"
        case .completed: "任务已完成"
        case .stopped: "任务已停止"
        case .failed: "任务未完成"
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
    @Published var instruction = ""
    @Published var phase: TaskPhase = .idle
    @Published var ready = false
    @Published var disconnected = false
    @Published var intent = ""
    @Published var step = 0
    @Published var executionPhase = ""
    @Published var recentAction = ""
    @Published var question = ""
    @Published var reply = ""
    @Published var answer = ""
    @Published var error = ""
    @Published var globalShortcutStatus = "⌃⌥Space · 呼出 / 收起"
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
    @Published var voiceMode = false
    @Published var voiceLevel = 0.0
    @Published var audioInputs: [AudioInputDevice] = []
    @Published var systemInputName = "无可用设备"
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
        do {
            try FileManager.default.createDirectory(at: self.dataDirectory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: self.dataDirectory.path)
            let path = self.dataDirectory.appendingPathComponent("history.json")
            if let data = try? Data(contentsOf: path), let records = try? JSONDecoder().decode([TaskRecord].self, from: data) {
                history = records.map { record in
                    var r = record
                    if TaskPhase(rawValue: r.state)?.active == true { r.state = "stopped"; r.answer = "上次运行意外中断，请确认操作结果后重新发起任务。" }
                    return r
                }
            }
        } catch { self.error = error.localizedDescription }
        bridge.onEvent = { [weak self] in self?.receive($0) }
        bridge.onExit = { [weak self] in
            guard let self else { return }
            self.ready = false
            self.disconnected = true
            self.error = "运行组件已退出，可以重新连接。"
            if self.phase.active { self.finish(.failed, text: "运行组件意外退出。部分操作可能已生效，请核实后再试。") }
            self.delegate?.runtimeExited()
        }
        qwenSpeech.context = { [weak self] in VoiceTaskContext(taskID: self?.taskID ?? "", questionID: self?.questionID ?? "", wasActive: self?.phase.active ?? false) }
        qwenSpeech.onTool = { [weak self] call, context in
            guard let self else { return ["accepted": false, "reason": "应用已关闭"] }
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
            self?.speechPending = ["正在请求麦克风…", "正在连接 Qwen…", "正在完成识别…"].contains(text)
        }
        qwenSpeech.onError = { [weak self] in self?.error = $0; self?.speechStatus = "连接已断开"; self?.speechPending = false }
        speech.onState = { [weak self] in self?.recording = $0 }
        speech.onError = { [weak self] in self?.error = $0 }
    }
    deinit { inputMonitor?.invalidate() }
    var colorScheme: ColorScheme? { preferences.appearance == "浅色" ? .light : preferences.appearance == "深色" ? .dark : nil }
    var status: String { phase == .running && !intent.isEmpty ? intent : phase.label }
    var tint: Color { phase == .failed || phase == .waiting ? .orange : phase == .completed ? .green : .accentColor }
    var showsVoiceTaskProgress: Bool { phase != .idle }
    var taskProgressTitle: String {
        guard phase == .running else { return phase.label }
        switch executionPhase {
        case "planning": return "思考中"
        case "execution": return "正在操作"
        case "postprocess": return "正在检查结果"
        case "compaction": return "正在整理上下文"
        case "recovery": return "正在恢复执行"
        case "user_wait": return "等待你的回复"
        case "complete": return "正在继续任务"
        default: return "正在准备"
        }
    }
    var taskProgressDetail: String {
        if phase == .waiting { return question }
        if !phase.active { return answer.isEmpty ? taskTitle : answer }
        if executionPhase == "execution", !intent.isEmpty { return intent }
        if !recentAction.isEmpty { return "最近动作：" + recentAction }
        return taskTitle
    }
    func connect() {
        disconnected = false; error = ""
        do { try bridge.launch(dataDirectory: dataDirectory) }
        catch { self.error = error.localizedDescription; disconnected = true }
    }
    func start(commandID: String? = nil) {
        guard ready, !phase.active, !instruction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        refreshPermissions()
        guard accessibilityGranted && screenRecordingGranted else {
            error = "请先为 OpenGCUA 开启辅助功能和屏幕录制权限。授权 CuaDriver 不会替代 OpenGCUA 的权限。"
            MacPermissions.showGuide(model: self)
            return
        }
        do {
            var config = try preferences.taskConfiguration()
            let endpoint = config["llm_base_url"] as! String
            let fileKey = (config["api_key"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let key = fileKey.isEmpty ? try CredentialStore.read(endpoint) : fileKey
            guard !key.isEmpty || config["allow_empty_api_key"] as? Bool == true else { delegate?.showSettings(); error = "请在配置文件或此服务对应的钥匙串中保存 API Key。"; return }
            config["api_key"] = key
            if !voiceMode { stopSpeech() }
            taskID = UUID().uuidString; taskTitle = instruction.trimmingCharacters(in: .whitespacesAndNewlines)
            question = ""; reply = ""; answer = ""; intent = ""; error = ""; runPath = ""; step = 0
            executionPhase = ""; recentAction = ""
            phase = .starting
            messageStatus = ""; sentMessages = [:]; sentMessageOrder = []; sendingMessage = false; awaitingMessageID = ""
            var request: [String: Any] = ["command": "start", "task_id": taskID, "text": taskTitle,
                             "config": config]
            if let commandID { request["command_id"] = commandID }
            try bridge.send(request)
            persistCurrent()
            delegate?.collapse()
        } catch { self.error = error.localizedDescription; if phase == .starting { finish(.failed, text: error.localizedDescription) } }
    }
    func command(_ name: String) {
        do {
            var command: [String: Any] = ["command": name, "task_id": taskID]
            if name == "reply" {
                guard !reply.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, !sendingMessage else { return }
                if !voiceMode { stopSpeech() }; command["question_id"] = questionID; command["text"] = reply
            }
            try bridge.send(command)
            if name == "reply" { sendingMessage = true }
            if name == "stop" { phase = .stopping }
            if name == "pause" { phase = .pausing }
        } catch { self.error = error.localizedDescription }
    }
    func receive(_ event: [String: Any]) {
        let kind = event["event"] as? String ?? ""
        if let id = event["command_id"] as? String, kind == "command_ack" || kind == "error" {
            resolveVoiceReceipt(id, result: kind == "command_ack"
                ? ["accepted": true, "task_id": event["task_id"] as? String ?? "", "state": phase.rawValue,
                   "meaning": "执行端已接收，尚不代表任务完成"]
                : ["accepted": false, "reason": event["text"] as? String ?? "执行端拒绝"])
            if kind == "command_ack" { return }
        }
        if kind == "ready" {
            guard event["protocol"] as? Int == 1 else { error = "运行组件版本不兼容。"; return }
            ready = true; return
        }
        if kind == "error" {
            if let id = event["task_id"] as? String, id != taskID { return }
            error = event["text"] as? String ?? "操作失败"
            sendingMessage = false
            if let id = event["message_id"] as? String { sentMessages[id] = nil }
            return
        }
        guard event["task_id"] as? String == taskID else { return }
        switch kind {
        case "state":
            if let state = event["state"] as? String, let value = TaskPhase(rawValue: state) { phase = value }
        case "message":
            guard let id = event["message_id"] as? String, let text = sentMessages[id] else { return }
            let state = event["state"] as? String
            if id == awaitingMessageID {
                sendingMessage = false
                if messageDraft == text { messageDraft = "" }
            }
            if state == "applied" {
                sentMessages[id] = nil; messageStatus = "补充要求已进入规划"
            } else { messageStatus = phase == .paused ? "已收到，继续任务后处理" : "已收到，将在下一轮规划中处理" }
        case "reply":
            sendingMessage = false; reply = ""; questionID = ""; question = ""; messageStatus = "回复已发送"
        case "progress":
            step = event["step"] as? Int ?? step
            executionPhase = event["phase"] as? String ?? executionPhase
            if executionPhase == "execution", !intent.isEmpty { recentAction = intent }
            if executionPhase == "planning" { intent = "" }
        case "intent": intent = event["text"] as? String ?? ""
        case "trajectory": runPath = event["path"] as? String ?? ""; persistCurrent()
        case "question":
            if !voiceMode { stopSpeech() }; sendingMessage = false
            questionID = event["question_id"] as? String ?? ""
            question = event["text"] as? String ?? ""; reply = ""; phase = .waiting
        case "finished":
            runPath = event["path"] as? String ?? runPath
            finish(TaskPhase(rawValue: event["state"] as? String ?? "failed") ?? .failed, text: event["text"] as? String ?? "")
        default: break
        }
        if voiceMode && ["state", "question", "finished"].contains(kind) { syncVoiceContext() }
        delegate?.refreshCompact()
    }
    func finish(_ state: TaskPhase, text: String) {
        if !voiceMode { stopSpeech() }
        if !sentMessages.isEmpty {
            // Never lose a message queued just before completion or cancellation.
            let queued = sentMessageOrder.compactMap { sentMessages[$0] }
            let remainingDraft = messageDraft.trimmingCharacters(in: .whitespacesAndNewlines)
            messageDraft = (queued + (remainingDraft.isEmpty || queued.contains(remainingDraft) ? [] : [remainingDraft])).joined(separator: "\n")
            messageStatus = "任务已结束，有补充尚未进入规划，请确认后重新发送"
        }
        sentMessages = [:]; sentMessageOrder = []; sendingMessage = false
        phase = state; answer = text; question = ""; questionID = ""
        persistCurrent(); delegate?.refreshCompact()
    }
    func persistCurrent() {
        guard !taskID.isEmpty else { return }
        let date = history.first(where: { $0.id == taskID })?.date ?? Date()
        history.removeAll { $0.id == taskID }
        history.insert(TaskRecord(id: taskID, title: taskTitle, date: date, state: phase.rawValue, answer: answer, path: runPath), at: 0)
        history = Array(history.prefix(100))
        do { try JSONEncoder().encode(history).write(to: dataDirectory.appendingPathComponent("history.json"), options: .atomic) }
        catch { self.error = "任务记录保存失败：" + error.localizedDescription }
    }
    func reset() { guard !phase.active else { return }; phase = .idle; answer = ""; error = ""; instruction = "" }
    var showsCompactResult: Bool { [.completed, .stopped, .failed].contains(phase) }
    var compactResultText: String {
        if !answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return answer }
        switch phase {
        case .completed: return "任务已完成，未返回文字结果。"
        case .stopped: return "任务已停止，未返回文字结果。"
        case .failed: return error.isEmpty ? "任务未完成，未返回文字结果。" : error
        default: return ""
        }
    }
    var compactPanelHeight: CGFloat {
        let content: CGFloat
        if voiceMode { content = showsCompactResult ? 335 : showsVoiceTaskProgress ? 255 : 174 }
        else { content = showsCompactResult ? 326 : phase == .waiting ? 318 : 206 }
        return content + (error.isEmpty ? 0 : 36)
    }
    var inputText: String {
        get { phase == .waiting ? reply : messageDraft }
        set { if phase == .waiting { reply = newValue } else { messageDraft = newValue } }
    }
    var inputPlaceholder: String { phase == .waiting ? "回复这个问题…" : phase.active ? "补充要求或调整任务…" : "输入一个新任务…" }
    var canSendInput: Bool { ready && !sendingMessage && !recording && !speechPending && phase != .stopping && !inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    func sendInput() {
        guard canSendInput else { return }
        error = ""
        let text = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard text.count <= 8000 else { error = "请将输入控制在 8,000 字以内。"; return }
        if phase == .waiting { command("reply"); return }
        if phase.active {
            let id = UUID().uuidString
            do {
                try bridge.send(["command": "message", "task_id": taskID, "message_id": id, "text": text])
                sentMessages[id] = text; sentMessageOrder.append(id); awaitingMessageID = id; sendingMessage = true
                messageDraft = text; messageStatus = "正在发送…"
            } catch { self.error = error.localizedDescription }
        } else {
            instruction = text; start()
            if phase == .starting { messageDraft = "" }
        }
    }
    var selectedInputName: String {
        if preferences.audioInputUID.isEmpty { return "系统默认 · " + systemInputName }
        return audioInputs.first { $0.uid == preferences.audioInputUID }?.name ?? "所选麦克风已断开"
    }
    var inputSignalText: String {
        if !recording { return "麦克风未开启" }
        if !hasInputFrames { return "等待音频数据…" }
        return inputDB > -50 ? "收到声音" : inputDB > -75 ? "声音较轻" : "未检测到声音"
    }
    func refreshAudioInputs() {
        let devices = AudioInputDevices.available(), defaultID = AudioInputDevices.defaultID
        if audioInputs != devices { audioInputs = devices }
        systemInputName = devices.first { $0.deviceID == defaultID }?.name ?? "无可用设备"
        guard voiceMode, recording else { return }
        let desired = AudioInputDevices.resolve(uid: preferences.audioInputUID, devices: devices, defaultID: defaultID)
        guard let desired else {
            qwenSpeech.stop(); error = "麦克风已断开，请切换输入音源。"; speechStatus = "音源不可用"; hasInputFrames = false
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
                guard !key.isEmpty else { throw AppError.message("请在设置中保存 Qwen 语音 API Key。") }
                var configuration = preferences.speechSession
                configuration["instructions"] = VoiceTaskTools.instructions + "\n当前电脑任务：" + voiceContextDescription
                await qwenSpeech.start(url: url, key: key, configuration: configuration, inputDeviceUID: preferences.audioInputUID)
            } catch { self.error = error.localizedDescription; speechPending = false; speechStatus = "连接已断开" }
        }
    }
    private var voiceContextDescription: String { "状态：\(phase.label)；任务：\(taskTitle)；问题：\(question)；结果：\(String(answer.prefix(1200)))" }
    private func syncVoiceContext() { qwenSpeech.updateTaskContext(voiceContextDescription) }
    private func resolveVoiceReceipt(_ id: String, result: [String: Any]) {
        voiceReceiptTimeouts.removeValue(forKey: id)?.cancel()
        voiceReceipts.removeValue(forKey: id)?.resume(returning: result)
    }
    func handleVoiceTool(_ call: VoiceTaskCall, context: VoiceTaskContext) async -> [String: Any] {
        guard voiceMode, ready, !disconnected else { return ["accepted": false, "reason": "语音或执行端未连接"] }
        // Bind a response to the task that existed when its generation began.
        guard context.taskID == taskID, context.questionID == questionID, context.wasActive == phase.active else {
            return ["accepted": false, "reason": "任务状态已变化，请根据当前任务重新确认"]
        }
        guard phase != .stopping else { return ["accepted": false, "reason": "当前任务正在停止，请等待停止完成"] }
        if call.name == "stop_task" && !phase.active { return ["accepted": false, "reason": "当前没有执行中的任务"] }
        guard !sendingMessage else { return ["accepted": false, "reason": "上一条要求仍在发送，请稍后重试"] }
        let id = UUID().uuidString
        return await withCheckedContinuation { continuation in
            voiceReceipts[id] = continuation
            voiceReceiptTimeouts[id] = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 8_000_000_000)
                guard !Task.isCancelled else { return }
                self?.resolveVoiceReceipt(id, result: ["accepted": false, "reason": "未收到执行端确认，状态未知；请核实当前任务，不要自动重发"])
            }
            do {
                if call.name == "stop_task" {
                    try bridge.send(["command": "stop", "task_id": taskID, "command_id": id]); phase = .stopping
                } else if phase == .waiting {
                    try bridge.send(["command": "reply", "task_id": taskID, "question_id": questionID, "text": call.text, "command_id": id])
                    sendingMessage = true
                } else if phase.active {
                    let messageID = UUID().uuidString
                    try bridge.send(["command": "message", "task_id": taskID, "message_id": messageID, "text": call.text, "command_id": id])
                    sentMessages[messageID] = call.text; sentMessageOrder.append(messageID)
                    awaitingMessageID = messageID; sendingMessage = true; messageStatus = "语音要求正在发送…"
                } else {
                    instruction = call.text; start(commandID: id)
                    if phase != .starting { resolveVoiceReceipt(id, result: ["accepted": false, "reason": error.isEmpty ? "任务未启动，请检查应用配置" : error]) }
                }
                delegate?.refreshCompact()
            } catch { resolveVoiceReceipt(id, result: ["accepted": false, "reason": error.localizedDescription]) }
        }
    }
    func checkSpeechConnection(_ settings: Preferences, key: String) {
        stopSpeech(); error = ""
        do {
            let url = try settings.validatedSpeechURL()
            guard !key.isEmpty else { throw AppError.message("请填写所选地区的语音 API Key。") }
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
        for id in Array(voiceReceipts.keys) { resolveVoiceReceipt(id, result: ["accepted": false, "reason": "语音已断开；已发送的电脑指令仍可能执行，请查看任务状态"]) }
        delegate?.refreshCompact()
    }
    func revealRun(_ path: String) {
        guard !path.isEmpty else { return }
        let url = URL(fileURLWithPath: path).standardizedFileURL
        guard url.path.hasPrefix(dataDirectory.appendingPathComponent("runs").path + "/") else { return }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }
}
