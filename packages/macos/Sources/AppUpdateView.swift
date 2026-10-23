import AppKit
import SwiftUI

/// A quiet status row; the installation decision uses a system-owned NSAlert.
struct AppUpdateStatus: View {
    @ObservedObject var updater: AppUpdater
    var taskActive: Bool
    var body: some View {
        if updater.showsPrompt, let release = updater.available {
            HStack(spacing: 12) {
                if updater.busy { ProgressView().controlSize(.small) }
                else { Image(systemName: "arrow.down.circle").foregroundStyle(.secondary).accessibilityHidden(true) }
                VStack(alignment: .leading, spacing: 3) {
                    Text(L("OpenGCUA %@ 可用", release.version))
                    if updater.busy || updater.isError {
                        Text(updater.message).font(.caption).foregroundStyle(updater.isError ? .orange : .secondary)
                    } else if taskActive {
                        Text(L("请在当前任务结束后安装更新。")).font(.caption).foregroundStyle(.secondary)
                    }
                }.fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Button(L("查看更新…")) { updater.requestPrompt() }.disabled(updater.busy || taskActive)
            }.controlSize(.small).padding(.vertical, 6)
        }
    }
}

@MainActor enum AppUpdateDialog {
    static func make(release: AppUpdateRelease, currentVersion: String, ready: Bool = false,
                     error: String? = nil, icon: NSImage? = nil) -> NSAlert {
        let alert = NSAlert()
        alert.alertStyle = error == nil ? .informational : .warning
        if let icon { alert.icon = icon }
        alert.messageText = error == nil ? L("OpenGCUA 有可用更新") : L("无法安装更新")
        alert.informativeText = error ?? (L("OpenGCUA %@ 现已推出，你当前使用的是 %@。", release.version, currentVersion)
            + "\n\n" + L("安装后将重新启动 OpenGCUA。"))
        alert.addButton(withTitle: ready ? L("安装并重新启动") : L("安装更新")).keyEquivalent = "\r"
        alert.addButton(withTitle: L("稍后")).keyEquivalent = "\u{1b}"
        if !ready { alert.addButton(withTitle: L("跳过此版本")) }
        let notes = NSTextField(labelWithAttributedString: NSAttributedString(string: L("发行说明"), attributes: [
            .link: release.releaseURL, .font: NSFont.systemFont(ofSize: NSFont.smallSystemFontSize),
        ]))
        notes.isSelectable = true; notes.allowsEditingTextAttributes = true
        notes.alignment = .center
        notes.setFrameSize(NSSize(width: 260, height: 18))
        alert.accessoryView = notes
        return alert
    }
}
