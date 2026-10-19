import AppKit
import ApplicationServices
import CoreGraphics
import SwiftUI

@MainActor enum MacPermissions {
    private static var guide: NSWindow?
    static func localizeGuide() { guide?.title = L("允许 OpenGCUA 控制这台 Mac") }
    static func closeGuide() { guide?.orderOut(nil) }
    static func showGuide(model: AppModel) {
        if guide == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 500, height: 470), styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = L("允许 OpenGCUA 控制这台 Mac")
            window.isReleasedWhenClosed = false
            window.level = .floating
            window.contentView = NSHostingView(rootView: PermissionGuide(model: model))
            window.center()
            guide = window
        }
        model.refreshPermissions()
        guide?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
    static var accessibility: Bool { AXIsProcessTrusted() }
    static var screenRecording: Bool { CGPreflightScreenCaptureAccess() }
    static func requestAccessibility() {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
    }
    static func requestScreenRecording() {
        _ = CGRequestScreenCaptureAccess()
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!)
    }
}

// A file URL on the native dragging pasteboard lets System Settings accept the
// actual running bundle, including when it lives outside /Applications.
final class DraggableApplicationIcon: NSImageView, NSDraggingSource {
    override func mouseDown(with event: NSEvent) {}
    override func mouseDragged(with event: NSEvent) {
        let item = NSDraggingItem(pasteboardWriter: Bundle.main.bundleURL as NSURL)
        item.setDraggingFrame(bounds, contents: image)
        beginDraggingSession(with: [item], event: event, source: self)
    }
    func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation { .copy }
}

struct ApplicationDragIcon: NSViewRepresentable {
    func makeNSView(context: Context) -> DraggableApplicationIcon {
        let view = DraggableApplicationIcon()
        view.image = NSWorkspace.shared.icon(forFile: Bundle.main.bundlePath)
        view.imageScaling = .scaleProportionallyUpOrDown
        view.toolTip = L("将 OpenGCUA.app 拖到系统设置的权限列表")
        view.setAccessibilityLabel(L("OpenGCUA.app，可拖到系统权限列表"))
        return view
    }
    func updateNSView(_ view: DraggableApplicationIcon, context: Context) {
        view.toolTip = L("将 OpenGCUA.app 拖到系统设置的权限列表")
        view.setAccessibilityLabel(L("OpenGCUA.app，可拖到系统权限列表"))
    }
}

struct PermissionGuide: View {
    @ObservedObject var model: AppModel
    private let refresh = Timer.publish(every: 2, on: .main, in: .common).autoconnect()
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 8) {
                Text(L("授权 OpenGCUA")).font(.title2.weight(.bold))
                Text(L("打开系统设置，将下方图标拖入权限列表，然后开启开关。")).font(.callout).foregroundStyle(.secondary)
            }
            HStack(spacing: 16) {
                ApplicationDragIcon().frame(width: 64, height: 64)
                VStack(alignment: .leading, spacing: 5) {
                    Text("OpenGCUA.app").font(.headline)
                    Text(L("拖动左侧图标加入权限列表")).font(.caption).foregroundStyle(.secondary)
                    Button(L("在 Finder 中显示")) { NSWorkspace.shared.activateFileViewerSelecting([Bundle.main.bundleURL]) }.buttonStyle(.link)
                }
            }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            permissionRow(L("辅助功能"), detail: L("用于点击、输入和操作应用"), granted: model.accessibilityGranted, action: MacPermissions.requestAccessibility)
            permissionRow(L("屏幕录制"), detail: L("用于查看你指定的任务界面"), granted: model.screenRecordingGranted, action: MacPermissions.requestScreenRecording)
            Text(L("请添加 OpenGCUA.app 本身。如果系统要求重新打开应用，请选择重新打开。此窗口会自动检查授权状态。")).font(.caption).foregroundStyle(.secondary)
            Divider()
            HStack {
                Label(model.accessibilityGranted && model.screenRecordingGranted ? L("权限已就绪") : L("等待系统授权"), systemImage: model.accessibilityGranted && model.screenRecordingGranted ? "checkmark.circle.fill" : "clock")
                    .foregroundStyle(model.accessibilityGranted && model.screenRecordingGranted ? Color.green : .secondary)
                Spacer()
                Button(model.accessibilityGranted && model.screenRecordingGranted ? L("完成") : L("关闭")) { MacPermissions.closeGuide() }.keyboardShortcut(.cancelAction)
            }.font(.callout)
        }.padding(24).frame(width: 500).fixedSize(horizontal: false, vertical: true).environment(\.locale, L10n.locale)
            .onReceive(refresh) { _ in model.refreshPermissions() }
            .onChange(of: model.accessibilityGranted && model.screenRecordingGranted) { _, granted in
                if granted { MacPermissions.closeGuide() }
            }
    }
    func permissionRow(_ title: String, detail: String, granted: Bool, action: @escaping () -> Void) -> some View {
        HStack(spacing: 12) {
            Image(systemName: granted ? "checkmark.circle.fill" : "circle").foregroundStyle(granted ? Color.green : .secondary)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.body.weight(.medium))
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button(L("打开设置"), action: action).accessibilityLabel(L("打开%@设置", String(describing: title)))
        }.padding(.vertical, 5)
    }
}
