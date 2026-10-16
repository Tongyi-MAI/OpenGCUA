import AppKit
import SwiftUI

enum TaskReturnAction { case passThrough, send, newline }

enum TaskReturnKey {
    static func action(keyCode: UInt16, modifiers: NSEvent.ModifierFlags,
                       sendWithEnter: Bool, composing: Bool) -> TaskReturnAction {
        guard [36, 76].contains(keyCode), !composing else { return .passThrough }
        let modifiers = modifiers.intersection([.command, .shift, .control, .option])
        if modifiers == .command { return .send }
        if modifiers == .shift { return .newline }
        if modifiers.isEmpty { return sendWithEnter ? .send : .newline }
        return .passThrough
    }
}

/// SwiftUI's multiline field handles Return before normal button shortcuts.
/// Monitor only this focused input's window, retaining its native text editor.
struct TaskInputShortcut: NSViewRepresentable {
    var focused: Bool
    var sendWithEnter: Bool
    var canSend: Bool
    var send: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        context.coordinator.view = view
        context.coordinator.monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak coordinator = context.coordinator] event in
            guard let coordinator else { return event }
            return coordinator.handle(event)
        }
        return view
    }
    func updateNSView(_ view: NSView, context: Context) { context.coordinator.owner = self }
    static func dismantleNSView(_ view: NSView, coordinator: Coordinator) {
        if let monitor = coordinator.monitor { NSEvent.removeMonitor(monitor) }
        coordinator.monitor = nil
    }
    final class Coordinator {
        var owner: TaskInputShortcut
        weak var view: NSView?
        var monitor: Any?
        init(_ owner: TaskInputShortcut) { self.owner = owner }
        func handle(_ event: NSEvent) -> NSEvent? {
            guard owner.focused, let window = view?.window, event.window === window,
                  let editor = window.firstResponder as? NSTextView, editor.isEditable else { return event }
            switch TaskReturnKey.action(keyCode: event.keyCode, modifiers: event.modifierFlags,
                                       sendWithEnter: owner.sendWithEnter, composing: editor.hasMarkedText()) {
            case .passThrough: return event
            case .send:
                if owner.canSend && !event.isARepeat { owner.send() }
                return nil
            case .newline:
                editor.insertNewlineIgnoringFieldEditor(nil)
                return nil
            }
        }
    }
}
