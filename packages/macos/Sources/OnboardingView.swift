import AppKit
import Combine
import SwiftUI

@MainActor struct OnboardingView: View {
    @ObservedObject var model: AppModel
    @StateObject private var editor: SettingsEditor
    @ViewState private var step = FirstLaunch.progress()
    private let refresh = Timer.publish(every: 2, on: .main, in: .common).autoconnect()

    init(model: AppModel) {
        self.model = model
        _editor = StateObject(wrappedValue: SettingsEditor(model: model))
    }
    var body: some View {
        VStack(spacing: 0) {
            Group {
                switch step {
                case .welcome: welcome
                case .model:
                    VStack(spacing: 0) {
                        heading("连接你的模型", subtitle: "填写服务信息，或选择已有的 JSON 配置。", symbol: "server.rack")
                        ModelSettingsForm(editor: editor, introductory: true)
                    }
                case .permissions: permissions
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
            if !editor.error.isEmpty {
                Label(editor.error, systemImage: "exclamationmark.circle.fill")
                    .font(.callout).foregroundStyle(.orange).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 28).padding(.bottom, 16)
            }
            Divider()
            HStack(spacing: 12) {
                Button("稍后设置") {
                    FirstLaunch.saveProgress(step); model.delegate?.deferSetup()
                }.buttonStyle(.plain).foregroundStyle(.secondary).keyboardShortcut(.cancelAction)
                Spacer()
                HStack(spacing: 6) {
                    ForEach(SetupStep.allCases, id: \.rawValue) { item in
                        Circle().fill(item == step ? Color.accentColor : Color.secondary.opacity(0.25)).frame(width: 6, height: 6)
                    }
                }.accessibilityElement(children: .ignore).accessibilityLabel("第 \(step.rawValue + 1) 步，共 3 步")
                Spacer()
                if step != .welcome { Button("返回") { move(to: step == .permissions ? .model : .welcome) } }
                Button(step == .permissions ? "开始使用" : "继续", action: advance)
                    .buttonStyle(.borderedProminent).controlSize(.large).keyboardShortcut(.defaultAction)
                    .disabled(step == .permissions && !(model.accessibilityGranted && model.screenRecordingGranted))
            }.padding(20).background(.bar)
        }.frame(width: 600, height: 580).background(Color(nsColor: .windowBackgroundColor))
            .preferredColorScheme(model.colorScheme)
            .onAppear {
                model.refreshPermissions()
                if step != .welcome { editor.load(includeVoice: false) }
            }
            .onReceive(refresh) { _ in if step == .permissions { model.refreshPermissions() } }
    }
    private var welcome: some View {
        VStack(spacing: 26) {
            Spacer(minLength: 8)
            Brand(size: 72)
            VStack(spacing: 10) {
                Text("欢迎使用 OpenGCUA").font(.largeTitle.weight(.bold))
                Text("把电脑上的事情，交给一句话。")
                    .font(.title3).foregroundStyle(.secondary)
            }.multilineTextAlignment(.center)
            VStack(alignment: .leading, spacing: 22) {
                benefit("macwindow", "为你操作 Mac", "理解任务、查看界面，完成应用里的操作。")
                benefit("bubble.left.and.bubble.right", "随时沟通", "用文字或语音补充要求，暂停或继续任务。")
                benefit("lock.shield", "由你掌握", "自行选择模型服务，并授权需要的系统权限。")
            }.frame(maxWidth: 390).padding(.top, 2)
            Spacer(minLength: 8)
        }.padding(32)
    }
    private func benefit(_ symbol: String, _ title: String, _ detail: String) -> some View {
        HStack(alignment: .center, spacing: 16) {
            Image(systemName: symbol).font(.system(size: 26)).foregroundStyle(.tint).frame(width: 36)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Text(detail).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
    private func heading(_ title: String, subtitle: String, symbol: String) -> some View {
        VStack(spacing: 10) {
            Image(systemName: symbol).font(.system(size: 32)).foregroundStyle(.tint).symbolRenderingMode(.hierarchical)
            Text(title).font(.title.weight(.bold))
            Text(subtitle).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }.frame(maxWidth: .infinity).padding(.top, 28).padding(.horizontal, 28).padding(.bottom, 12)
    }
    private var permissions: some View {
        VStack(spacing: 0) {
            heading("允许 OpenGCUA 操作 Mac", subtitle: "两项权限让它能看见界面，并替你点击和输入。", symbol: "hand.raised")
            Form {
                Section {
                    PermissionStatusRow(title: "辅助功能", detail: "点击、输入和操作应用", symbol: "cursorarrow", granted: model.accessibilityGranted)
                    PermissionStatusRow(title: "屏幕录制", detail: "查看任务相关的界面", symbol: "rectangle.inset.filled", granted: model.screenRecordingGranted)
                }
                Section {
                    Button("打开拖拽授权引导…") {
                        do { try editor.persist(includeVoice: false); MacPermissions.showGuide(model: model) }
                        catch { editor.error = error.localizedDescription }
                    }.frame(maxWidth: .infinity, alignment: .center)
                } footer: { Text("请授权 OpenGCUA 本身。首次查看屏幕时，macOS 还可能额外确认直接访问屏幕。麦克风权限将在使用语音时请求。") }
            }.formStyle(.grouped)
        }
    }
    private func move(to next: SetupStep) {
        editor.error = ""
        if next == .model && step == .welcome { editor.load(includeVoice: false) }
        step = next; FirstLaunch.saveProgress(next)
    }
    private func advance() {
        do {
            switch step {
            case .welcome: move(to: .model)
            case .model:
                try editor.validateModel(); try editor.persist(includeVoice: false)
                model.refreshPermissions(); move(to: .permissions)
            case .permissions:
                try editor.validateModel(); model.refreshPermissions()
                guard model.accessibilityGranted && model.screenRecordingGranted else { return }
                try editor.persist(includeVoice: false); model.delegate?.completeSetup()
            }
        } catch { editor.error = error.localizedDescription }
    }
}
