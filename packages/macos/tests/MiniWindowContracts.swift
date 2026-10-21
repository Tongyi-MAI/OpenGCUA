import AppKit

@MainActor final class RecordingPanel: FloatingPanel {
    var exposed = false
    var transitions: [String] = []
    var dragEvent: NSEvent?
    var pointerEvents: [(NSEvent.EventType, NSPoint)] = []
    override var isVisible: Bool { exposed }
    override func orderFrontRegardless() { exposed = true; transitions.append("show") }
    override func orderOut(_ sender: Any?) { exposed = false; transitions.append("hide") }
    override func makeKeyAndOrderFront(_ sender: Any?) { preconditionFailure("Updates must never take keyboard focus") }
    override func performDrag(with event: NSEvent) {
        dragEvent = event
    }
    override func nextIconPointerEvent() -> NSEvent? {
        precondition(dragging, "Hover must stay suspended for the entire pointer stream")
        guard !pointerEvents.isEmpty else { return nil }
        let (type, screenPoint) = pointerEvents.removeFirst()
        return NSEvent.mouseEvent(with: type, location: convertPoint(fromScreen: screenPoint),
                                  modifierFlags: [], timestamp: 0, windowNumber: windowNumber,
                                  context: nil, eventNumber: 2, clickCount: 1, pressure: 1)
    }
}

