import AppKit
import Foundation

/// A private inherited pipe; no port, shell, repository path, or installed Python.
@MainActor final class RuntimeBridge {
    var onEvent: (([String: Any]) -> Void)?
    var onExit: (() -> Void)?
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private let reader = DispatchQueue(label: "app.generalist.events")
    private var generation = UUID()
    var running: Bool { process?.isRunning == true }

    func launch(dataDirectory: URL) throws {
        guard !running, let resources = Bundle.main.resourceURL else { return }
        let python = resources.appendingPathComponent("python/bin/python3")
        let host = resources.appendingPathComponent("runtime/native_host.py")
        guard FileManager.default.isExecutableFile(atPath: python.path),
              FileManager.default.fileExists(atPath: host.path) else {
            throw AppError.message(L("应用运行组件缺失，请重新安装 OpenGCUA。"))
        }
        let p = Process(), toHost = Pipe(), fromHost = Pipe()
        let token = UUID(); generation = token
        p.executableURL = python
        p.arguments = ["-I", "-B", "-u", host.path]
        var env = ProcessInfo.processInfo.environment
        for key in ["API_KEY", "DASHSCOPE_API_KEY", "PYTHONPATH", "PYTHONHOME", "MAC_AGENT_CONFIG"] { env.removeValue(forKey: key) }
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["GENERALIST_DATA_DIR"] = dataDirectory.path
        env["GENERALIST_DRIVER_APP"] = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/CuaDriver.app").path
        p.environment = env
        p.currentDirectoryURL = dataDirectory
        p.standardInput = toHost; p.standardOutput = fromHost; p.standardError = FileHandle.nullDevice
        p.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                guard let self, self.generation == token else { return }
                self.onExit?()
            }
        }
        try p.run()
        process = p; input = toHost.fileHandleForWriting; output = fromHost.fileHandleForReading
        // Dedicated serial queue preserves event ordering and UTF-8 split across reads.
        let handle = fromHost.fileHandleForReading
        reader.async { [weak self] in
            var pending = Data()
            while true {
                let chunk = handle.availableData
                if chunk.isEmpty { break }
                pending.append(chunk)
                if pending.count > 4 * 1024 * 1024 { break }
                while let end = pending.firstIndex(of: 10) {
                    let line = pending[..<end]
                    pending.removeSubrange(...end)
                    guard let value = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any] else { continue }
                    Task { @MainActor in
                        guard let self, self.generation == token else { return }
                        self.onEvent?(value)
                    }
                }
            }
        }
    }
    func send(_ command: [String: Any]) throws {
        guard running, let input else { throw AppError.message(L("运行组件已断开，请重新打开应用。")) }
        var data = try JSONSerialization.data(withJSONObject: command)
        data.append(10)
        try input.write(contentsOf: data)
    }
    func shutdown() {
        try? send(["command": "shutdown"])
        try? input?.close(); input = nil
        // Host sees EOF even if the app crashes; it drains and kills its worker.
    }
}

enum AppError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case let .message(text) = self { return text }; return nil }
}
