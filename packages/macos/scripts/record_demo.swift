// Developer-only demo recorder (macOS 15+). Configuration lives outside the
// signed bundle so changing a recording target does not invalidate TCC grants.
import AppKit
import ScreenCaptureKit
import AVFoundation

@MainActor final class Recorder: NSObject, SCRecordingOutputDelegate {
    var finished = false
    var failed = false
    nonisolated func recordingOutputDidStartRecording(_ output: SCRecordingOutput) { print("RECORDING_STARTED"); fflush(stdout) }
    nonisolated func recordingOutputDidFinishRecording(_ output: SCRecordingOutput) {
        Task { @MainActor in self.finished = true; print("RECORDING_FINISHED"); fflush(stdout) }
    }
    nonisolated func recordingOutput(_ output: SCRecordingOutput, didFailWithError error: Error) {
        Task { @MainActor in self.failed = true; self.finished = true; print("RECORDING_ERROR: \(error.localizedDescription)"); fflush(stdout) }
    }
}
struct Capture {
    struct Configuration: Decodable {
        var outputPath: String
        var targetBundleID: String
        var targetWindowTitle: String?
    }
    @MainActor static func run(started: @escaping () -> Void) async throws {
        let configURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Generalist/demo-recorder.json")
        let options = try JSONDecoder().decode(Configuration.self, from: Data(contentsOf: configURL))
        guard ["com.apple.calculator", "com.google.Chrome"].contains(options.targetBundleID) else {
            throw NSError(domain: "DemoRecorder", code: 1, userInfo: [NSLocalizedDescriptionKey: "请选择计算器或 Chrome 演示窗口。"])
        }
        let outputPath = options.outputPath
        let stopPath = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : outputPath + ".stop"
        let output = URL(fileURLWithPath: outputPath)
        let stop = URL(fileURLWithPath: stopPath)
        let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: false)
        let apps = content.applications.filter { ["app.generalist.mac", options.targetBundleID].contains($0.bundleIdentifier) }
        guard apps.count == 2 else { throw NSError(domain: "DemoRecorder", code: 2, userInfo: [NSLocalizedDescriptionKey: "请先打开 OpenGCUA 和目标应用。"] ) }
        let targetWindows = content.windows.filter { $0.owningApplication?.bundleIdentifier == options.targetBundleID }
        let candidates = targetWindows.filter { $0.isOnScreen && $0.frame.width > 200 && $0.frame.height > 200 && (options.targetWindowTitle == nil || ($0.title ?? "").contains(options.targetWindowTitle!)) }
        guard candidates.count == 1, let appWindow = candidates.first else { throw NSError(domain: "DemoRecorder", code: 3, userInfo: [NSLocalizedDescriptionKey: "无法唯一匹配演示窗口，请检查窗口标题。"] ) }
        let excluded = targetWindows.filter { $0.windowID != appWindow.windowID }
        guard let display = content.displays.first(where: { $0.frame.intersects(appWindow.frame) }) ?? content.displays.first else { fatalError("No display") }
        let filter = SCContentFilter(display: display, including: apps, exceptingWindows: excluded)
        filter.includeMenuBar = false
        let config = SCStreamConfiguration()
        let scale = min(1.0, 1920.0 / Double(display.width))
        config.width = Int(Double(display.width) * scale) / 2 * 2
        config.height = Int(Double(display.height) * scale) / 2 * 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 30)
        config.queueDepth = 5
        config.capturesAudio = false
        config.captureMicrophone = false
        config.showsCursor = true
        config.showMouseClicks = true
        let background = CGColor(red: 0.91, green: 0.93, blue: 0.96, alpha: 1)
        config.backgroundColor = background
        let stream = SCStream(filter: filter, configuration: config, delegate: nil)
        let recordingConfig = SCRecordingOutputConfiguration()
        recordingConfig.outputURL = output
        recordingConfig.videoCodecType = .h264
        recordingConfig.outputFileType = .mp4
        let delegate = Recorder()
        let recording = SCRecordingOutput(configuration: recordingConfig, delegate: delegate)
        try stream.addRecordingOutput(recording)
        print("CAPTURE \(config.width)x\(config.height); OpenGCUA + target window \(appWindow.windowID)"); fflush(stdout)
        try await stream.startCapture()
        started()
        let deadline = Date().addingTimeInterval(1800)
        while !FileManager.default.fileExists(atPath: stop.path) && !delegate.finished && Date() < deadline {
            try await Task.sleep(for: .milliseconds(250))
        }
        if !delegate.finished { try await stream.stopCapture() }
        let finishDeadline = Date().addingTimeInterval(15)
        while !delegate.finished && Date() < finishDeadline { try await Task.sleep(for: .milliseconds(100)) }
        withExtendedLifetime(background) {}
        guard delegate.finished && !delegate.failed else { throw NSError(domain: "DemoRecorder", code: 4, userInfo: [NSLocalizedDescriptionKey: "录屏输出未正常完成，请检查视频文件。"] ) }
    }
}

@MainActor final class RecorderApp: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var label: NSTextField!
    var task: Task<Void, Never>?
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 420, height: 180), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "OpenGCUA Demo Recorder"
        window.isReleasedWhenClosed = false
        label = NSTextField(wrappingLabelWithString: "录制 OpenGCUA 与配置中指定的演示窗口，不录其他现有窗口或声音。")
        label.frame = NSRect(x: 20, y: 65, width: 380, height: 90)
        let start = NSButton(title: "开始录制", target: self, action: #selector(startRecording))
        start.frame = NSRect(x: 280, y: 18, width: 115, height: 32)
        let settings = NSButton(title: "打开录屏权限设置", target: self, action: #selector(permissions))
        settings.frame = NSRect(x: 20, y: 18, width: 160, height: 32)
        window.contentView?.addSubview(label); window.contentView?.addSubview(start); window.contentView?.addSubview(settings)
        window.center(); window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    @objc func permissions() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!)
    }
    @objc func startRecording() {
        guard task == nil else { return }
        label.stringValue = "正在准备录制…"
        task = Task {
            do { try await Capture.run { self.label.stringValue = "正在录制 · OpenGCUA 与演示窗口" }; label.stringValue = "录制完成。" }
            catch { label.stringValue = "无法录制：" + error.localizedDescription }
            task = nil
        }
    }
}
@main enum DemoRecorderMain {
    @MainActor static func main() {
        let app = NSApplication.shared, delegate = RecorderApp()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
