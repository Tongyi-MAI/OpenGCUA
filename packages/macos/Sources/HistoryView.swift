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
                }.listStyle(.sidebar).navigationTitle("任务记录")
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
                            Text(record.answer.isEmpty ? "尚无最终结果" : record.answer)
                                .font(.body).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                            if !record.path.isEmpty { Button("查看运行记录", systemImage: "folder") { model.revealRun(record.path) } }
                        }.padding(28)
                    }
                } else {
                    ContentUnavailableView(model.history.isEmpty ? "还没有任务记录" : "选择一个任务", systemImage: "clock.arrow.circlepath",
                                           description: Text("任务结果会保存在这台 Mac。"))
                }
            }
            Divider()
            HStack {
                Button("打开记录文件夹", systemImage: "folder") { NSWorkspace.shared.open(model.dataDirectory) }
                Spacer()
                Button("完成", action: onClose).keyboardShortcut(.cancelAction)
            }.padding(16)
        }.frame(minWidth: 640, idealWidth: 720, minHeight: 440, idealHeight: 500)
            .preferredColorScheme(model.colorScheme)
            .onAppear { selection = model.history.first?.id }
    }
}
