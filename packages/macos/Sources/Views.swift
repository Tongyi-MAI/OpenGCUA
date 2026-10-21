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
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Brand(size: 28)
                VStack(alignment: .leading, spacing: 3) {
                    Text("OpenGCUA").font(.headline)
                    Text(model.phase == .idle ? L("你的电脑助手") : model.conversationOpen && !model.phase.active ? L("随时继续对话") : model.phase.label)
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Spacer()
                if model.phase != .idle {
                    TaskControls(model: model)
                    if !model.phase.active && !model.conversationOpen {
                        SymbolButton(symbol: "square.and.pencil", title: L("新对话")) { model.reset() }
                    }
                }
                SymbolButton(symbol: "gearshape", title: L("设置")) { model.delegate?.showSettings() }
            }.padding(18)
            SessionPicker(model: model).padding(.horizontal, 18).padding(.bottom, 10)
            Divider()
            if model.disconnected {
                HStack { Text(L("连接已断开")); Spacer(); Button(L("重新连接")) { model.connect() } }
                    .font(.caption).foregroundStyle(.secondary).padding(.horizontal, 18).padding(.top, 10)
            }
            ScrollViewReader { scroll in
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        SessionControlPanel(model: model)
                        ForEach(model.controls.approvals) { approval in
                            ApprovalCard(model: model, approval: approval).id(approval.id)
                        }
                        if model.phase == .idle {
                            VStack(spacing: 10) {
                                Image(systemName: "bubble.left.and.bubble.right").font(.system(size: 32, weight: .light)).foregroundStyle(.secondary)
                                Text(L("有什么需要我来做？")).font(.title3.weight(.semibold))
                                Text(L("发一条消息，或开始语音对话。")).font(.callout).foregroundStyle(.secondary)
                            }.frame(maxWidth: .infinity).padding(.vertical, 28)
                        } else {
                            ChatTranscript(messages: model.conversationMessages)
                            if model.phase.active {
                                if model.phase != .waiting {
                                    HStack(spacing: 8) {
                                        if model.taskProgressIndeterminate { ProgressView().controlSize(.small) }
                                        else { Image(systemName: model.taskProgressSymbol) }
                                        Text(model.taskProgressTitle)
                                            .font(.system(size: 12)).foregroundStyle(.secondary).textSelection(.enabled)
                                    }.padding(.horizontal, 14).padding(.vertical, 11)
                                        .background(Color(nsColor: .quaternaryLabelColor).opacity(0.4), in: RoundedRectangle(cornerRadius: 18))
                                }
                            } else if !model.runPath.isEmpty {
                                Button(L("查看运行记录"), systemImage: "folder") { model.revealRun(model.runPath) }
                                    .buttonStyle(.borderless).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        ExecutionDetailsView(model: model)
                        Color.clear.frame(height: 1).id("latest")
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(18)
                }.scrollIndicators(.never)
                    .onAppear { if model.phase != .idle { scroll.scrollTo("latest", anchor: .bottom) } }
                    .onChange(of: model.conversationMessages.count) { _, _ in
                        withAnimation(.easeOut(duration: 0.2)) { scroll.scrollTo("latest", anchor: .bottom) }
                    }
                    .onChange(of: model.phase) { _, _ in scroll.scrollTo("latest", anchor: .bottom) }
                    .onChange(of: model.controls.approvals.map(\.id)) { _, ids in
                        if let id = ids.first { scroll.scrollTo(id, anchor: .top) }
                    }
            }
            VStack(alignment: .leading, spacing: 10) {
                if !model.error.isEmpty {
                    Label(L(model.error), systemImage: "exclamationmark.circle").font(.system(size: 12)).foregroundStyle(.orange).textSelection(.enabled)
                }
                if model.restoresHistory {
                    Text(L("将恢复对话历史；Python 变量与界面引用会重新建立。"))
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
                InputComposer(model: model)
                HStack {
                    SymbolButton(symbol: "clock.arrow.circlepath", title: L("对话记录")) { model.delegate?.showHistory() }
                    Spacer()
                    SymbolButton(symbol: "rectangle.bottomthird.inset.filled", title: L("打开小窗")) { model.delegate?.collapse() }
                    Menu {
                        Button(L("设置…")) { model.delegate?.showSettings() }
                        Button(L("显示进度气泡")) { model.delegate?.showProgressBubble() }
                        Button(L("隐藏进度气泡")) { model.delegate?.hideProgressBubble() }
                        Button(L("Mac 控制权限…")) { MacPermissions.showGuide(model: model) }
                        Button(L("打开对话记录文件夹")) { NSWorkspace.shared.open(model.dataDirectory) }
                        Divider()
                        Button(L("退出 OpenGCUA")) { NSApp.terminate(nil) }.keyboardShortcut("q")
                    } label: { Image(systemName: "ellipsis") }.menuStyle(.borderlessButton).fixedSize().accessibilityLabel(L("更多"))
                }.font(.system(size: 11)).foregroundStyle(.secondary)
            }.padding(.horizontal, 18).padding(.bottom, 16).padding(.top, 8)
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(MaterialView(material: .popover)).preferredColorScheme(model.colorScheme).environment(\.locale, L10n.locale)
            .onChange(of: model.phase) { _, _ in model.delegate?.refreshMainSize() }
    }
}

