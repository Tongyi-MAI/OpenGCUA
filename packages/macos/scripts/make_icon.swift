import AppKit

guard CommandLine.arguments.count >= 2 else {
    fatalError("Usage: swift make_icon.swift output.iconset [logo.png]")
}
let directory = URL(fileURLWithPath: CommandLine.arguments[1])
let defaultLogo = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
    .deletingLastPathComponent().appendingPathComponent("Resources/OpenGCUALogo.png")
let logoURL = CommandLine.arguments.count > 2 ? URL(fileURLWithPath: CommandLine.arguments[2]) : defaultLogo
guard let logo = NSImage(contentsOf: logoURL) else { fatalError("Cannot load logo at \(logoURL.path)") }
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        // Explicit pixel dimensions keep the iconset correct on Retina and headless builders.
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
                                      bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                      isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
        NSGraphicsContext.current?.imageInterpolation = .high
        let full = CGFloat(pixels), inset = full * 0.08
        let bounds = NSRect(x: inset, y: inset, width: full - 2 * inset, height: full - 2 * inset)
        let shape = NSBezierPath(roundedRect: bounds, xRadius: full * 0.20, yRadius: full * 0.20)
        NSColor(srgbRed: 0.08, green: 0.085, blue: 0.11, alpha: 1).setFill()
        shape.fill()
        logo.draw(in: bounds.insetBy(dx: full * 0.04, dy: full * 0.04))
        NSGraphicsContext.restoreGraphicsState()
        let name = "icon_\(size)x\(size)\(scale == 2 ? "@2x" : "").png"
        try bitmap.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent(name))
    }
}
