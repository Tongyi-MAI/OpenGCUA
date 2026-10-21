import AppKit

/// 独立主窗口不依赖菜单栏位置；自动避让时恢复窗口但不抢走键盘焦点。
@MainActor final class MainWindow: NSWindow {
    private(set) var requested = false
    private var hiddenForAutomation = false

    init(content: NSView) {
        super.init(contentRect: NSRect(x: 0, y: 0, width: 640, height: 760),
                   styleMask: [.titled, .closable, .miniaturizable, .resizable],
                   backing: .buffered, defer: false)
        title = "OpenGCUA"
        isReleasedWhenClosed = false
        contentMinSize = NSSize(width: 480, height: 600)
        contentView = content
        center()
    }

    func present(suppressed: Bool) {
        requested = true
        if suppressed {
            hiddenForAutomation = true
            orderOut(nil)
            return
        }
        hiddenForAutomation = false
        if isMiniaturized { deminiaturize(nil) }
        makeKeyAndOrderFront(nil)
    }

    func hide() {
        requested = false
        hiddenForAutomation = false
        orderOut(nil)
    }

    func updateOcclusion(_ suppressed: Bool) {
        if suppressed {
            if requested && isVisible && !isMiniaturized {
                hiddenForAutomation = true
                orderOut(nil)
            }
        } else if requested && hiddenForAutomation {
            hiddenForAutomation = false
            orderFront(nil)
        }
    }
}
