// Minimal model and detail fixtures; the hosting, orb, hover state and resize code are production views.
import AppKit
import SwiftUI
enum TaskPhase { case idle, running, paused, waiting, completed, failed, stopped
    var active: Bool { self == .running || self == .paused || self == .waiting }
    var label: String { "Ready" }
}
@MainActor final class AppDelegate { func expand() {} }
@MainActor final class AppModel: ObservableObject {
    var phase = TaskPhase.idle, voiceMode = false, recording = false, speechPending = false
    var tint = Color.blue, speechStatus = "Ready", taskProgressDetail = ""
    var colorScheme: ColorScheme? = .light
    var delegate: AppDelegate?
}
struct CompactView: View {
    var model: AppModel
    var orbAtBottom = true
    var body: some View { RoundedRectangle(cornerRadius: 18).fill(.regularMaterial).overlay(Text("Details")) }
}
@main enum MiniWindowLayoutContracts {
    @MainActor static func main() async throws {
        _ = NSApplication.shared
        var samples = 0
        for upward in [true, false] {
            let model = AppModel(), presentation = MiniWindowPresentation(enterDelay: 0, exitDelay: 0)
            let screen = NSRect(x: 0, y: 0, width: 1512, height: 982)
            let anchor = NSPoint(x: 652, y: upward ? 200 : 900)
            presentation.expandsUpward = upward
            let initial = MiniWindowPlacement.fit(anchor: anchor, expanded: false, detailHeight: 156, screen: screen).frame
            let host = NSHostingView(rootView: MiniWindowView(model: model, presentation: presentation))
            host.sizingOptions = []
            let panel = FloatingPanel(contentRect: initial, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            panel.contentView = host
            presentation.update(requested: true, suppressed: false)
            host.layoutSubtreeIfNeeded()
            try await Task.sleep(nanoseconds: 30_000_000)
            func orb(_ view: NSView) -> OrbHandleView? {
                if let found = view as? OrbHandleView { return found }
                return view.subviews.lazy.compactMap { orb($0) }.first
            }
            func verify(_ label: String) {
                guard let handle = orb(host) else { preconditionFailure("Missing actual orb interaction view") }
                let frame = panel.convertToScreen(handle.convert(handle.bounds, to: nil))
                precondition(abs(frame.minX - initial.minX) < 0.5 && abs(frame.minY - initial.minY) < 0.5,
                             "Badge jumped during \(label): \(frame), expected \(initial)")
                precondition(frame.size == initial.size)
                samples += 1
            }
            verify("initial layout")
            presentation.onChange = {
                let placement = MiniWindowPlacement.fit(anchor: anchor, expanded: presentation.expanded, detailHeight: 156, screen: screen)
                presentation.expandsUpward = placement.expandsUpward
                panel.setPresentationFrame(placement.frame)
                verify("the synchronous resize, before the next render turn")
            }
            for iteration in 0..<4 {
                let expanded = iteration.isMultiple(of: 2)
                presentation.hover(expanded)
                for _ in 0..<20 {
                    try await Task.sleep(nanoseconds: 10_000_000)
                    verify("the detail opacity animation")
                }
                precondition(presentation.expanded == expanded)
            }
        }
        print("Mini window badge remained at the same screen coordinate across \(samples) immediate and animation-frame checks, above and below the anchor")
    }
}