struct ChatBubble: View {
    let text: String
    var isUser = false
    var body: some View {
        HStack(alignment: .bottom, spacing: 0) {
            if isUser { Spacer(minLength: 44) }
            Text(text).font(.system(size: 14)).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14).padding(.vertical, 10)
                .foregroundStyle(isUser ? Color.white : Color.primary)
                .background(isUser ? Color.blue : Color(nsColor: .quaternaryLabelColor).opacity(0.55),
                            in: UnevenRoundedRectangle(topLeadingRadius: 18, bottomLeadingRadius: isUser ? 18 : 5,
                                                       bottomTrailingRadius: isUser ? 5 : 18, topTrailingRadius: 18))
            if !isUser { Spacer(minLength: 44) }
        }.frame(maxWidth: .infinity, alignment: isUser ? .trailing : .leading)
            .accessibilityElement(children: .combine).accessibilityLabel((isUser ? L("你：") : L("OpenGCUA：")) + text)
    }
}

struct ChatTranscript: View {
    let messages: [ConversationMessage]
    var body: some View {
        ForEach(messages) { message in ChatBubble(text: message.text, isUser: message.isUser) }
    }
}

struct TaskControls: View {
    @ObservedObject var model: AppModel
    var compact = false
    var body: some View {
        HStack(spacing: 4) {
            if model.canInterrupt {
                if model.controls.interruptPending {
                    Menu {
                        Button(L("强制中止（重置 worker）")) { model.interruptTurn(force: true) }
                    } label: {
                        if compact { Image(systemName: "bolt.slash").frame(width: 24, height: 24) }
                        else { Text(L("正在中止本轮…")) }
                    }.menuStyle(.borderlessButton).fixedSize().font(.caption).accessibilityLabel(L("正在中止本轮…"))
                } else if compact {
                    SymbolButton(symbol: "bolt.slash", title: L("中止本轮")) { model.interruptTurn() }
                } else {
                    Button(L("中止本轮"), systemImage: "bolt.slash") { model.interruptTurn() }
                        .buttonStyle(.borderless).font(.caption).help(L("中止当前轮次，保留会话以发送纠正；不会撤销已执行的操作。"))
                }
            }
            if model.phase == .running {
                SymbolButton(symbol: "pause.fill", title: L("暂停任务")) { model.command("pause") }
            } else if model.phase == .paused || model.phase == .pausing {
                SymbolButton(symbol: "play.fill", title: L("继续任务")) { model.command("resume") }
            }
            if model.phase.active {
                SymbolButton(symbol: "stop.fill", title: L("停止任务")) { model.command("stop") }.disabled(model.phase == .stopping)
            } else if model.conversationOpen {
                SymbolButton(symbol: "square.and.pencil", title: L("新对话")) { model.reset() }.disabled(model.sendingMessage)
            }
        }
    }
}
struct QuestionView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ScrollView { ChatBubble(text: model.question) }.frame(maxHeight: 100)
            InputComposer(model: model)
        }
    }
}
struct CompactView: View {
    @ObservedObject var model: AppModel
    var orbAtBottom = true
    var body: some View {
        Group {
            if model.voiceMode { VoiceBar(model: model, orbAtBottom: orbAtBottom) }
            else { compactContent }
        }
    }
    var compactContent: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.phase.active || model.controls.interrupted ? model.taskProgressTitle : model.phase.label).font(.system(size: 12, weight: .semibold)).foregroundStyle(model.tint).lineLimit(1)
                    Text(model.phase == .idle ? L("有什么可以交给我？") : model.taskTitle)
                        .font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                }.frame(maxWidth: .infinity, alignment: .leading).frame(minHeight: 30).overlay(WindowDragHandle())
                SymbolButton(symbol: "arrow.up.left.and.arrow.down.right", title: L("展开任务")) { model.delegate?.showMain() }
                SymbolButton(symbol: "xmark", title: L("关闭小窗")) { model.delegate?.closeCompact() }
                    .help(L("隐藏小窗，电脑任务继续执行；可从菜单栏重新打开"))
                if !orbAtBottom { Color.clear.frame(width: 42, height: 32).allowsHitTesting(false) }
            }
            if !model.foregroundWarning.isEmpty {
                Text(model.foregroundWarning).font(.system(size: 10)).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true).help(model.foregroundWarning)
            }
            if model.phase.active && !model.currentIntent.isEmpty {
                Text("Intent: " + model.currentIntent).font(.system(size: 11)).lineLimit(2)
                    .help(model.currentIntent).textSelection(.enabled)
            }
            if model.showsCompactResult { CompactResultView(text: model.compactResultText, height: 84).id(model.taskID) }
            else if model.phase == .waiting {
                ScrollView { ChatBubble(text: model.question) }.frame(height: 84)
            } else if model.phase.active && model.currentIntent.isEmpty {
                Text(model.taskProgressDetail).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(2)
                    .help(model.taskProgressDetail).frame(maxWidth: .infinity, alignment: .leading)
            }
            RecoveryNoticeView(model: model, compact: true)
            if !model.controls.approvals.isEmpty {
                Button(L("查看前台审批（默认拒绝）")) { model.delegate?.showMain() }.font(.caption)
            }
            if !model.compactYieldsToAutomation { InputComposer(model: model, compact: true) }
            if !model.controls.result.isEmpty { Text(model.controls.result).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1).help(model.controls.result) }
            if !model.error.isEmpty { Text(L(model.error)).font(.system(size: 10)).foregroundStyle(.orange).lineLimit(2).help(model.error) }
            Spacer(minLength: 0)
            HStack(spacing: 6) {
                if model.step > 0 { Text(L("第 %@ 步", String(model.step))).font(.system(size: 10)).foregroundStyle(.secondary).monospacedDigit() }
                TaskControls(model: model, compact: true)
                Menu {
                    Button(L("设置…")) { model.delegate?.showSettings() }
                    if !model.runPath.isEmpty { Button(L("查看运行记录")) { model.revealRun(model.runPath) } }
                    Divider()
                    Button(L("退出 OpenGCUA")) { NSApp.terminate(nil) }
                } label: { Image(systemName: "ellipsis") }.menuStyle(.borderlessButton).fixedSize().accessibilityLabel(L("任务菜单"))
                Spacer(minLength: 0)
                if orbAtBottom { Color.clear.frame(width: 42).allowsHitTesting(false) }
            }.frame(height: orbAtBottom ? 32 : 24)
        }.padding(10).frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(MaterialView(material: .popover)).clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).stroke(Color(nsColor: .separatorColor).opacity(0.5)))
            .preferredColorScheme(model.colorScheme).environment(\.locale, L10n.locale)
            .onChange(of: model.error) { _, _ in model.delegate?.refreshCompact() }
    }
}

