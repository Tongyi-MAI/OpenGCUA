// Test-only helper: observe the real pointer, or preserve clipboard data in memory
// around a SDK text round trip. This is deliberately not shipped as an SDK API.
import Cocoa
import CoreGraphics
func report(_ value: [String:Any]) {
    if let bytes = try? JSONSerialization.data(withJSONObject:value,options:[.sortedKeys]) {
        print(String(decoding:bytes,as:UTF8.self));fflush(stdout)
    }
}
if CommandLine.arguments.dropFirst().first == "pointer" {
    let point = CGEvent(source:nil)!.location
    report(["x":point.x,"y":point.y])
} else {
    let pb = NSPasteboard.general
    let nonce = CommandLine.arguments[1]
    let oldCount = pb.changeCount
    var copies: [NSPasteboardItem] = []
    var complete = true
    for item in pb.pasteboardItems ?? [] {
        let copy = NSPasteboardItem()
        for type in item.types {
            if let bytes = item.data(forType:type) { copy.setData(bytes,forType:type) }
            else { complete = false }
        }
        copies.append(copy)
    }
    guard complete && oldCount == pb.changeCount else { report(["ready":false]);exit(0) }
    report(["ready":true,"change_count":oldCount])
    _ = readLine()
    // Never overwrite a later user clipboard change. No clipboard bytes reach logs.
    guard pb.string(forType:.string) == nonce else { report(["restored":false,"reason":"clipboard_changed"]);exit(0) }
    pb.clearContents()
    let restored = copies.isEmpty || pb.writeObjects(copies)
    report(["restored":restored])
}
