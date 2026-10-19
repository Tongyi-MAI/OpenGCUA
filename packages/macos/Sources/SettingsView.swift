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
                general.tabItem { Label(L("通用"), systemImage: "slider.horizontal.3") }
                ModelSettingsForm(editor: editor).tabItem { Label(L("模型"), systemImage: "server.rack") }
                permissions.tabItem { Label(L("权限"), systemImage: "hand.raised") }
                voice.tabItem { Label(L("语音"), systemImage: "waveform") }
            }.padding(.top, 8).id(model.preferences.language)
            if !settingsError.isEmpty {
                Label(L(settingsError), systemImage: "exclamationmark.circle.fill")
                    .font(.callout).foregroundStyle(.orange).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 20).padding(.bottom, 12)
            }
            Divider()
            HStack {
                Button(L("取消"), action: onClose).keyboardShortcut(.cancelAction)
                Spacer()
                Button(L("保存")) {
                    do { try editor.persist(includeVoice: true); onClose() }
                    catch { editor.error = error.localizedDescription }
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
            }.padding(16)
        }.frame(width: 640, height: 550)
            .background(Color(nsColor: .windowBackgroundColor)).preferredColorScheme(model.colorScheme).environment(\.locale, L10n.locale)
            .onAppear { editor.load(includeVoice: true) }
            .onReceive(permissionRefresh) { _ in model.refreshPermissions() }
    }
    private var settingsError: String { editor.error.isEmpty ? model.error : editor.error }
    private var general: some View {
        Form {
            Section(L("语言")) {
                Picker(L("界面语言"), selection: $editor.draft.language) {
                    Text(L("跟随系统")).tag("system")
                    Text("English").tag("en")
                    Text("简体中文").tag("zh-Hans")
                }.onChange(of: editor.draft.language) { _, value in model.setLanguage(value) }
                Text(L("语言更改会立即生效。")).font(.caption).foregroundStyle(.secondary)
            }
            Section(L("外观")) {
                Picker(L("外观"), selection: $editor.draft.appearance) {
                    ForEach(["跟随系统", "浅色", "深色"], id: \.self) { Text(L($0)).tag($0) }
                }
            }
            Section {
                Toggle(L("按 Enter 键发送"), isOn: $editor.draft.sendWithEnter)
                LabeledContent(L("全局快捷键"), value: model.globalShortcutStatus).textSelection(.enabled)
            } header: { Text(L("键盘")) }
            footer: { Text(L("开启后，Shift+Enter 换行；关闭后，Enter 换行、⌘Enter 发送。中文输入法确认候选词时不会发送。")) }
            Section("OpenGCUA") {
                Button(L("打开设置助理…")) { onClose(); model.delegate?.showSetup() }
                Button(L("打开任务记录文件夹"), systemImage: "folder") { NSWorkspace.shared.open(model.dataDirectory) }
            }
        }.formStyle(.grouped)
    }
    private var permissions: some View {
        Form {
            Section {
                PermissionStatusRow(title: L("辅助功能"), detail: L("点击、输入和操作应用"), symbol: "cursorarrow", granted: model.accessibilityGranted)
                PermissionStatusRow(title: L("屏幕录制"), detail: L("查看任务所需的屏幕内容"), symbol: "rectangle.inset.filled", granted: model.screenRecordingGranted)
                Button(L("打开拖拽授权引导…")) { MacPermissions.showGuide(model: model) }
            } header: { Text(L("Mac 控制权限")) }
            footer: { Text(L("请授权 OpenGCUA 本身。首次查看屏幕时，macOS 还可能要求确认直接访问屏幕。")) }
            Section {
                Label(L("麦克风权限会在使用语音时请求"), systemImage: "mic")
                Text(L("任务记录保存在这台 Mac。执行任务时，相关界面和任务内容会发送到你选择的模型服务。"))
                    .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            } header: { Text(L("隐私")) }
        }.formStyle(.grouped)
    }
    private var voice: some View {
        Form {
            Section(L("输入")) {
                Picker(L("麦克风"), selection: $editor.draft.audioInputUID) {
                    Text(L("系统默认 · ") + model.systemInputName).tag("")
                    ForEach(model.audioInputs) { Text($0.name).tag($0.uid) }
                    if !editor.draft.audioInputUID.isEmpty && !model.audioInputs.contains(where: { $0.uid == editor.draft.audioInputUID }) {
                        Text(L("所选麦克风已断开")).tag(editor.draft.audioInputUID)
                    }
                }
                Button(L("刷新音源"), action: model.refreshAudioInputs)
            }
            Section {
                Picker(L("服务地区"), selection: $editor.draft.speechRegion) { Text(L("北京")).tag("北京"); Text(L("新加坡")).tag("新加坡") }
                TextField(L("语音模型"), text: $editor.draft.speechModel)
                TextField(L("Workspace ID（可选）"), text: $editor.draft.speechWorkspaceID)
                if editor.sharesSpeechKey && !editor.fileHasKey { Label(L("使用模型设置中的钥匙串 API Key"), systemImage: "key").foregroundStyle(.secondary) }
                else if editor.sharesSpeechKey { SecureField(L("语音 API Key"), text: $editor.modelKey).help(L("配置文件中的密钥不会复制到语音服务；此密钥保存在钥匙串中")) }
                else { SecureField(L("语音 API Key"), text: $editor.speechKey) }
                Button(L("测试连接（不录音）")) {
                    model.checkSpeechConnection(editor.draft, key: editor.sharesSpeechKey ? editor.modelKey : editor.speechKey)
                }.disabled(model.speechPending || model.recording)
                if !model.speechStatus.isEmpty { Text(L(model.speechStatus)).foregroundStyle(.secondary) }
            } header: { Text(L("实时语音服务")) }
            footer: { Text(L("开启语音后持续收音，退出语音条后停止。API Key 需要与所选服务地区和业务空间匹配。")) }
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
            Label(granted ? L("已允许") : L("待授权"), systemImage: granted ? "checkmark.circle.fill" : "circle")
                .font(.callout).foregroundStyle(granted ? Color.green : Color.secondary)
        }.padding(.vertical, 5)
    }
}