@main enum MiniWindowContracts {
    @MainActor static func main() async throws {
        _ = NSApplication.shared
        let panel = RecordingPanel(contentRect: NSRect(x: 30, y: 40, width: 320, height: 110),
                                   styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        // Open, start, pause, resume, finish, dismiss; later updates stay dismissed.
        for (requested, active) in [(true, false), (true, true), (true, true), (true, false),
                                    (true, true), (true, false), (false, false), (false, false)] {
            panel.updatePresentation(requested: requested, automationActive: active)
        }
        precondition(panel.transitions == ["show", "hide", "show", "hide", "show", "hide"])
        precondition(!panel.exposed)
        panel.updatePresentation(requested: true, automationActive: true)
        precondition(!panel.exposed, "Opening the mini window during automation must not cover its target")

        let root = NSView(frame: NSRect(x: 0, y: 0, width: 320, height: 110))
        let handle = DragHandleView(frame: NSRect(x: 10, y: 70, width: 170, height: 30))
        let button = NSButton(frame: NSRect(x: 280, y: 70, width: 24, height: 24))
        root.addSubview(handle); root.addSubview(button); panel.contentView = root
        precondition(root.hitTest(NSPoint(x: 30, y: 80)) === handle)
        precondition(root.hitTest(NSPoint(x: 290, y: 80)) === button, "Dragging must leave controls clickable")
        let event = NSEvent.mouseEvent(with: .leftMouseDown, location: NSPoint(x: 30, y: 80),
                                      modifierFlags: [], timestamp: 0, windowNumber: panel.windowNumber,
                                      context: nil, eventNumber: 1, clickCount: 1, pressure: 1)!
        precondition(handle.acceptsFirstMouse(for: event), "A background nonactivating panel must drag on its first click")
        handle.mouseDown(with: event)
        precondition(panel.dragEvent === event, "Forward the original mouse event into native window dragging")
        panel.dragEvent = nil
        precondition(panel.dragHandle(at: NSPoint(x: 290, y: 80)) == nil)
        panel.sendEvent(event)
        precondition(panel.dragEvent === event, "Intercept title drags before NSHostingView can consume them")
        handle.isHidden = true
        precondition(panel.dragHandle(at: event.locationInWindow) == nil)
        handle.isHidden = false
        handle.removeFromSuperview()
        let orb = OrbHandleView(frame: handle.frame)
        root.addSubview(orb)
        var opened = 0
        orb.openChat = { opened += 1 }
        let start = panel.convertPoint(toScreen: event.locationInWindow)
        var dragStates: [Bool] = []
        panel.onDrag = { dragStates.append($0) }
        panel.pointerEvents = [(.leftMouseUp, start)]
        panel.sendEvent(event)
        precondition(opened == 1 && panel.canBecomeKey, "Click opens chat; the panel must accept native mouse input")
        let origin = panel.frame.origin
        panel.pointerEvents = [(.leftMouseDragged, NSPoint(x: start.x + 25, y: start.y - 20)),
                               (.leftMouseUp, NSPoint(x: start.x + 25, y: start.y - 20))]
        panel.sendEvent(event)
        precondition(panel.frame.origin == NSPoint(x: origin.x + 25, y: origin.y - 20))
        precondition(opened == 1 && !panel.dragging && dragStates == [true, false, true, false],
                     "Real pointer deltas move the badge and release hover without opening chat")
        panel.setFrameOrigin(origin)
        panel.pointerEvents = [(.leftMouseDragged, NSPoint(x: start.x + 25, y: start.y)),
                               (.leftMouseDragged, start), (.leftMouseUp, start)]
        panel.sendEvent(event)
        precondition(panel.frame.origin == origin && opened == 1,
                     "A drag back to its start must not be misclassified as a click")
        panel.pointerEvents = [(.leftMouseDragged, NSPoint(x: start.x + 1, y: start.y + 1)),
                               (.leftMouseUp, start)]
        panel.sendEvent(event)
        precondition(panel.frame.origin == origin && opened == 2, "Small hand jitter still counts as a click")
        panel.sendEvent(event)
        precondition(opened == 2 && !panel.dragging, "A cancelled pointer stream must not open chat or leave drag active")

        let screen = NSRect(x: -1440, y: 24, width: 1440, height: 876)
        let dragged = NSRect(x: -650, y: 200, width: 320, height: 110)
        let expanded = FloatingPanel.fittedFrame(dragged, height: 204, screen: screen)
        precondition(expanded.minX == dragged.minX && expanded.maxY == dragged.maxY)
        precondition(FloatingPanel.fittedFrame(expanded, height: 110, screen: screen) == dragged,
                     "Content changes must not move the user's chosen top edge")
        let offscreen = FloatingPanel.fittedFrame(NSRect(x: -20, y: -80, width: 360, height: 326), height: 204, screen: screen)
        precondition(screen.contains(offscreen) && offscreen.width == 320)
        let tinyScreen = NSRect(x: 0, y: 0, width: 250, height: 180)
        precondition(tinyScreen.contains(FloatingPanel.fittedFrame(dragged, height: 204, screen: tinyScreen)))
        let badgeAnchor = NSPoint(x: -50, y: 50)
        let badge = MiniWindowPlacement.fit(anchor: badgeAnchor, expanded: false, detailHeight: 248, screen: screen)
        let card = MiniWindowPlacement.fit(anchor: badgeAnchor, expanded: true, detailHeight: 248, screen: screen)
        precondition(badge.frame.size == NSSize(width: 44, height: 44))
        let progress = MiniWindowPlacement.fit(anchor: badgeAnchor, expanded: false, detailHeight: 248,
                                                screen: screen, badgeWidth: FloatingPanel.progressWidth)
        precondition(progress.frame.width == FloatingPanel.progressWidth && progress.frame.height == 44)
        precondition(progress.frame.maxX == badge.frame.maxX && progress.frame.minY == badge.frame.minY)
        precondition(screen.contains(progress.frame))
        precondition(card.expandsUpward && card.frame.maxX == badge.frame.maxX && card.frame.minY == badge.frame.minY)
        let top = NSPoint(x: -50, y: screen.maxY - 60)
        let topBadge = MiniWindowPlacement.fit(anchor: top, expanded: false, detailHeight: 248, screen: screen)
        let topCard = MiniWindowPlacement.fit(anchor: top, expanded: true, detailHeight: 248, screen: screen)
        precondition(!topCard.expandsUpward && topCard.frame.maxY == topBadge.frame.maxY)
        precondition(screen.contains(topCard.frame), "Near the top edge, details must grow down around the same icon")
        let hover = MiniWindowPresentation(enterDelay: 20_000_000, exitDelay: 20_000_000, fadeDuration: 0)
        let cover = NSRect(x: 100, y: 100, width: 44, height: 44)
        precondition(OverlayOcclusion.intersects(points: [["x": 110.0, "y": 110.0]], frame: cover))
        precondition(!OverlayOcclusion.intersects(points: [["x": 90.0, "y": 90.0]], frame: cover))
        precondition(OverlayOcclusion.intersects(points: [["x": 50.0, "y": 120.0], ["x": 180.0, "y": 120.0]], frame: cover))
        precondition(!OverlayOcclusion.intersects(points: [["x": 80.0, "y": 115.0], ["x": 115.0, "y": 80.0]], frame: cover),
                     "An overlapping bounding box alone does not mean a diagonal drag crosses the badge")
        precondition(!OverlayOcclusion.intersects(points: [["x": Double.nan, "y": 120.0]], frame: cover))
        precondition(OverlayOcclusion.intersects(points: [["x": -110.0, "y": 120.0]], frame: NSRect(x: -144, y: 100, width: 44, height: 44)))
        hover.update(requested: true, suppressed: false)
        hover.setPointerOperation(true); hover.hover(true)
        try await Task.sleep(nanoseconds: 45_000_000)
        precondition(!hover.expanded, "Automation must not accidentally reveal hover details while moving the pointer")
        hover.setPointerOperation(false)
        hover.hover(true); hover.hover(false)
        try await Task.sleep(nanoseconds: 45_000_000)
        precondition(!hover.expanded, "A cursor merely crossing the icon must not open details")
        hover.hover(true)
        try await Task.sleep(nanoseconds: 45_000_000)
        precondition(hover.expanded)
        hover.keepExpanded = { true }; hover.hover(false)
        try await Task.sleep(nanoseconds: 45_000_000)
        precondition(hover.expanded, "Editing and menu tracking must keep details accessible")
        hover.keepExpanded = { false }; hover.hover(false)
        try await Task.sleep(nanoseconds: 45_000_000)
        precondition(!hover.expanded)
        hover.hover(true); hover.update(requested: true, suppressed: true)
        try await Task.sleep(nanoseconds: 45_000_000)
        precondition(!hover.expanded, "Execution cancels pending expansion")
        hover.update(requested: true, suppressed: false); hover.drag(true); hover.hover(true)
        try await Task.sleep(nanoseconds: 45_000_000)
        precondition(!hover.expanded, "Dragging cannot resize the window under the pointer")
        hover.drag(false); hover.hover(true)
        try await Task.sleep(nanoseconds: 45_000_000)
        precondition(hover.expanded)
        hover.dismiss(); hover.hover(true)
        try await Task.sleep(nanoseconds: 45_000_000)
        precondition(!hover.expanded, "Escape stays collapsed until the pointer leaves")
        hover.hover(false); hover.hover(true); hover.update(requested: false, suppressed: false)
        try await Task.sleep(nanoseconds: 45_000_000)
        precondition(!hover.expanded, "Dismissal cancels hover timers and must not recreate the panel")
        if !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            let fading = MiniWindowPresentation(enterDelay: 0, exitDelay: 0, fadeDuration: 80_000_000)
            fading.update(requested: true, suppressed: false); fading.hover(true)
            try await Task.sleep(nanoseconds: 15_000_000)
            precondition(fading.expanded && fading.detailsVisible)
            fading.hover(false)
            try await Task.sleep(nanoseconds: 15_000_000)
            precondition(fading.expanded && !fading.detailsVisible, "Fade the card before shrinking its window")
            fading.hover(true)
            try await Task.sleep(nanoseconds: 15_000_000)
            precondition(fading.expanded && fading.detailsVisible, "Re-entry reverses the fade without remounting or moving the badge")
            fading.hover(false)
            try await Task.sleep(nanoseconds: 110_000_000)
            precondition(!fading.expanded && !fading.detailsVisible)
        }
        let visibility = MiniWindowPresentation(enterDelay: 0, exitDelay: 0)
        visibility.update(requested: true, suppressed: false)
        visibility.hideBubble()
        for suppressed in [false, true, false, false] {
            visibility.update(requested: true, suppressed: suppressed)
            visibility.hover(true)
            try await Task.sleep(nanoseconds: 10_000_000)
            precondition(visibility.manuallyHidden && !visibility.requested && !visibility.expanded,
                         "手动隐藏后，进度和遮挡状态更新不能重新显示气泡")
        }
        visibility.allowBubble()
        visibility.update(requested: true, suppressed: false)
        precondition(visibility.requested && !visibility.manuallyHidden)
        print("Mini window progress badge, manual hiding, pointer dragging and screen geometry passed")
    }
}
