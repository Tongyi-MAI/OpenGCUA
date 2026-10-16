import Cocoa

final class DragPad: NSView {
    var drags = 0
    var pressed = false
    var inputEvents: [[String:Any]] = []
    func record(_ event:NSEvent, _ kind:String) {
        let point=convert(event.locationInWindow,from:nil)
        inputEvents.append(["kind":kind,"x":point.x,"y":point.y,"modifiers":event.modifierFlags.rawValue,"dx":event.type == .scrollWheel ? event.scrollingDeltaX : 0,"dy":event.type == .scrollWheel ? event.scrollingDeltaY : 0])
    }
    override func draw(_ rect: NSRect) { NSColor.systemBlue.setFill(); bounds.fill() }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) { pressed=true;record(event,"down") }
    override func mouseDragged(with event: NSEvent) { drags += 1;record(event,"drag") }
    override func mouseUp(with event: NSEvent) { pressed=false;record(event,"up") }
    override func scrollWheel(with event:NSEvent) { record(event,"wheel") }
}

final class Fixture: NSObject, NSApplicationDelegate {
    var windows: [NSWindow] = []
    var fields: [NSTextField] = []
    var scrolls: [NSScrollView] = []
    var pads: [DragPad] = []
    var counts = [0, 0]
    let output: URL
    init(output: URL) { self.output = output }
    func applicationDidFinishLaunching(_ note: Notification) {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit Fixture", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(appItem)
        let fixtureItem = NSMenuItem(title: "Fixture", action: nil, keyEquivalent: "")
        let fixtureMenu = NSMenu(title: "Fixture")
        let menuAction = NSMenuItem(title: "Record menu action", action: #selector(recordMenu), keyEquivalent: "")
        menuAction.target = self
        fixtureMenu.addItem(menuAction)
        fixtureItem.submenu = fixtureMenu
        menu.addItem(fixtureItem)
        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Select All", action: Selector(("selectAll:")), keyEquivalent: "a")
        editItem.submenu = editMenu;menu.addItem(editItem)
        NSApp.mainMenu = menu
        for i in 0..<2 {
            let window = NSWindow(contentRect: NSRect(x: 80 + i*500, y: 180, width: 450, height: 460), styleMask: [.titled,.closable,.resizable], backing: .buffered, defer: false)
            window.title = "MAI Fixture \(i+1)"
            window.isReleasedWhenClosed = false
            window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            let view = window.contentView!
            let label = NSTextField(labelWithString: "Counter \(i+1): 0")
            label.frame = NSRect(x: 24, y: 390, width: 350, height: 28)
            label.identifier = NSUserInterfaceItemIdentifier("counter-\(i)")
            view.addSubview(label)
            let button = NSButton(title: "Increment \(i+1)", target: self, action: #selector(increment(_:)))
            button.tag = i; button.frame = NSRect(x:24,y:335,width:160,height:36)
            view.addSubview(button)
            let field = NSTextField(string: "initial-\(i+1)")
            field.frame = NSRect(x:24,y:280,width:350,height:30)
            field.setAccessibilityLabel("Editor \(i+1)")
            field.target = self; field.action = #selector(save)
            view.addSubview(field); fields.append(field)
            let scroll = NSScrollView(frame:NSRect(x:24,y:30,width:350,height:230))
            scroll.hasVerticalScroller = true
            let document = NSView(frame:NSRect(x:0,y:0,width:320,height:1400))
            for row in 0..<35 {
                let text = NSTextField(labelWithString:"Fixture row \(row)")
                text.frame=NSRect(x:12,y:row*38,width:280,height:30);document.addSubview(text)
            }
            scroll.documentView=document;view.addSubview(scroll);scrolls.append(scroll)
            let pad = DragPad(frame: NSRect(x:385,y:30,width:50,height:230))
            view.addSubview(pad);pads.append(pad)
            windows.append(window);window.orderFront(nil)
        }
        NSApp.activate(ignoringOtherApps: true)
        save()
        Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { _ in self.save() }
    }
    @objc func increment(_ button:NSButton) {
        counts[button.tag] += 1
        let label = windows[button.tag].contentView!.subviews.compactMap { $0 as? NSTextField }.first!
        label.stringValue = "Counter \(button.tag+1): \(counts[button.tag])"
        save()
    }
    var menuCount = 0
    @objc func recordMenu() { menuCount += 1; save() }
    @objc func save() {
        let data: [String:Any] = ["counts":counts,"text":fields.map(\.stringValue),"menu_count":menuCount,"scroll_y":scrolls.map { $0.contentView.bounds.origin.y },"drag_events":pads.map(\.drags),"pointer_pressed":pads.map(\.pressed),"input_events":pads.map(\.inputEvents),"frames":windows.map { ["x":$0.frame.origin.x,"y":$0.frame.origin.y,"width":$0.frame.width,"height":$0.frame.height] },"pid":ProcessInfo.processInfo.processIdentifier,"frontmost_pid":NSWorkspace.shared.frontmostApplication?.processIdentifier ?? -1]
        if let bytes=try? JSONSerialization.data(withJSONObject:data,options:[.sortedKeys]) {try? bytes.write(to:output,options:.atomic)}
    }
}
let app = NSApplication.shared
let output = URL(fileURLWithPath:CommandLine.arguments.dropFirst().first ?? "/tmp/mai-ui-fixture.json")
let delegate = Fixture(output: output)
app.delegate=delegate
app.setActivationPolicy(.regular)
app.run()
