import AppKit
import SwiftUI

@MainActor final class FloatingPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSPopoverDelegate {
    let model = AppModel()
    var panel: FloatingPanel?
    var status: NSStatusItem!
    var popover: NSPopover?
    var compact = false
    var quitting = false
    private let globalShortcut = GlobalShortcut()
    var mainSize: NSSize { NSSize(width: 420, height: model.phase == .idle ? 330 : 400) }
    func refreshMainSize() {
        guard let popover else { return }
        popover.contentSize = mainSize
    }
    var screenObserver: NSObjectProtocol?
    private var setupWindow: NSWindow?
    private var settingsWindow: NSWindow?
    private var historyWindow: NSWindow?
    private lazy var menuBehavior = MenuPopoverBehavior { [weak self] in self?.dismissMenuPopover() }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        model.delegate = self
        let menu = NSMenu(), root = NSMenuItem(), commands = NSMenu()
        commands.addItem(withTitle: "打开 OpenGCUA", action: #selector(showMain), keyEquivalent: "1")
        commands.addItem(withTitle: "呼出 / 收起 OpenGCUA（⌃⌥Space）", action: #selector(togglePresentation), keyEquivalent: "")
        commands.addItem(withTitle: "设置…", action: #selector(showSettings), keyEquivalent: ",")
        commands.addItem(withTitle: "任务记录…", action: #selector(showHistory), keyEquivalent: "")
        commands.addItem(withTitle: "Mac 控制权限…", action: #selector(showPermissions), keyEquivalent: "")
        commands.addItem(.separator())
        commands.addItem(withTitle: "退出 OpenGCUA", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        root.submenu = commands; menu.addItem(root)
        // Preserve normal macOS text editing and keyboard shortcuts in hosted fields.
        let editRoot = NSMenuItem(), edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editRoot.submenu = edit; menu.addItem(editRoot); NSApp.mainMenu = menu
        status = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        status.button?.image = AppBrand.menuBarImage(description: "OpenGCUA")
        status.button?.toolTip = "OpenGCUA · ⌃⌥Space 呼出 / 收起"
        status.button?.target = self; status.button?.action = #selector(statusClick)
        let popover = NSPopover(); popover.behavior = .transient; popover.delegate = self
        popover.contentSize = mainSize
        let content = NSHostingController(rootView: MainView(model: model)); content.sizingOptions = []
        popover.contentViewController = content; self.popover = popover
        screenObserver = NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.clampPanel() }
        }
        // Later launches stay in the menu bar; unfinished setup opens directly.
        model.connect()
        let shortcutResult = globalShortcut.register { [weak self] in self?.togglePresentation() }
        if shortcutResult != 0 {
            model.globalShortcutStatus = "⌃⌥Space 未启用（\(shortcutResult)）。请检查快捷键冲突后重新启动。"
            status.button?.toolTip = "OpenGCUA · 全局快捷键未启用，请查看设置"
        }
        if FirstLaunch.needsSetup() { showSetup() }
    }
    @objc func showHistory() {
        dismissMenuPopover(); model.stopSpeech()
        if historyWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 500), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            window.title = "任务记录"; window.isReleasedWhenClosed = false; window.delegate = self
            window.contentMinSize = NSSize(width: 640, height: 440)
            window.contentView = NSHostingView(rootView: HistoryView(model: model, onClose: { [weak self] in self?.historyWindow?.orderOut(nil) }))
            window.center(); historyWindow = window
        }
        historyWindow?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    @objc func showMain() { expand() }
    @objc func showSettings() {
        dismissMenuPopover(); model.stopSpeech()
        if settingsWindow?.isVisible == true {
            settingsWindow?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return
        }
        // Recreate the editor from saved preferences; Cancel discards the draft.
        if settingsWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 550), styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = "OpenGCUA 设置"; window.isReleasedWhenClosed = false; window.delegate = self
            window.center(); settingsWindow = window
        }
        settingsWindow?.contentView = NSHostingView(rootView: SettingsView(model: model, onClose: { [weak self] in self?.settingsWindow?.orderOut(nil) }))
        settingsWindow?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    func showSetup() {
        dismissMenuPopover()
        if setupWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 600, height: 580),
                                  styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = "欢迎使用 OpenGCUA"
            window.isReleasedWhenClosed = false; window.delegate = self
            window.center(); setupWindow = window
        }
        if setupWindow?.isVisible != true { setupWindow?.contentView = NSHostingView(rootView: OnboardingView(model: model)) }
        setupWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
    func deferSetup() { setupWindow?.orderOut(nil) }
    func completeSetup() {
        FirstLaunch.complete()
        setupWindow?.orderOut(nil)
        expand()
    }
    @objc func showPermissions() { MacPermissions.showGuide(model: model) }
    @objc func statusClick() {
        if compact { expand(); return }
        togglePresentation()
    }
    @objc func togglePresentation() {
        if popover?.isShown == true || panel?.isVisible == true {
            closeCompact()
            popover?.performClose(nil)
        } else { expand() }
    }
    func expand() {
        if setupWindow?.isVisible == true { showSetup(); return }
        model.stopSpeech(); compact = false; panel?.orderOut(nil)
        guard let button = status.button, let popover else { return }
        NSApp.activate(ignoringOtherApps: true)
        refreshMainSize()
        if !popover.isShown { popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY) }
        popover.contentViewController?.view.window?.makeKey()
    }
    func collapse() {
        if !model.voiceMode { model.stopSpeech() }; compact = true; popover?.performClose(nil)
        if panel == nil {
            let p = FloatingPanel(contentRect: NSRect(x: 0, y: 0, width: 360, height: 148), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            p.title = "OpenGCUA · 执行状态"
            p.isFloatingPanel = true; p.level = .floating; p.hidesOnDeactivate = false
            p.isOpaque = false; p.backgroundColor = .clear; p.hasShadow = true
            p.isMovableByWindowBackground = true; p.isReleasedWhenClosed = false
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            let content = NSHostingView(rootView: CompactView(model: model)); content.sizingOptions = []
            p.contentView = content; p.delegate = self
            let bounds = (status.button?.window?.screen ?? NSScreen.main)?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
            p.setFrameOrigin(NSPoint(x: bounds.maxX - 384, y: bounds.minY + 26))
            if let position = UserDefaults.standard.array(forKey: "floatingPosition") as? [Double], position.count == 2 {
                p.setFrameOrigin(NSPoint(x: position[0], y: position[1]))
            }
            panel = p
        }
        refreshCompact(); panel?.orderFrontRegardless()
    }
    func closeCompact() {
        // Closing the presentation must not send a stop command to the task runtime.
        compact = false
        model.stopSpeech()
        panel?.orderOut(nil)
    }
    func refreshCompact() {
        let description = "OpenGCUA · \(model.phase.label)"
        switch model.phase {
        case .waiting:
            status?.button?.image = NSImage(systemSymbolName: "bubble.left.and.bubble.right", accessibilityDescription: description)
        case .failed:
            status?.button?.image = NSImage(systemSymbolName: "exclamationmark.circle", accessibilityDescription: description)
        default:
            status?.button?.image = AppBrand.menuBarImage(description: description)
        }
        guard let panel else { return }
        let height = model.compactPanelHeight
        let old = panel.frame
        panel.setFrame(NSRect(x: old.minX, y: old.maxY - height, width: 360, height: height), display: true)
        clampPanel()
        // State changes never activate the app or take keyboard focus.
    }
    func clampPanel() {
        guard let panel, let screen = NSScreen.screens.first(where: { $0.visibleFrame.intersects(panel.frame) }) ?? NSScreen.main else { return }
        let b = screen.visibleFrame, f = panel.frame
        panel.setFrameOrigin(NSPoint(x: max(b.minX, min(f.minX, b.maxX - f.width)), y: max(b.minY, min(f.minY, b.maxY - f.height))))
    }
    func windowDidMove(_ notification: Notification) {
        if let p = notification.object as? FloatingPanel { UserDefaults.standard.set([p.frame.minX, p.frame.minY], forKey: "floatingPosition") }
    }
    func windowDidResignKey(_ notification: Notification) {
        // The agent changes the foreground app while the visible panel is listening.
        // Keep that explicit recording alive; hiding/closing the panel stops it.
        if !(notification.object is FloatingPanel) && !model.voiceMode { model.stopSpeech() }
    }
    func popoverDidClose(_ notification: Notification) {
        menuBehavior.detach()
        if !model.voiceMode { model.stopSpeech() }
        // Native dismissal (including Show Desktop) stays dismissed. Explicit
        // task start / small-window actions are the only routes to collapse().
    }
    func popoverDidShow(_ notification: Notification) {
        if let window = popover?.contentViewController?.view.window { menuBehavior.attach(to: window) }
    }
    func dismissMenuPopover() {
        guard popover?.isShown == true else { return }
        model.stopSpeech()
        popover?.performClose(nil)
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if sender === panel { closeCompact() }
        else { model.stopSpeech(); sender.orderOut(nil) }
        return false
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if FirstLaunch.needsSetup() { showSetup() } else { expand() }
        return true
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitting { return .terminateLater }
        quitting = true; model.stopSpeech()
        guard model.bridge.running else { return .terminateNow }
        model.bridge.shutdown()
        return .terminateLater
    }
    func runtimeExited() { if quitting { NSApp.reply(toApplicationShouldTerminate: true) } }
    func applicationWillTerminate(_ notification: Notification) {
        globalShortcut.unregister()
        menuBehavior.detach()
        model.stopSpeech(); model.bridge.shutdown()
        if let screenObserver { NotificationCenter.default.removeObserver(screenObserver) }
        if let status { NSStatusBar.system.removeStatusItem(status) }
    }
}

@main enum OpenGCUAApp {
    @MainActor static func main() {
        let app = NSApplication.shared, delegate = AppDelegate()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
