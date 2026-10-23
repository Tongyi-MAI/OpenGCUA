import AppKit
import SwiftUI

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSPopoverDelegate {
    let model = AppModel()
    let updater = AppUpdater()
    var panel: FloatingPanel?
    var status: NSStatusItem!
    var popover: NSPopover?
    private var mainWindow: MainWindow?
    var compact = false
    let miniPresentation = MiniWindowPresentation()
    private var miniAnchor: NSPoint?
    private var refreshingMini = false
    private var miniMenuTracking = false
    private var miniMenuObservers: [NSObjectProtocol] = []
    var quitting = false
    private let globalShortcut = GlobalShortcut()
    private var shortcutResultCode = 0
    var mainSize: NSSize { NSSize(width: 480, height: model.phase == .idle ? 600 : 680) }
    func refreshMainSize() {
        guard let popover else { return }
        popover.contentSize = mainSize
    }
    var screenObserver: NSObjectProtocol?
    private var setupWindow: NSWindow?
    private var settingsWindow: NSWindow?
    private var historyWindow: NSWindow?
    private var updateAlert: NSAlert?
    private lazy var menuBehavior = MenuPopoverBehavior { [weak self] in self?.dismissMenuPopover() }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        model.delegate = self
        miniPresentation.onChange = { [weak self] in self?.refreshCompact() }
        miniPresentation.keepExpanded = { [weak self] in
            guard let self, let panel = self.panel else { return false }
            return self.miniMenuTracking || panel.editingText || panel.frame.contains(NSEvent.mouseLocation)
        }
        let center = NotificationCenter.default
        for (name, tracking) in [(NSMenu.didBeginTrackingNotification, true), (NSMenu.didEndTrackingNotification, false)] {
            miniMenuObservers.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in
                    guard let self else { return }
                    self.miniMenuTracking = tracking
                    if !tracking { self.miniPresentation.hover(self.panel?.frame.contains(NSEvent.mouseLocation) == true) }
                }
            })
        }
        let menu = NSMenu(), root = NSMenuItem(title: "OpenGCUA", action: nil, keyEquivalent: ""), commands = NSMenu(title: "OpenGCUA")
        commands.addItem(withTitle: L("打开 OpenGCUA"), action: #selector(showMain), keyEquivalent: "1")
        commands.addItem(withTitle: L("呼出 / 收起 OpenGCUA（⌃⌥Space）"), action: #selector(togglePresentation), keyEquivalent: "")
        commands.addItem(withTitle: L("设置…"), action: #selector(showSettings), keyEquivalent: ",")
        commands.addItem(withTitle: L("检查更新…"), action: #selector(checkForUpdates), keyEquivalent: "")
        commands.addItem(withTitle: L("对话记录…"), action: #selector(showHistory), keyEquivalent: "")
        commands.addItem(withTitle: L("Mac 控制权限…"), action: #selector(showPermissions), keyEquivalent: "")
        commands.addItem(.separator())
        commands.addItem(withTitle: L("隐藏主界面，显示进度气泡"), action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        commands.addItem(withTitle: L("显示进度气泡"), action: #selector(showProgressBubble), keyEquivalent: "")
        commands.addItem(withTitle: L("隐藏进度气泡"), action: #selector(hideProgressBubble), keyEquivalent: "")
        commands.addItem(.separator())
        commands.addItem(withTitle: L("退出 OpenGCUA"), action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        root.submenu = commands; menu.addItem(root)
        // Preserve normal macOS text editing and keyboard shortcuts in hosted fields.
        let editRoot = NSMenuItem(), edit = NSMenu(title: L("编辑"))
        edit.addItem(withTitle: L("撤销"), action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: L("剪切"), action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: L("复制"), action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: L("粘贴"), action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: L("全选"), action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editRoot.submenu = edit; menu.addItem(editRoot); NSApp.mainMenu = menu
        status = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        status.button?.title = "OG"
        status.button?.imagePosition = .imageLeading
        status.button?.image = AppBrand.menuBarImage(description: "OpenGCUA")
        status.button?.toolTip = L("OpenGCUA · ⌃⌥Space 呼出 / 收起")
        status.button?.target = self; status.button?.action = #selector(statusClick)
        let popover = NSPopover(); popover.behavior = .transient; popover.delegate = self
        popover.contentSize = mainSize
        let content = NSHostingController(rootView: MainView(model: model, updater: updater)); content.sizingOptions = []
        popover.contentViewController = content; self.popover = popover
        screenObserver = NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.clampPanel() }
        }
        // Later launches stay in the menu bar; unfinished setup opens directly.
        model.connect()
        let shortcutResult = globalShortcut.register { [weak self] in self?.togglePresentation() }
        shortcutResultCode = Int(shortcutResult)
        if shortcutResult != 0 {
            model.globalShortcutStatus = L("⌃⌥Space 未启用（%@）。请检查快捷键冲突后重新启动。", String(describing: shortcutResult))
            status.button?.toolTip = L("OpenGCUA · 全局快捷键未启用，请查看设置")
        }
        if FirstLaunch.needsSetup() { showSetup() } else { showMain() }
        updater.canInstall = { [weak self] in
            guard let self else { return false }
            return !self.quitting && !self.model.phase.active && !self.model.recording
        }
        updater.onRelaunch = { NSApp.terminate(nil) }
        updater.onChange = { [weak self] in
            guard let self else { return }
            self.status.button?.title = self.updater.showsPrompt ? "OG •" : "OG"
            self.presentPendingUpdate()
        }
        updater.start()
    }
    func languageChanged() {
        let titles = ["showMain": "打开 OpenGCUA", "togglePresentation": "呼出 / 收起 OpenGCUA（⌃⌥Space）",
                      "showSettings": "设置…", "checkForUpdates": "检查更新…", "showHistory": "对话记录…", "showPermissions": "Mac 控制权限…",
                      "hide:": "隐藏主界面，显示进度气泡", "showProgressBubble": "显示进度气泡", "hideProgressBubble": "隐藏进度气泡",
                      "terminate:": "退出 OpenGCUA", "undo:": "撤销", "cut:": "剪切", "copy:": "复制", "paste:": "粘贴", "selectAll:": "全选"]
        func update(_ menu: NSMenu) {
            for item in menu.items {
                if let action = item.action, let key = titles[NSStringFromSelector(action)] { item.title = L(key) }
                if let child = item.submenu { update(child) }
            }
        }
        if let menu = NSApp.mainMenu {
            update(menu)
            if menu.items.count > 1 { menu.items[1].title = L("编辑"); menu.items[1].submenu?.title = L("编辑") }
        }
        historyWindow?.title = L("对话记录"); settingsWindow?.title = L("OpenGCUA 设置")
        setupWindow?.title = L("欢迎使用 OpenGCUA"); panel?.title = L("OpenGCUA · 执行状态")
        status?.button?.toolTip = shortcutResultCode == 0 ? L("OpenGCUA · ⌃⌥Space 呼出 / 收起") : L("OpenGCUA · 全局快捷键未启用，请查看设置")
        model.globalShortcutStatus = shortcutResultCode == 0 ? L("⌃⌥Space · 呼出 / 收起") : L("⌃⌥Space 未启用（%@）。请检查快捷键冲突后重新启动。", String(shortcutResultCode))
        MacPermissions.localizeGuide()
    }
    @objc func showHistory() {
        dismissMenuPopover(); model.stopSpeech()
        if historyWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 500), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            window.title = L("对话记录"); window.isReleasedWhenClosed = false; window.delegate = self
            window.contentMinSize = NSSize(width: 640, height: 440)
            window.contentView = NSHostingView(rootView: HistoryView(model: model, onClose: { [weak self] in self?.historyWindow?.orderOut(nil) }))
            window.center(); historyWindow = window
        }
        historyWindow?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    @objc func showMain() {
        // Dock、应用菜单和重新打开均走独立窗口，不需要可见的菜单栏按钮。
        dismissMenuPopover()
        closeCompact()
        miniPresentation.allowBubble()
        if NSApp.isHidden { NSApp.unhideWithoutActivation() }
        if mainWindow == nil {
            let content = NSHostingView(rootView: MainView(model: model, updater: updater))
            content.sizingOptions = []
            let window = MainWindow(content: content)
            window.delegate = self
            mainWindow = window
        }
        let suppressed = model.compactHidesForComputerAction
        if !suppressed { NSApp.activate(ignoringOtherApps: true) }
        mainWindow?.present(suppressed: suppressed)
    }
    @objc func showSettings() {
        dismissMenuPopover(); model.stopSpeech()
        if settingsWindow?.isVisible == true {
            settingsWindow?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return
        }
        // Recreate the editor from saved preferences; Cancel discards the draft.
        if settingsWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 550), styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = L("OpenGCUA 设置"); window.isReleasedWhenClosed = false; window.delegate = self
            window.center(); settingsWindow = window
        }
        settingsWindow?.contentView = NSHostingView(rootView: SettingsView(model: model, updater: updater, onClose: { [weak self] in self?.settingsWindow?.orderOut(nil) }))
        settingsWindow?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    @objc func checkForUpdates() { showSettings(); updater.check() }
    func applicationDidBecomeActive(_ notification: Notification) { presentPendingUpdate() }
    func windowDidBecomeKey(_ notification: Notification) { presentPendingUpdate() }
    private func presentPendingUpdate() {
        guard updateAlert == nil, updater.showsPrompt, !updater.busy,
              !quitting, !model.phase.active, !model.recording, NSApp.isActive,
              let release = updater.available, let window = NSApp.keyWindow,
              window.isVisible, window.attachedSheet == nil,
              window === mainWindow || window === settingsWindow
                || window === popover?.contentViewController?.view.window else { return }
        let alert = AppUpdateDialog.make(release: release, currentVersion: updater.currentVersion,
                                        ready: updater.state == .ready,
                                        error: updater.isError ? updater.message : nil, icon: AppBrand.icon)
        updateAlert = alert
        alert.beginSheetModal(for: window) { [weak self] response in
            Task { @MainActor in
                guard let self else { return }
                guard self.updater.available == release else {
                    self.updateAlert = nil; self.presentPendingUpdate(); return
                }
                // Keep the prompt retained while the updater changes state.
                switch response {
                case .alertFirstButtonReturn: self.updater.install()
                case .alertThirdButtonReturn: self.updater.skipVersion()
                default: self.updater.remindLater()
                }
                self.updateAlert = nil
            }
        }
    }
    func showSetup() {
        dismissMenuPopover()
        if setupWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 600, height: 580),
                                  styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = L("欢迎使用 OpenGCUA")
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
        if mainWindow?.isVisible == true && mainWindow?.isMiniaturized != true {
            collapse(automatically: true)
            return
        }
        if popover?.isShown == true || panel?.isVisible == true {
            closeCompact()
            popover?.performClose(nil)
        } else { expand() }
    }
    func expand() {
        if mainWindow?.requested == true { showMain(); return }
        if setupWindow?.isVisible == true { showSetup(); return }
        model.stopSpeech(); compact = false; miniPresentation.update(requested: false, suppressed: false); panel?.orderOut(nil)
        guard status?.isVisible == true, let button = status?.button,
              button.window?.screen != nil, let popover else { showMain(); return }
        NSApp.activate(ignoringOtherApps: true)
        refreshMainSize()
        if !popover.isShown { popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY) }
        popover.contentViewController?.view.window?.makeKey()
    }
    @objc func showProgressBubble() { collapse() }
    @objc func hideProgressBubble() { closeCompact() }

    func applicationWillHide(_ notification: Notification) {
        mainWindow?.hide()
        settingsWindow?.orderOut(nil)
        historyWindow?.orderOut(nil)
        setupWindow?.orderOut(nil)
        MacPermissions.closeGuide()
        collapse(automatically: true)
    }
    func applicationDidHide(_ notification: Notification) {
        // 系统隐藏会连 NSPanel 一起隐藏；只恢复气泡，不恢复主窗口或激活应用。
        guard compact, !quitting else { return }
        NSApp.unhideWithoutActivation()
        refreshCompact()
    }
    func windowDidMiniaturize(_ notification: Notification) {
        if notification.object as? NSWindow === mainWindow { collapse(automatically: true) }
    }

    func collapse(automatically: Bool = false) {
        if !automatically { miniPresentation.allowBubble() }
        if !model.voiceMode { model.stopSpeech() }
        compact = !miniPresentation.manuallyHidden
        popover?.performClose(nil)
        mainWindow?.hide()
        guard compact else { refreshCompact(); return }
        if panel == nil {
            let p = FloatingPanel(contentRect: NSRect(x: 0, y: 0, width: FloatingPanel.iconSize, height: FloatingPanel.iconSize), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            p.title = L("OpenGCUA · 执行状态")
            p.isFloatingPanel = true; p.level = .floating; p.hidesOnDeactivate = false
            p.isOpaque = false; p.backgroundColor = .clear; p.hasShadow = true
            p.isMovableByWindowBackground = true; p.isReleasedWhenClosed = false
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            let content = NSHostingView(rootView: MiniWindowView(model: model, presentation: miniPresentation)); content.sizingOptions = []
            p.contentView = content; p.delegate = self
            let bounds = (status.button?.window?.screen ?? NSScreen.main)?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
            if let saved = UserDefaults.standard.array(forKey: "floatingOrbAnchor") as? [Double], saved.count == 2 {
                miniAnchor = NSPoint(x: saved[0], y: saved[1])
            } else if let saved = UserDefaults.standard.array(forKey: "floatingAnchor") as? [Double], saved.count == 2 {
                miniAnchor = NSPoint(x: saved[0] + FloatingPanel.compactWidth, y: saved[1] - 110)
            } else {
                miniAnchor = NSPoint(x: bounds.maxX - 24, y: bounds.minY + 26)
            }
            p.onHover = { [weak self] in self?.miniPresentation.hover($0) }
            p.onDrag = { [weak self] active in
                guard let self else { return }
                self.miniPresentation.drag(active)
                if !active {
                    self.rememberMiniPosition()
                    self.miniPresentation.hover(self.panel?.frame.contains(NSEvent.mouseLocation) == true)
                }
            }
            p.onCancelDetails = { [weak self] in self?.miniPresentation.dismiss() }
            panel = p
        }
        if NSApp.isHidden && compact { NSApp.unhideWithoutActivation() }
        refreshCompact()
    }
    func closeCompact() {
        // Closing the presentation must not send a stop command to the task runtime.
        compact = false
        model.stopSpeech()
        miniPresentation.hideBubble()
        panel?.orderOut(nil)
    }
    func refreshCompact() {
        presentPendingUpdate()
        let description = "OpenGCUA · \(model.phase.label)"
        switch model.phase {
        case .waiting:
            status?.button?.image = NSImage(systemSymbolName: "bubble.left.and.bubble.right", accessibilityDescription: description)
        case .failed:
            status?.button?.image = NSImage(systemSymbolName: "exclamationmark.circle", accessibilityDescription: description)
        default:
            status?.button?.image = AppBrand.menuBarImage(description: description)
        }
        mainWindow?.updateOcclusion(model.compactHidesForComputerAction)
        guard let panel, !refreshingMini else { return }
        refreshingMini = true; defer { refreshingMini = false }
        miniPresentation.setPointerOperation(model.overlayPointerBusy)
        miniPresentation.update(requested: compact, suppressed: model.compactHidesForComputerAction)
        if panel.dragging {
            panel.updatePresentation(requested: compact, automationActive: model.compactHidesForComputerAction)
            return
        }
        let anchor = miniAnchor ?? NSPoint(x: panel.frame.maxX, y: panel.frame.minY)
        let iconRect = NSRect(x: anchor.x - FloatingPanel.iconSize, y: anchor.y, width: FloatingPanel.iconSize, height: FloatingPanel.iconSize)
        guard let screen = NSScreen.screens.first(where: { $0.visibleFrame.intersects(iconRect) }) ?? NSScreen.main else { return }
        let placement = MiniWindowPlacement.fit(anchor: anchor, expanded: miniPresentation.expanded,
                                                detailHeight: model.compactPanelHeight, screen: screen.visibleFrame,
                                                badgeWidth: FloatingPanel.progressWidth, badgeHeight: model.progressBubbleHeight)
        if miniPresentation.expandsUpward != placement.expandsUpward { miniPresentation.expandsUpward = placement.expandsUpward }
        panel.detailsExpanded = miniPresentation.expanded
        if !panel.detailsExpanded && panel.isKeyWindow { panel.makeFirstResponder(nil); panel.resignKey() }
        if panel.frame != placement.frame { panel.setPresentationFrame(placement.frame) }
        panel.updatePresentation(requested: compact, automationActive: model.compactHidesForComputerAction)
        // Hover and task updates never activate the app or take keyboard focus.
    }
    func clampPanel() { refreshCompact() }
    func overlayWouldObstruct(_ event: [String: Any]) -> Bool {
        guard let primary = NSScreen.screens.first,
              event["impact"] as? String == "pointer",
              let points = event["points"] as? [[String: Any]] else { return false }
        let windows: [NSWindow] = [panel, mainWindow].compactMap { $0 }
        return windows.contains { window in
            guard window.isVisible, !window.isMiniaturized else { return false }
            let frame = NSRect(x: window.frame.minX, y: primary.frame.maxY - window.frame.maxY,
                               width: window.frame.width, height: window.frame.height)
            return OverlayOcclusion.intersects(points: points, frame: frame)
        }
    }
    private func rememberMiniPosition() {
        guard let panel else { return }
        miniAnchor = NSPoint(x: panel.frame.maxX,
                             y: !miniPresentation.expandsUpward ? panel.frame.maxY - FloatingPanel.iconSize : panel.frame.minY)
        UserDefaults.standard.set([miniAnchor!.x, miniAnchor!.y], forKey: "floatingOrbAnchor")
    }
    func windowDidMove(_ notification: Notification) {
        if let p = notification.object as? FloatingPanel, p.dragging { rememberMiniPosition() }
    }
    func windowDidResignKey(_ notification: Notification) {
        // The agent changes the foreground app while the visible panel is listening.
        // Keep that explicit recording alive; hiding/closing the panel stops it.
        if notification.object is FloatingPanel { miniPresentation.hover(panel?.frame.contains(NSEvent.mouseLocation) == true) }
        else if !model.voiceMode { model.stopSpeech() }
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
        else if sender === mainWindow { collapse(automatically: true) }
        else { model.stopSpeech(); sender.orderOut(nil) }
        return false
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMain()
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
        updater.stop()
        globalShortcut.unregister()
        menuBehavior.detach()
        model.stopSpeech(); model.bridge.shutdown()
        miniPresentation.update(requested: false, suppressed: true)
        for observer in miniMenuObservers { NotificationCenter.default.removeObserver(observer) }
        if let screenObserver { NotificationCenter.default.removeObserver(screenObserver) }
        if let status { NSStatusBar.system.removeStatusItem(status) }
    }
}

@main enum OpenGCUAApp {
    @MainActor static func main() async {
        if CommandLine.arguments.dropFirst().first == "--install-update" {
            exit(await AppUpdateInstaller.runHelper(CommandLine.arguments))
        }
        let app = NSApplication.shared, delegate = AppDelegate()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
