import AppKit
import SwiftUI
import UniformTypeIdentifiers

// Explicit property-wrapper alias also supports SDKs that export a State macro.
typealias ViewState<Value> = SwiftUI.State<Value>

struct MaterialView: NSViewRepresentable {
    var material: NSVisualEffectView.Material = .sidebar
    func makeNSView(context: Context) -> NSVisualEffectView {
        let v = NSVisualEffectView(); v.material = material; v.blendingMode = .behindWindow; v.state = .active; return v
    }
    func updateNSView(_ view: NSVisualEffectView, context: Context) { view.material = material }
}
struct SymbolButton: View {
    var symbol: String; var title: String; var action: () -> Void
    var body: some View {
        Button(action: action) { Image(systemName: symbol).frame(width: 24, height: 24) }
            .buttonStyle(.borderless).help(title).accessibilityLabel(title)
    }
}
struct MainView: View {
    @ObservedObject var model: AppModel
    @FocusState private var inputFocused: Bool
    var canStartTask: Bool { !model.recording && !model.speechPending && model.ready && !model.instruction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 10) {
                Brand(size: 26)
                VStack(alignment: .leading, spacing: 2) {
                    Text("OpenGCUA").font(.headline)
                }
                Spacer()
                SymbolButton(symbol: "gearshape", title: "设置") { model.delegate?.showSettings() }
            }
            if model.disconnected {
                HStack { Text("连接已断开").foregroundStyle(.secondary); Spacer(); Button("重新连接") { model.connect() } }.font(.caption)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if model.phase == .idle {
                        VStack(alignment: .leading, spacing: 7) {
                            Text("有什么需要我来做？").font(.title3.weight(.semibold))
                            Text("描述任务，或开始语音对话。").font(.callout).foregroundStyle(.secondary)
                        }
                        composer
                    } else {
                        taskDetail
                        if !model.phase.active {
                            Button("新任务", systemImage: "plus") { model.reset() }.buttonStyle(.borderedProminent)
                        }
                    }
                    if !model.error.isEmpty {
                        Label(model.error, systemImage: "exclamationmark.circle").font(.system(size: 12)).foregroundStyle(.orange).textSelection(.enabled)
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 2)
            }.scrollIndicators(.hidden)
            Divider()
            HStack {
                Button("任务记录", systemImage: "clock.arrow.circlepath") { model.delegate?.showHistory() }.buttonStyle(.borderless)
                Spacer()
                Button("打开小窗", systemImage: "rectangle.bottomthird.inset.filled") { model.delegate?.collapse() }.buttonStyle(.borderless)
                Menu {
                    Button("设置…") { model.delegate?.showSettings() }
                    Button("Mac 控制权限…") { MacPermissions.showGuide(model: model) }
                    Button("打开任务记录文件夹") { NSWorkspace.shared.open(model.dataDirectory) }
                    Divider()
                    Button("退出 OpenGCUA") { NSApp.terminate(nil) }.keyboardShortcut("q")
                } label: { Image(systemName: "ellipsis") }.menuStyle(.borderlessButton).fixedSize().accessibilityLabel("更多")
            }.font(.system(size: 12)).foregroundStyle(.secondary)
        }.padding(18).frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(MaterialView(material: .popover)).preferredColorScheme(model.colorScheme)
            .onChange(of: model.phase) { _, _ in model.delegate?.refreshMainSize() }
    }
    var composer: some View {
        VStack(alignment: .leading, spacing: 8) {
            VStack(alignment: .leading, spacing: 12) {
                TextField("描述你想完成的事情…", text: $model.instruction, axis: .vertical)
                    .textFieldStyle(.plain).font(.body).lineLimit(3...5).accessibilityLabel("任务指令").disabled(model.recording || model.speechPending)
                    .focused($inputFocused)
                    .background(TaskInputShortcut(focused: inputFocused, sendWithEnter: model.preferences.sendWithEnter,
                                                  canSend: canStartTask, send: { model.start() }).allowsHitTesting(false))
                HStack {
                    Button { model.toggleSpeech() } label: {
                        Label("语音对话", systemImage: model.recording ? "waveform" : "mic")
                    }.buttonStyle(.borderless).foregroundStyle(model.recording ? Color.accentColor : .secondary).help("切换到独立语音条，可以直接对话、下发或停止任务")
                    Spacer()
                    Button("开始任务", systemImage: "arrow.up") { model.start() }.buttonStyle(.borderedProminent)
                        .keyboardShortcut(.return, modifiers: .command)
                        .disabled(!canStartTask).help(model.preferences.sendShortcutHint)
                }
                if !model.speechStatus.isEmpty { Text(model.speechStatus).font(.caption).foregroundStyle(.secondary) }
            }.padding(15).background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
                .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color(nsColor: .separatorColor).opacity(0.5)))
            Text(model.preferences.sendShortcutHint).font(.caption).foregroundStyle(.secondary)
        }
    }
    var taskDetail: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(model.taskTitle).font(.title3.weight(.semibold)).textSelection(.enabled)
            Label(model.phase.label, systemImage: model.phase.symbol).foregroundStyle(model.tint)
            if model.phase.active {
                Text(model.intent.isEmpty ? "正在准备运行环境…" : model.intent).font(.system(size: 13)).foregroundStyle(.secondary).textSelection(.enabled)
                if model.phase == .waiting { QuestionView(model: model) }
                else { InputComposer(model: model) }
                HStack {
                    TaskControls(model: model)
                    Spacer()
                    Button("收起为小窗", systemImage: "arrow.down.right.and.arrow.up.left") { model.delegate?.collapse() }
                }
            } else {
                Text(model.answer).font(.system(size: 14)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                if !model.runPath.isEmpty { Button("查看运行记录", systemImage: "folder") { model.revealRun(model.runPath) } }
            }
        }
    }
}

