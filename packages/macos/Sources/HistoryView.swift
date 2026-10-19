import AppKit
import SwiftUI

struct HistoryView: View {
    @ObservedObject var model: AppModel
    var onClose: () -> Void = {}
    @ViewState private var selection: String?
    private var selected: TaskRecord? { model.history.first { $0.id == selection } }
    var body: some View {
        VStack(spacing: 0) {
            NavigationSplitView {
                List(model.history, selection: $selection) { record in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: TaskPhase(rawValue: record.state)?.symbol ?? "checkmark.circle")
                            .foregroundStyle(.secondary).frame(width: 16).padding(.top, 2)
                        VStack(alignment: .leading, spacing: 5) {
                            Text(record.title).font(.body.weight(.medium)).lineLimit(2)
                            Text(record.date, style: .date).font(.caption).foregroundStyle(.secondary)
                        }
                    }.padding(.vertical, 5).tag(record.id)
                }.listStyle(.sidebar).navigationTitle(L("对话记录"))
                    .navigationSplitViewColumnWidth(min: 180, ideal: 220, max: 280)
            } detail: {
                if let record = selected {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 20) {
                            VStack(alignment: .leading, spacing: 10) {
                                Text(record.title).font(.title2.weight(.semibold)).textSelection(.enabled)
                                Label(TaskPhase(rawValue: record.state)?.label ?? record.state,
                                      systemImage: TaskPhase(rawValue: record.state)?.symbol ?? "circle")
                                    .font(.callout).foregroundStyle(.secondary)
                            }
                            Divider()
                            if let messages = record.messages, !messages.isEmpty {
                                ChatTranscript(messages: messages)
                            } else {
                                ChatBubble(text: record.title, isUser: true)
                                ChatBubble(text: record.answer.isEmpty ? L("尚无最终结果") : record.answer)
                            }
                            HStack {
                                Button(record.id == model.currentConversationID ? L("返回当前对话") : L("继续对话"), systemImage: "bubble.left.and.bubble.right") {
                                    if model.openConversation(record) { onClose(); model.delegate?.expand() }
                                }.buttonStyle(.borderedProminent).disabled(!model.canOpenConversation(record))
                                if model.phase.active && record.id != model.currentConversationID {
                                    Text(L("先停止当前任务即可切换")).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            if !model.error.isEmpty { Text(L(model.error)).font(.caption).foregroundStyle(.orange).textSelection(.enabled) }
                            if !record.path.isEmpty { Button(L("查看运行记录"), systemImage: "folder") { model.revealRun(record.path) } }
                        }.padding(28)
                    }
                } else {
                    ContentUnavailableView(model.history.isEmpty ? L("还没有对话记录") : L("选择一个对话"), systemImage: "clock.arrow.circlepath",
                                           description: Text(L("对话会保存在这台 Mac。")))
                }
            }
            Divider()
            HStack {
                Button(L("打开记录文件夹"), systemImage: "folder") { NSWorkspace.shared.open(model.dataDirectory) }
                Spacer()
                Button(L("完成"), action: onClose).keyboardShortcut(.cancelAction)
            }.padding(16)
        }.frame(minWidth: 640, idealWidth: 720, minHeight: 440, idealHeight: 500)
            .preferredColorScheme(model.colorScheme).environment(\.locale, L10n.locale)
            .onAppear { selection = model.history.first?.id }
    }
}
