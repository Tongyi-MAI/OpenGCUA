import AppKit
import SwiftUI

enum AppBrand {
    static let logo = resource("OpenGCUALogo", fileExtension: "png")
    static let icon = resource("OpenGCUA", fileExtension: "icns")

    private static func resource(_ name: String, fileExtension suffix: String) -> NSImage {
        guard let url = Bundle.main.url(forResource: name, withExtension: suffix),
              let image = NSImage(contentsOf: url) else {
            preconditionFailure("Missing app brand resource: \(name).\(suffix)")
        }
        return image
    }

    static func menuBarImage(description: String) -> NSImage {
        let size = NSSize(width: 20, height: 20)
        let image = NSImage(size: size, flipped: false) { bounds in
            logo.draw(in: bounds)
            return true
        }
        // macOS uses the original alpha mask to provide contrast in either appearance.
        image.isTemplate = true
        image.accessibilityDescription = description
        return image
    }
}

struct Brand: View {
    var size: CGFloat = 32
    var body: some View {
        Image(nsImage: AppBrand.icon).resizable().interpolation(.high).scaledToFit()
            .frame(width: size, height: size).accessibilityHidden(true)
    }
}
