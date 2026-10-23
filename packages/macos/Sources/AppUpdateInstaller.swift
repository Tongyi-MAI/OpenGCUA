import AppKit
import Foundation
import Security
import Darwin

enum AppUpdateInstaller {
    static let bundleID = "app.generalist.mac"

    static func checkLocation(_ app: URL) throws {
        let values = try app.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
        guard app.pathExtension == "app", values.isDirectory == true, values.isSymbolicLink != true,
              FileManager.default.isWritableFile(atPath: app.deletingLastPathComponent().path),
              !app.path.contains("/AppTranslocation/"), !app.path.hasPrefix("/Volumes/") else {
            throw AppUpdateError.installLocation
        }
    }

    /// All expensive work happens off the UI thread, before the running app exits.
    static func prepare(dmg: URL, installed: URL, version: String, directory: URL) throws -> URL {
        try checkLocation(installed)
        let mount = directory.appendingPathComponent("mount", isDirectory: true)
        try FileManager.default.createDirectory(at: mount, withIntermediateDirectories: false)
        do { _ = try command("/usr/bin/hdiutil", ["attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", mount.path, dmg.path], timeout: 120) }
        catch {
            // attach can mount successfully before timing out or failing later.
            _ = try? command("/usr/bin/hdiutil", ["detach", "-force", mount.path], timeout: 30)
            throw error
        }
        defer {
            if (try? command("/usr/bin/hdiutil", ["detach", mount.path], timeout: 30)) == nil {
                _ = try? command("/usr/bin/hdiutil", ["detach", "-force", mount.path], timeout: 30)
            }
        }
        let source = mount.appendingPathComponent("OpenGCUA.app", isDirectory: true)
        try validate(source, installed: installed, version: version)
        let stage = installed.deletingLastPathComponent().appendingPathComponent(".OpenGCUA-update-" + UUID().uuidString + ".app", isDirectory: true)
        do {
            _ = try command("/usr/bin/ditto", [source.path, stage.path], timeout: 300)
            try validate(stage, installed: installed, version: version)
            return stage
        } catch {
            try? FileManager.default.removeItem(at: stage)
            throw error
        }
    }

    static func validate(_ candidate: URL, installed: URL, version: String) throws {
        let info = try metadata(candidate)
        guard let expected = AppReleaseVersion(version),
              info["CFBundleIdentifier"] as? String == bundleID,
              info["CFBundleShortVersionString"] as? String == version,
              let old = try metadata(installed)["CFBundleShortVersionString"] as? String,
              let current = AppReleaseVersion(old), expected > current,
              let executable = info["CFBundleExecutable"] as? String,
              !executable.isEmpty, ![".", ".."].contains(executable), !executable.contains("/") else { throw AppUpdateError.invalidApp }
        if let minimum = info["LSMinimumSystemVersion"] as? String {
            let fields = minimum.split(separator: ".").compactMap { Int($0) }
            guard !fields.isEmpty, fields.count <= 3,
                  fields.count == minimum.split(separator: ".").count,
                  ProcessInfo.processInfo.isOperatingSystemAtLeast(OperatingSystemVersion(
                    majorVersion: fields[0], minorVersion: fields.count > 1 ? fields[1] : 0,
                    patchVersion: fields.count > 2 ? fields[2] : 0)) else { throw AppUpdateError.invalidApp }
        }
        let binary = candidate.appendingPathComponent("Contents/MacOS/" + executable)
        guard binary.resolvingSymlinksInPath().path.hasPrefix(candidate.resolvingSymlinksInPath().path + "/Contents/") else {
            throw AppUpdateError.invalidApp
        }
        _ = try command("/usr/bin/lipo", ["-verify_arch", "arm64", binary.path], timeout: 20)
        do { _ = try command("/usr/bin/codesign", ["--verify", "--deep", "--strict", candidate.path], timeout: 120) }
        catch { throw AppUpdateError.signature }
        // Developer ID builds must keep the same publisher. Development builds are
        // ad-hoc signed; those rely on the checksum from the official HTTPS release.
        if let team = try signingTeam(installed) {
            guard try signingTeam(candidate) == team else { throw AppUpdateError.signature }
            var original: SecStaticCode?, replacement: SecStaticCode?, requirement: SecRequirement?
            guard SecStaticCodeCreateWithPath(installed as CFURL, [], &original) == errSecSuccess,
                  SecStaticCodeCreateWithPath(candidate as CFURL, [], &replacement) == errSecSuccess,
                  let original, let replacement,
                  SecCodeCopyDesignatedRequirement(original, [], &requirement) == errSecSuccess, let requirement,
                  SecStaticCodeCheckValidity(replacement, SecCSFlags(rawValue: kSecCSCheckAllArchitectures), requirement) == errSecSuccess else {
                throw AppUpdateError.signature
            }
        }
    }

    private static func metadata(_ app: URL) throws -> [String: Any] {
        let values = try app.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
        guard values.isDirectory == true, values.isSymbolicLink != true,
              let info = try PropertyListSerialization.propertyList(
                from: Data(contentsOf: app.appendingPathComponent("Contents/Info.plist")), format: nil) as? [String: Any] else {
            throw AppUpdateError.invalidApp
        }
        return info
    }
    private static func signingTeam(_ app: URL) throws -> String? {
        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(app as CFURL, [], &code) == errSecSuccess, let code else { throw AppUpdateError.signature }
        var information: CFDictionary?
        guard SecCodeCopySigningInformation(code, SecCSFlags(rawValue: kSecCSSigningInformation), &information) == errSecSuccess else {
            throw AppUpdateError.signature
        }
        return (information as? [String: Any])?[kSecCodeInfoTeamIdentifier as String] as? String
    }

