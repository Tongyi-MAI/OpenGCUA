import AppKit
import SwiftUI

@MainActor class FloatingPanel: NSPanel {
    static let compactWidth: CGFloat = 320
    static let iconSize: CGFloat = 44
    var detailsExpanded = false
    private(set) var dragging = false
    var onHover: ((Bool) -> Void)?
    var onDrag: ((Bool) -> Void)?
    var onCancelDetails: (() -> Void)?
    private var hoverArea: NSTrackingArea?
    override var contentView: NSView? {
        didSet {
            if let hoverArea { oldValue?.removeTrackingArea(hoverArea) }
            let area = NSTrackingArea(rect: .zero, options: [.activeAlways, .mouseEnteredAndExited, .inVisibleRect], owner: self, userInfo: nil)
            contentView?.addTrackingArea(area); hoverArea = area
        }
    }
    override func mouseEntered(with event: NSEvent) { onHover?(true) }
    override func mouseExited(with event: NSEvent) { onHover?(false) }
    override func cancelOperation(_ sender: Any?) { onCancelDetails?() }
    override func performDrag(with event: NSEvent) {
        beginDragging()
        defer { endDragging() }
        super.performDrag(with: event)
    }
    private func beginDragging() { dragging = true; onDrag?(true) }
    private func endDragging() { dragging = false; onDrag?(false) }

    /// Track the borderless badge independently of AppKit's title-bar drag loop
    /// so hover resizing cannot change how its click/drag gesture is interpreted.
    func trackIconPointer(with down: NSEvent, onClick: (() -> Void)?) {
        let origin = frame.origin
        let start = convertPoint(toScreen: down.locationInWindow)
        var moved = false
        var released = false
        beginDragging()
        // Keep hover and task updates from changing geometry until mouse-up.
        while let event = nextIconPointerEvent() {
            if event.type == .leftMouseUp { released = true; break }
            guard event.type == .leftMouseDragged else { continue }
            let point = convertPoint(toScreen: event.locationInWindow)
            let delta = NSPoint(x: point.x - start.x, y: point.y - start.y)
            if !moved && hypot(delta.x, delta.y) < 3 { continue }
            moved = true
            setFrameOrigin(NSPoint(x: origin.x + delta.x, y: origin.y + delta.y))
        }
        endDragging()
        if released && !moved { onClick?() }
    }

    func nextIconPointerEvent() -> NSEvent? {
        nextEvent(matching: [.leftMouseDragged, .leftMouseUp], until: .distantFuture,
                  inMode: .eventTracking, dequeue: true)
    }
    var editingText: Bool { isKeyWindow && (firstResponder as? NSTextView)?.isFieldEditor == true }
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    /// Commit SwiftUI's new corner alignment before the resized window is drawn.
    func setPresentationFrame(_ frame: NSRect) {
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0
            context.allowsImplicitAnimation = false
            setFrame(frame, display: false)
            contentView?.layoutSubtreeIfNeeded()
            displayIfNeeded()
        }
    }

    override func sendEvent(_ event: NSEvent) {
        // NSHostingView may own hit testing even over represented native views.
        // Catch a title drag at the window boundary before SwiftUI consumes it.
        if event.type == .leftMouseDown, let handle = dragHandle(at: event.locationInWindow) {
            handle.mouseDown(with: event)
            return
        }
        super.sendEvent(event)
    }

    func dragHandle(at windowPoint: NSPoint) -> DragHandleView? {
        func find(in view: NSView) -> DragHandleView? {
            guard !view.isHidden, view.alphaValue > 0 else { return nil }
            if let handle = view as? DragHandleView,
               handle.bounds.contains(handle.convert(windowPoint, from: nil)) { return handle }
            for child in view.subviews {
                if let handle = find(in: child) { return handle }
            }
            return nil
        }
        return contentView.flatMap { find(in: $0) }
    }

    /// Hide only while the current pointer operation intersects this panel.
    /// Ordering the panel never activates OpenGCUA or interrupts its voice session.
    func updatePresentation(requested: Bool, automationActive: Bool) {
        if !requested || automationActive {
            if isVisible { orderOut(nil) }
        } else if !isVisible {
            orderFrontRegardless()
        }
    }

    static func fittedFrame(_ frame: NSRect, height: CGFloat, screen: NSRect, width: CGFloat? = nil) -> NSRect {
        let width = min(width ?? compactWidth, screen.width)
        let height = min(height, screen.height)
        return NSRect(x: max(screen.minX, min(frame.minX, screen.maxX - width)),
                      y: max(screen.minY, min(frame.maxY - height, screen.maxY - height)),
                      width: width, height: height)
    }
}

/// SwiftUI's hosting view consumes background mouse events. Forward only the
/// noninteractive title area to AppKit's native window drag, leaving controls live.
struct WindowDragHandle: NSViewRepresentable {
    func makeNSView(context: Context) -> DragHandleView { DragHandleView() }
    func updateNSView(_ view: DragHandleView, context: Context) {
        view.toolTip = L("拖动小窗")
        view.setAccessibilityLabel(L("拖动小窗"))
    }
}

class DragHandleView: NSView {
    override var mouseDownCanMoveWindow: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) { window?.performDrag(with: event) }
    override func resetCursorRects() { addCursorRect(bounds, cursor: .openHand) }
}

/// The icon can be clicked or dragged, without making the background app key.
struct OrbInteraction: NSViewRepresentable {
    var openChat: () -> Void
    func makeNSView(context: Context) -> OrbHandleView { OrbHandleView() }
    func updateNSView(_ view: OrbHandleView, context: Context) { view.openChat = openChat }
}
final class OrbHandleView: DragHandleView {
    var openChat: (() -> Void)?
    override func mouseDown(with event: NSEvent) {
        guard let panel = window as? FloatingPanel else { return }
        panel.trackIconPointer(with: event, onClick: openChat)
    }
}


