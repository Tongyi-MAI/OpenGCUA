import AppKit

@main enum TaskInputContracts {
    @MainActor static func main() {
        _ = NSApplication.shared
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 300, height: 100),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let root = NSView(frame: window.contentView!.bounds)
        let anchor = NSView(frame: .zero)
        let editor = NSTextView(frame: root.bounds)
        root.addSubview(editor); root.addSubview(anchor); window.contentView = root
        precondition(window.makeFirstResponder(editor))
        var sends = 0
        let handler = TaskInputShortcut.Coordinator(TaskInputShortcut(focused: true, sendWithEnter: true,
                                                                      canSend: true, send: { sends += 1 }))
        handler.view = anchor
        func key(_ code: UInt16 = 36, _ modifiers: NSEvent.ModifierFlags = [], repeatKey: Bool = false) -> NSEvent {
            NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: modifiers,
                            timestamp: 0, windowNumber: window.windowNumber, context: nil,
                            characters: "\r", charactersIgnoringModifiers: "\r", isARepeat: repeatKey, keyCode: code)!
        }
        precondition(handler.handle(key()) == nil && sends == 1)
        precondition(handler.handle(key(76)) == nil && sends == 2)
        precondition(handler.handle(key(repeatKey: true)) == nil && sends == 2)
        handler.owner.canSend = false
        precondition(handler.handle(key()) == nil && sends == 2)
        handler.owner.canSend = true
        handler.owner.focused = false
        precondition(handler.handle(key()) != nil && sends == 2)
        handler.owner.focused = true
        editor.isEditable = false
        precondition(handler.handle(key()) != nil && sends == 2)
        editor.isEditable = true
        precondition(handler.handle(key(36, .control)) != nil && sends == 2)
        precondition(handler.handle(key(0)) != nil && sends == 2)
        // Shift+Enter inserts at the native selection instead of appending text.
        editor.string = "ab"; editor.setSelectedRange(NSRange(location: 1, length: 0))
        precondition(handler.handle(key(36, .shift)) == nil && sends == 2)
        precondition(editor.string == "a\nb")
        handler.owner.sendWithEnter = false
        editor.string = "ab"; editor.setSelectedRange(NSRange(location: 1, length: 0))
        precondition(handler.handle(key()) == nil && editor.string == "a\nb" && sends == 2)
        precondition(handler.handle(key(36, .command)) == nil && sends == 3)
        // The IME owns Return until marked text has been committed.
        editor.setMarkedText("拼", selectedRange: NSRange(location: 1, length: 0),
                             replacementRange: NSRange(location: NSNotFound, length: 0))
        precondition(editor.hasMarkedText())
        precondition(handler.handle(key(36, .command)) != nil && sends == 3)
        handler.owner.sendWithEnter = true
        precondition(handler.handle(key()) != nil && sends == 3)
        editor.unmarkText()
        let otherWindow = NSWindow(contentRect: .zero, styleMask: [], backing: .buffered, defer: false)
        otherWindow.isReleasedWhenClosed = false
        handler.view = otherWindow.contentView
        precondition(handler.handle(key()) != nil && sends == 3)
        print("Task input Return, keypad Enter, newline, focus, repeat, disabled-send and IME contracts passed")
    }
}