/// Keep the full terminal answer in the panel, including paragraphs longer than its viewport.
struct CompactResultView: View {
    var text: String
    var height: CGFloat = 120
    var body: some View {
        ScrollView {
            ChatBubble(text: text)
                .padding(.vertical, 2)
        }.frame(height: height).accessibilityLabel(L("任务最终结果"))
    }
}

struct InputComposer: View {
    @ObservedObject var model: AppModel
    var compact = false
    @FocusState private var inputFocused: Bool
    private var statusText: String {
        if !model.speechStatus.isEmpty { return L(model.speechStatus) }
        if model.recording { return L("正在听 · 点击麦克风结束") }
        return model.messageStatus
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: 6) {
                TextField(model.inputPlaceholder, text: Binding(get: { model.inputText }, set: { model.inputText = $0 }), axis: .vertical)
                    .textFieldStyle(.plain).font(.system(size: 13)).lineLimit(1...(compact ? 1 : 2))
                    .frame(minHeight: 25, alignment: .center)
                    .accessibilityLabel(L("消息输入"))
                    .disabled(model.recording || model.speechPending || model.sendingMessage)
                    .focused($inputFocused)
                    .background(TaskInputShortcut(focused: inputFocused, sendWithEnter: model.preferences.sendWithEnter,
                                                  canSend: model.canSendInput, send: { model.sendInput() }).allowsHitTesting(false))
                SymbolButton(symbol: model.recording ? "stop.circle.fill" : model.speechPending ? "ellipsis.circle" : "mic", title: L("语音对话")) {
                    model.toggleSpeech(interaction: true)
                }.foregroundStyle(model.recording ? Color.red : Color.secondary)
                    .disabled(model.sendingMessage || model.phase == .stopping)
                Button { model.sendInput() } label: {
                    Image(systemName: "arrow.up").font(.system(size: 12, weight: .semibold)).frame(width: 25, height: 25)
                }.buttonStyle(.plain).foregroundStyle(model.canSendInput ? .white : .secondary)
                    .background(model.canSendInput ? Color.accentColor : Color.primary.opacity(0.06), in: Circle())
                    .disabled(!model.canSendInput).keyboardShortcut(.return, modifiers: .command)
                    .help(model.preferences.sendShortcutHint).accessibilityLabel(model.phase == .waiting ? L("发送回复") : model.phase.active ? L("发送补充要求") : model.conversationOpen || model.restoresHistory ? L("继续对话") : L("发送消息"))
            }.padding(9).background(Color(nsColor: .textBackgroundColor).opacity(0.7), in: RoundedRectangle(cornerRadius: 20))
                .overlay(RoundedRectangle(cornerRadius: 20).stroke(.primary.opacity(0.12)))
            if !compact && !statusText.isEmpty {
                Text(statusText)
                    .font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                    .help(model.messageStatus)
            }
        }
    }
}