struct TaskControls: View {
    @ObservedObject var model: AppModel
    var body: some View {
        HStack(spacing: 4) {
            if model.phase == .running {
                SymbolButton(symbol: "pause.fill", title: "暂停任务") { model.command("pause") }
            } else if model.phase == .paused || model.phase == .pausing {
                SymbolButton(symbol: "play.fill", title: "继续任务") { model.command("resume") }
            }
            if model.phase.active {
                SymbolButton(symbol: "stop.fill", title: "停止任务") { model.command("stop") }.disabled(model.phase == .stopping)
            }
        }
    }
}
struct QuestionView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ScrollView { Text(model.question).font(.system(size: 13)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }.frame(maxHeight: 100)
            InputComposer(model: model)
        }
    }
}
struct CompactView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        Group {
            if model.voiceMode { VoiceBar(model: model) }
            else { compactContent }
        }
    }
    var compactContent: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Brand(size: 24)
                VStack(alignment: .leading, spacing: 4) {
                    Text(model.phase.label).font(.system(size: 13, weight: .semibold)).foregroundStyle(model.tint).lineLimit(1)
                    Text(model.phase == .idle ? "有什么可以交给我？" : model.phase == .running ? model.status : model.taskTitle).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 0)
                TaskControls(model: model)
                SymbolButton(symbol: "arrow.up.left.and.arrow.down.right", title: "展开任务") { model.delegate?.expand() }
                SymbolButton(symbol: "xmark", title: "关闭小窗") { model.delegate?.closeCompact() }
                    .help("隐藏小窗，电脑任务继续执行；可从菜单栏重新打开")
            }
            if model.showsCompactResult { CompactResultView(text: model.compactResultText).id(model.taskID) }
            else if model.phase == .waiting { QuestionView(model: model) }
            else if [.starting, .running, .pausing, .stopping].contains(model.phase) {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small).scaleEffect(0.8)
                    Text(model.phase == .pausing ? "当前动作结束后暂停" : model.step > 0 ? "第 \(model.step) 步" : "正在准备")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
            } else {
                Text(model.phase == .idle ? "从这里开始一个新任务" : model.phase == .paused ? "点击继续即可恢复任务" : "展开查看运行记录")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            if model.phase != .waiting { InputComposer(model: model) }
            if !model.error.isEmpty { Text(model.error).font(.system(size: 11)).foregroundStyle(.orange).lineLimit(2).help(model.error) }
            Spacer(minLength: 0)
            HStack {
                Image(systemName: "line.3.horizontal").font(.system(size: 9)).accessibilityHidden(true)
                Text("OpenGCUA").font(.system(size: 10))
                Spacer()
                if !model.error.isEmpty { Image(systemName: "exclamationmark.circle").foregroundStyle(.orange).help(model.error) }
                Menu {
                    Button("展开任务") { model.delegate?.expand() }
                    Button("关闭小窗") { model.delegate?.closeCompact() }
                    Button("设置…") { model.delegate?.showSettings() }
                    if !model.runPath.isEmpty { Button("查看运行记录") { model.revealRun(model.runPath) } }
                    Divider()
                    Button("退出 OpenGCUA") { NSApp.terminate(nil) }
                } label: { Image(systemName: "ellipsis") }.menuStyle(.borderlessButton).fixedSize().accessibilityLabel("任务菜单")
            }.foregroundStyle(.secondary)
        }.padding(16).frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(MaterialView(material: .popover)).clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(Color(nsColor: .separatorColor).opacity(0.5)))
            .preferredColorScheme(model.colorScheme)
            .onChange(of: model.error) { _, _ in model.delegate?.refreshCompact() }
    }
}

