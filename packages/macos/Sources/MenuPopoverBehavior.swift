import AppKit

/// Keep menu bar content out of Mission Control / Show Desktop arrangements.
@MainActor final class MenuPopoverBehavior {
    private var observations: [NSObjectProtocol] = []
    private weak var window: NSWindow?
    private var wasVisible = false
    private let center: NotificationCenter
    private let workspaceCenter: NotificationCenter
    private let dismiss: () -> Void
    init(center: NotificationCenter = .default,
         workspaceCenter: NotificationCenter = NSWorkspace.shared.notificationCenter,
         dismiss: @escaping () -> Void) {
        self.center = center; self.workspaceCenter = workspaceCenter; self.dismiss = dismiss
    }
    func attach(to window: NSWindow) {
        detach()
        self.window = window
        window.collectionBehavior = window.collectionBehavior.subtracting([.managed, .stationary]).union(.transient)
        wasVisible = window.occlusionState.contains(.visible)
        observations.append(center.addObserver(forName: NSWindow.didChangeOcclusionStateNotification, object: window, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.visibilityChanged() }
        })
        observations.append(center.addObserver(forName: NSApplication.willResignActiveNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.dismiss() }
        })
        observations.append(workspaceCenter.addObserver(forName: NSWorkspace.activeSpaceDidChangeNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.dismiss() }
        })
    }
    func visibilityChanged() {
        guard let window else { return }
        let visible = window.occlusionState.contains(.visible)
        // Settings and the JSON file picker may fully cover their parent.
        // They are still part of this interaction, not a desktop transition.
        if wasVisible && !visible && window.attachedSheet == nil { dismiss() }
        wasVisible = visible
    }
    func detach() {
        for observation in observations {
            center.removeObserver(observation)
            workspaceCenter.removeObserver(observation)
        }
        observations = []; window = nil; wasVisible = false
    }
}
