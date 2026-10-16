import Carbon
import Foundation

/// Owned and registered on the app's main thread. Uses the system hot-key API,
/// not a keyboard event tap, so it does not require Input Monitoring permission.
final class GlobalShortcut {
    static let display = "⌃⌥Space"
    private static let signature: OSType = 0x476E7374 // Gnst
    private var hotKey: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private var action: (@MainActor () -> Void)?

    func register(action: @escaping @MainActor () -> Void) -> OSStatus {
        unregister()
        self.action = action
        // Trigger once on release; holding Space must not repeatedly open/close UI.
        var event = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased))
        let installed = InstallEventHandler(GetApplicationEventTarget(), { _, event, context in
            guard let event, let context else { return OSStatus(eventNotHandledErr) }
            var id = EventHotKeyID()
            let result = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                                          nil, MemoryLayout<EventHotKeyID>.size, nil, &id)
            guard result == noErr, id.signature == GlobalShortcut.signature, id.id == 1 else {
                return OSStatus(eventNotHandledErr)
            }
            let shortcut = Unmanaged<GlobalShortcut>.fromOpaque(context).takeUnretainedValue()
            Task { @MainActor [weak shortcut] in shortcut?.action?() }
            return noErr
        }, 1, &event, Unmanaged.passUnretained(self).toOpaque(), &handler)
        guard installed == noErr else { unregister(); return installed }
        let result = RegisterEventHotKey(UInt32(kVK_Space), UInt32(controlKey | optionKey),
                                         EventHotKeyID(signature: Self.signature, id: 1), GetApplicationEventTarget(),
                                         OptionBits(kEventHotKeyExclusive), &hotKey)
        if result != noErr { unregister() }
        return result
    }

    func unregister() {
        if let hotKey { UnregisterEventHotKey(hotKey) }
        if let handler { RemoveEventHandler(handler) }
        hotKey = nil; handler = nil; action = nil
    }
    deinit { unregister() }
}