    @MainActor static func launchHelper(installed: URL, staged: URL, directory: URL) async throws -> Process {
        guard let executable = Bundle.main.executableURL else { throw AppUpdateError.installation }
        let helper = directory.appendingPathComponent("OpenGCUAUpdater-" + UUID().uuidString)
        try FileManager.default.copyItem(at: executable, to: helper)
        let ready = directory.appendingPathComponent("ready")
        try? FileManager.default.removeItem(at: ready)
        let process = Process()
        process.executableURL = helper
        process.arguments = ["--install-update", String(getpid()), installed.path, staged.path, directory.path]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        for _ in 0..<300 {
            if FileManager.default.fileExists(atPath: ready.path), process.isRunning { return process }
            guard process.isRunning else { throw AppUpdateError.installation }
            try await Task.sleep(for: .milliseconds(100))
        }
        process.terminate()
        throw AppUpdateError.timeout
    }

    /// Swapping sibling directories is atomic on macOS, with the old app kept
    /// at `staged` until Launch Services confirms the new app has launched.
    @MainActor static func replaceAndRelaunch(installed: URL, staged: URL,
                                             launch: (URL) async throws -> Void) async throws {
        guard installed.deletingLastPathComponent() == staged.deletingLastPathComponent(),
              installed != staged, staged.lastPathComponent.hasPrefix(".OpenGCUA-update-"),
              staged.pathExtension == "app" else { throw AppUpdateError.installation }
        try swap(installed, staged)
        do { try await launch(installed) }
        catch {
            try swap(installed, staged)
            throw error
        }
        try? FileManager.default.removeItem(at: staged)
    }
    private static func swap(_ first: URL, _ second: URL) throws {
        guard renamex_np(first.path, second.path, UInt32(RENAME_SWAP)) == 0 else {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
    }

    @MainActor static func runHelper(_ arguments: [String]) async -> Int32 {
        guard arguments.count == 6, let pid = Int32(arguments[2]), pid > 1, pid != getpid() else { return 1 }
        let installed = URL(fileURLWithPath: arguments[3]).standardizedFileURL
        let staged = URL(fileURLWithPath: arguments[4]).standardizedFileURL
        let directory = URL(fileURLWithPath: arguments[5]).standardizedFileURL
        let prefix = "OpenGCUA-update-", stagePrefix = ".OpenGCUA-update-"
        guard directory.lastPathComponent.hasPrefix(prefix),
              UUID(uuidString: String(directory.lastPathComponent.dropFirst(prefix.count))) != nil,
              directory.deletingLastPathComponent().resolvingSymlinksInPath() == FileManager.default.temporaryDirectory.resolvingSymlinksInPath(),
              (try? directory.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) == false,
              let attributes = try? FileManager.default.attributesOfItem(atPath: directory.path),
              (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
              let mode = (attributes[.posixPermissions] as? NSNumber)?.intValue, mode & 0o077 == 0,
              staged.lastPathComponent.hasPrefix(stagePrefix), staged.pathExtension == "app",
              UUID(uuidString: String(staged.deletingPathExtension().lastPathComponent.dropFirst(stagePrefix.count))) != nil,
              staged.deletingLastPathComponent() == installed.deletingLastPathComponent(), installed != staged else { return 1 }
        do {
            let info = try metadata(staged)
            guard let version = info["CFBundleShortVersionString"] as? String else { throw AppUpdateError.invalidApp }
            try validate(staged, installed: installed, version: version)
            try Data().write(to: directory.appendingPathComponent("ready"), options: .atomic)
            let deadline = Date().addingTimeInterval(120)
            while kill(pid, 0) == 0 || errno == EPERM {
                guard Date() < deadline else { throw AppUpdateError.timeout }
                try await Task.sleep(for: .milliseconds(250))
            }
            try await replaceAndRelaunch(installed: installed, staged: staged) { url in
                let configuration = NSWorkspace.OpenConfiguration()
                configuration.createsNewApplicationInstance = true
                _ = try await NSWorkspace.shared.openApplication(at: url, configuration: configuration)
            }
            try? FileManager.default.removeItem(at: directory)
            return 0
        } catch {
            let defaults = UserDefaults(suiteName: bundleID)
            defaults?.set("更新未能安装，原应用已保留。", forKey: "lastAppUpdateError")
            defaults?.synchronize()
            // An unsuccessful replacement leaves (or restores) the original app.
            if kill(pid, 0) != 0 { _ = NSWorkspace.shared.open(installed) }
            return 1
        }
    }

    @discardableResult static func command(_ executable: String, _ arguments: [String], timeout: TimeInterval) throws -> Data {
        let process = Process(), output = Pipe()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        let timer = DispatchSource.makeTimerSource(queue: .global())
        timer.schedule(deadline: .now() + timeout)
        timer.setEventHandler { if process.isRunning { kill(process.processIdentifier, SIGKILL) } }
        timer.resume()
        defer { timer.cancel() }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw AppUpdateError.installation }
        return data
    }
}
