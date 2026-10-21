import AppKit

final class VisibilityWindow: NSWindow {
    var exposed = false
    var testSheet: NSWindow?
    override var occlusionState: NSWindow.OcclusionState { exposed ? .visible : [] }
    override var attachedSheet: NSWindow? { testSheet }
}

@main enum PopoverContracts {
    @MainActor static func main() {
        _ = NSApplication.shared
        let window = VisibilityWindow(contentRect: .zero, styleMask: [], backing: .buffered, defer: false)
        let center = NotificationCenter(), workspace = NotificationCenter()
        var dismissals = 0
        let behavior = MenuPopoverBehavior(center: center, workspaceCenter: workspace) { dismissals += 1 }
        window.collectionBehavior = [.managed, .fullScreenAuxiliary]
        behavior.attach(to: window)
        precondition(window.collectionBehavior.contains(.transient) && !window.collectionBehavior.contains(.managed))
        precondition(window.collectionBehavior.contains(.fullScreenAuxiliary))
        center.post(name: NSWindow.didChangeOcclusionStateNotification, object: window)
        precondition(dismissals == 0, "Initial hidden window must not close a newly opening popover")
        window.exposed = true
        center.post(name: NSWindow.didChangeOcclusionStateNotification, object: window)
        window.testSheet = NSWindow(contentRect: .zero, styleMask: [], backing: .buffered, defer: false)
        window.exposed = false
        center.post(name: NSWindow.didChangeOcclusionStateNotification, object: window)
        precondition(dismissals == 0, "A settings sheet covering its parent must not close the popover")
        window.exposed = true
        window.testSheet = nil
        center.post(name: NSWindow.didChangeOcclusionStateNotification, object: window)
        window.exposed = false
        center.post(name: NSWindow.didChangeOcclusionStateNotification, object: window)
        precondition(dismissals == 1)
        center.post(name: NSApplication.willResignActiveNotification, object: nil)
        workspace.post(name: NSWorkspace.activeSpaceDidChangeNotification, object: nil)
        precondition(dismissals == 3)
        behavior.detach()
        center.post(name: NSApplication.willResignActiveNotification, object: nil)
        workspace.post(name: NSWorkspace.activeSpaceDidChangeNotification, object: nil)
        precondition(dismissals == 3, "Detached observers must not close subsequent presentations")
        behavior.attach(to: window)
        center.post(name: NSApplication.willResignActiveNotification, object: nil)
        precondition(dismissals == 4, "Reopening must install a single observer")
        behavior.detach()
        let main = MainWindow(content: NSView())
        precondition(main.styleMask.contains(.titled) && main.styleMask.contains(.closable))
        precondition(main.styleMask.contains(.miniaturizable) && main.styleMask.contains(.resizable))
        precondition(!main.isReleasedWhenClosed && main.level == .normal)
        precondition(main.contentMinSize == NSSize(width: 480, height: 600))
        precondition(!main.requested && !main.isVisible)
        main.present(suppressed: true)
        precondition(main.requested && !main.isVisible, "电脑操作期间打开 Dock 窗口不能遮挡目标")
        main.hide()
        main.updateOcclusion(false)
        precondition(!main.requested && !main.isVisible, "主动关闭的主窗口不能被自动恢复")
        print("Popover and independent Dock window lifecycle contracts passed")
    }
}