/// Keep the full terminal answer in the panel, including paragraphs longer than its viewport.
struct CompactResultView: View {
    var text: String
    var body: some View {
        ScrollView {
            Text(text).font(.system(size: 13)).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 2)
        }.frame(height: 120).accessibilityLabel("任务最终结果")
    }
}

struct InputComposer: View {
    @ObservedObject var model: AppModel
    @FocusState private var inputFocused: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: 6) {
                TextField(model.inputPlaceholder, text: Binding(get: { model.inputText }, set: { model.inputText = $0 }), axis: .vertical)
                    .textFieldStyle(.plain).font(.system(size: 13)).lineLimit(1...2)
                    .frame(minHeight: 25, alignment: .center)
                    .accessibilityLabel("小窗输入")
                    .disabled(model.recording || model.speechPending || model.sendingMessage)
                    .focused($inputFocused)
                    .background(TaskInputShortcut(focused: inputFocused, sendWithEnter: model.preferences.sendWithEnter,
                                                  canSend: model.canSendInput, send: { model.sendInput() }).allowsHitTesting(false))
                SymbolButton(symbol: model.recording ? "stop.circle.fill" : model.speechPending ? "ellipsis.circle" : "mic", title: "语音对话") {
                    model.toggleSpeech(interaction: true)
                }.foregroundStyle(model.recording ? Color.red : Color.secondary)
                    .disabled(model.sendingMessage || model.phase == .stopping)
                Button { model.sendInput() } label: {
                    Image(systemName: "arrow.up").font(.system(size: 12, weight: .semibold)).frame(width: 25, height: 25)
                }.buttonStyle(.plain).foregroundStyle(model.canSendInput ? .white : .secondary)
                    .background(model.canSendInput ? Color.accentColor : Color.primary.opacity(0.06), in: Circle())
                    .disabled(!model.canSendInput).keyboardShortcut(.return, modifiers: .command)
                    .help(model.preferences.sendShortcutHint).accessibilityLabel(model.phase == .waiting ? "发送回复" : model.phase.active ? "发送补充要求" : "开始新任务")
            }.padding(9).background(Color(nsColor: .textBackgroundColor).opacity(0.7), in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(.primary.opacity(0.09)))
            Text(!model.speechStatus.isEmpty ? model.speechStatus : model.recording ? "正在听 · 点击麦克风结束" : !model.messageStatus.isEmpty ? model.messageStatus : model.preferences.sendShortcutHint)
                .font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                .help(model.messageStatus)
        }
    }
}