@MainActor struct MiniWindowPlacement {
    let frame: NSRect
    let expandsUpward: Bool
    static func fit(anchor: NSPoint, expanded: Bool, detailHeight: CGFloat, screen: NSRect) -> Self {
        let icon = FloatingPanel.fittedFrame(NSRect(x: anchor.x - FloatingPanel.iconSize, y: anchor.y,
                                                    width: FloatingPanel.iconSize, height: FloatingPanel.iconSize),
                                            height: FloatingPanel.iconSize, screen: screen, width: FloatingPanel.iconSize)
        let height = min(detailHeight, screen.height)
        let above = screen.maxY - icon.minY, below = icon.maxY - screen.minY
        let upward = above >= height || above >= below
        guard expanded else { return Self(frame: icon, expandsUpward: upward) }
        let proposed = NSRect(x: icon.maxX - FloatingPanel.compactWidth,
                              y: upward ? icon.minY : icon.maxY - height,
                              width: FloatingPanel.compactWidth, height: height)
        return Self(frame: FloatingPanel.fittedFrame(proposed, height: height, screen: screen), expandsUpward: upward)
    }
}

enum OverlayOcclusion {
    /// SDK geometry uses global screen points with a top-left origin.
    static func intersects(points: [[String: Any]], frame: NSRect) -> Bool {
        let path = points.compactMap { value -> NSPoint? in
            guard let x = value["x"] as? Double, let y = value["y"] as? Double,
                  x.isFinite, y.isFinite else { return nil }
            return NSPoint(x: x, y: y)
        }
        guard !path.isEmpty, path.count == points.count else { return false }
        func hits(_ a: NSPoint, _ b: NSPoint) -> Bool {
            var low: CGFloat = 0, high: CGFloat = 1
            for (start, delta, minimum, maximum) in [(a.x, b.x-a.x, frame.minX, frame.maxX),
                                                     (a.y, b.y-a.y, frame.minY, frame.maxY)] {
                if abs(delta) < 0.00001 {
                    if start < minimum || start > maximum { return false }
                } else {
                    let first = (minimum-start)/delta, last = (maximum-start)/delta
                    low = max(low, min(first, last)); high = min(high, max(first, last))
                    if low > high { return false }
                }
            }
            return true
        }
        if path.count == 1 { return hits(path[0], path[0]) }
        return zip(path, path.dropFirst()).contains { hits($0, $1) }
    }
}

/// Debounce boundary crossings, and keep a draft open while its editor or menu is active.
@MainActor final class MiniWindowPresentation: ObservableObject {
    @Published private(set) var expanded = false
    @Published private(set) var detailsVisible = false
    @Published private(set) var requested = false
    @Published private(set) var suppressed = false
    @Published var expandsUpward = true
    var onChange: (() -> Void)?
    var keepExpanded: () -> Bool = { false }
    private var pending: Task<Void, Never>?
    private var pointerInside = false
    private var dragging = false
    private var pointerOperation = false
    private var ignoreUntilExit = false
    private let enterDelay: UInt64
    private let exitDelay: UInt64
    private let fadeDuration: UInt64
    init(enterDelay: UInt64 = 180_000_000, exitDelay: UInt64 = 300_000_000,
         fadeDuration: UInt64 = 140_000_000) {
        self.enterDelay = enterDelay; self.exitDelay = exitDelay; self.fadeDuration = fadeDuration
    }
    func update(requested: Bool, suppressed: Bool) {
        if self.requested != requested { self.requested = requested }
        if self.suppressed != suppressed { self.suppressed = suppressed }
        if !requested || suppressed {
            pending?.cancel(); pending = nil; pointerInside = false; ignoreUntilExit = false
            setExpanded(false)
        }
    }
    func hover(_ inside: Bool) {
        pointerInside = inside
        pending?.cancel(); pending = nil
        if !inside { ignoreUntilExit = false }
        guard requested, !suppressed, !dragging, !pointerOperation, !ignoreUntilExit else { return }
        if inside && expanded { detailsVisible = true; return }
        pending = Task { [weak self, enterDelay, exitDelay] in
            do { try await Task.sleep(nanoseconds: inside ? enterDelay : exitDelay) } catch { return }
            guard let self, !Task.isCancelled, self.requested, !self.suppressed, !self.dragging, !self.pointerOperation else { return }
            if !inside && self.keepExpanded() { return }
            if inside { self.setExpanded(true); return }
            // Keep the native window and badge anchored until the card has faded.
            self.detailsVisible = false
            let duration = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion ? 0 : self.fadeDuration
            do { try await Task.sleep(nanoseconds: duration) } catch { return }
            guard !Task.isCancelled, !self.pointerInside, !self.dragging, !self.pointerOperation else { return }
            if self.keepExpanded() { self.detailsVisible = true; return }
            self.setExpanded(false)
        }
    }
    func drag(_ active: Bool) {
        dragging = active; pending?.cancel(); pending = nil
        if !active && !pointerInside { hover(false) }
    }
    func setPointerOperation(_ active: Bool) {
        guard pointerOperation != active else { return }
        pointerOperation = active; pending?.cancel(); pending = nil
        if !active { hover(pointerInside) }
    }
    func dismiss() {
        pending?.cancel(); pending = nil; ignoreUntilExit = pointerInside
        setExpanded(false)
    }
    private func setExpanded(_ value: Bool) {
        if detailsVisible != value { detailsVisible = value }
        guard expanded != value else { return }
        expanded = value; onChange?()
    }
}
