import AppKit
import Combine
import SwiftUI

@MainActor struct SettingsView: View {
    @ObservedObject var model: AppModel
    @StateObject private var editor: SettingsEditor
    var onClose: () -> Void
    private let permissionRefresh = Timer.publish(every: 2, on: .main, in: .common).autoconnect()

    init(model: AppModel, onClose: @escaping () -> Void = {}) {
        self.model = model; self.onClose = onClose
        _editor = StateObject(wrappedValue: SettingsEditor(model: model))
    }
    var body: some View {
        VStack(spacing: 0) {
            TabView {
                general.tabItem { Label("通用", systemImage: "slider.horizontal.3") }
                ModelSettingsForm(editor: editor).tabItem { Label("模型", systemImage: "server.rack") }
                permissions.tabItem { Label("权限", systemImage: "hand.raised") }
                voice.tabItem { Label("语音", systemImage: "waveform") }
            }.padding(.top, 8)
            if !settingsError.isEmpty {
                Label(settingsError, systemImage: "exclamationmark.circle.fill")
                    .font(.callout).foregroundStyle(.orange).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 20).padding(.bottom, 12)
            }
            Divider()
            HStack {
                Button("取消", action: onClose).keyboardShortcut(.cancelAction)
                Spacer()
                Button("保存") {
                    do { try editor.persist(includeVoice: true); onClose() }
                    catch { editor.error = error.localizedDescription }
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
            }.padding(16)
        }.frame(width: 640, height: 550)
            .background(Color(nsColor: .windowBackgroundColor)).preferredColorScheme(model.colorScheme)
            .onAppear { editor.load(includeVoice: true) }
            .onReceive(permissionRefresh) { _ in model.refreshPermissions() }
    }
    private var settingsError: String { editor.error.isEmpty ? model.error : editor.error }
    private var general: some View {
        Form {
            Section("外观") {
                Picker("外观", selection: $editor.draft.appearance) {
                    ForEach(["跟随系统", "浅色", "深色"], id: \.self) { Text($0) }
                }
            }
            Section {
                Toggle("按 Enter 键发送", isOn: $editor.draft.sendWithEnter)
                LabeledContent("全局快捷键", value: model.globalShortcutStatus).textSelection(.enabled)
            } header: { Text("键盘") }
            footer: { Text("开启后，Shift+Enter 换行；关闭后，Enter 换行、⌘Enter 发送。中文输入法确认候选词时不会发送。") }
            Section("OpenGCUA") {
                Button("打开设置助理…") { onClose(); model.delegate?.showSetup() }
                Button("打开任务记录文件夹", systemImage: "folder") { NSWorkspace.shared.open(model.dataDirectory) }
            }
        }.formStyle(.grouped)
    }
    private var permissions: some View {
        Form {
            Section {
                PermissionStatusRow(title: "辅助功能", detail: "点击、输入和操作应用", symbol: "cursorarrow", granted: model.accessibilityGranted)
                PermissionStatusRow(title: "屏幕录制", detail: "查看任务所需的屏幕内容", symbol: "rectangle.inset.filled", granted: model.screenRecordingGranted)
                Button("打开拖拽授权引导…") { MacPermissions.showGuide(model: model) }
            } header: { Text("Mac 控制权限") }
            footer: { Text("请授权 OpenGCUA 本身。首次查看屏幕时，macOS 还可能要求确认直接访问屏幕。") }
            Section {
                Label("麦克风权限会在使用语音时请求", systemImage: "mic")
                Text("任务记录保存在这台 Mac。执行任务时，相关界面和任务内容会发送到你选择的模型服务。")
                    .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            } header: { Text("隐私") }
        }.formStyle(.grouped)
    }
    private var voice: some View {
        Form {
            Section("输入") {
                Picker("麦克风", selection: $editor.draft.audioInputUID) {
                    Text("系统默认 · " + model.systemInputName).tag("")
                    ForEach(model.audioInputs) { Text($0.name).tag($0.uid) }
                    if !editor.draft.audioInputUID.isEmpty && !model.audioInputs.contains(where: { $0.uid == editor.draft.audioInputUID }) {
                        Text("所选麦克风已断开").tag(editor.draft.audioInputUID)
                    }
                }
                Button("刷新音源", action: model.refreshAudioInputs)
            }
            Section {
                Picker("服务地区", selection: $editor.draft.speechRegion) { Text("北京").tag("北京"); Text("新加坡").tag("新加坡") }
                TextField("语音模型", text: $editor.draft.speechModel)
                TextField("Workspace ID（可选）", text: $editor.draft.speechWorkspaceID)
                if editor.sharesSpeechKey && !editor.fileHasKey { Label("使用模型设置中的钥匙串 API Key", systemImage: "key").foregroundStyle(.secondary) }
                else if editor.sharesSpeechKey { SecureField("语音 API Key", text: $editor.modelKey).help("配置文件中的密钥不会复制到语音服务；此密钥保存在钥匙串中") }
                else { SecureField("语音 API Key", text: $editor.speechKey) }
                Button("测试连接（不录音）") {
                    model.checkSpeechConnection(editor.draft, key: editor.sharesSpeechKey ? editor.modelKey : editor.speechKey)
                }.disabled(model.speechPending || model.recording)
                if !model.speechStatus.isEmpty { Text(model.speechStatus).foregroundStyle(.secondary) }
            } header: { Text("实时语音服务") }
            footer: { Text("开启语音后持续收音，退出语音条后停止。API Key 需要与所选服务地区和业务空间匹配。") }
        }.formStyle(.grouped).textFieldStyle(.roundedBorder)
            .onChange(of: editor.draft.speechRegion) { _, _ in editor.loadSpeechKey() }
    }
}

struct PermissionStatusRow: View {
    var title: String
    var detail: String
    var symbol: String
    var granted: Bool
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).font(.title3).foregroundStyle(.secondary).frame(width: 26)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.body.weight(.medium))
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 12)
            Label(granted ? "已允许" : "待授权", systemImage: granted ? "checkmark.circle.fill" : "circle")
                .font(.callout).foregroundStyle(granted ? Color.green : Color.secondary)
        }.padding(.vertical, 5)
    }
}
