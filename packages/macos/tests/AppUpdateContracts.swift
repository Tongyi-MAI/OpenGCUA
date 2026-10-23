import AppKit
import Foundation
import CryptoKit

final class ReleaseProtocol: URLProtocol {
    static var body = Data()
    static var status = 200
    static var requests = [URLRequest]()
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.requests.append(request)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: Self.status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Self.body)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main enum AppUpdateContracts {
    static func release(version: String = "0.1.10", assets: Bool = true) -> [String: Any] {
        let root = AppUpdateRelease.repository
        return ["tag_name": "v" + version, "html_url": root + "/releases/tag/v" + version,
                "draft": false, "prerelease": false, "published_at": "2026-10-23T00:00:00Z",
                "assets": assets ? [["name": AppUpdateRelease.assetName, "state": "uploaded", "size": 3,
                    "browser_download_url": root + "/releases/download/v" + version + "/" + AppUpdateRelease.assetName,
                    "digest": "sha256:" + String(repeating: "a", count: 64)]] : []]
    }
    static func parse(_ object: [String: Any], current: String = "0.1.2") throws -> AppUpdateRelease.Result {
        try AppUpdateRelease.parse(JSONSerialization.data(withJSONObject: object), currentVersion: current)
    }
    static func expect(_ condition: @autoclosure () throws -> Bool, _ message: String = "Contract failed") rethrows {
        let result = try condition(); precondition(result, message)
    }
    static func fails(_ operation: () throws -> Void) {
        do { try operation(); preconditionFailure("Expected rejection") } catch {}
    }
    static func releaseContracts(_ directory: URL) throws {
        precondition(AppReleaseVersion("0.1.10")! > AppReleaseVersion("0.1.2")!)
        precondition(AppReleaseVersion("1.0.0")! > AppReleaseVersion("0.99.99")!)
        for value in ["", "1.2", "01.2.3", "1.2.3rc1", "1.2.3+dev", "-1.2.3", "v1.2.3"] { precondition(AppReleaseVersion(value) == nil) }
        guard case let .available(update) = try parse(release()) else { preconditionFailure("Ready app update was not offered") }
        precondition(update.version == "0.1.10")
        try expect(try parse(release(version: "0.1.2")) == .current)
        try expect(try parse(release(version: "0.1.1")) == .current)
        try expect(try parse(release(assets: false)) == .preparing("0.1.10"))
        for flag in ["draft", "prerelease"] {
            var object = release(); object[flag] = true
            try expect(try parse(object) == .current)
        }
        var object = release(); object["published_at"] = NSNull()
        try expect(try parse(object) == .current)
        object = release(); object["html_url"] = "https://example.test/release"
        fails { _ = try parse(object) }
        object = release()
        var assets = object["assets"] as! [[String: Any]]
        assets[0]["browser_download_url"] = "https://example.test/update.dmg"; object["assets"] = assets
        fails { _ = try parse(object) }
        assets[0]["browser_download_url"] = update.downloadURL.absoluteString
        assets[0]["state"] = "new"; object["assets"] = assets
        try expect(try parse(object) == .preparing("0.1.10"))
        assets[0]["state"] = "uploaded"; assets[0]["digest"] = NSNull(); object["assets"] = assets
        try expect(try parse(object) == .preparing("0.1.10"))
        let checksumURL = update.downloadURL.absoluteString + ".sha256"
        assets.append(["name": AppUpdateRelease.assetName + ".sha256", "size": 93, "state": "uploaded", "browser_download_url": checksumURL])
        object["assets"] = assets
        guard case let .available(fallback) = try parse(object) else { preconditionFailure("Checksum fallback missing") }
        precondition(fallback.digest == nil && fallback.checksumURL?.absoluteString == checksumURL)
        assets[0]["digest"] = "sha256:invalid"; object["assets"] = assets
        fails { _ = try parse(object) }
        fails { _ = try parse(release(), current: "invalid") }
        let file = directory.appendingPathComponent("checksum-fixture")
        let data = Data("abc".utf8); try data.write(to: file)
        let digest = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        try AppUpdateRelease.verify(file, digest: digest, size: 3)
        fails { try AppUpdateRelease.verify(file, digest: String(repeating: "0", count: 64), size: 3) }
        fails { try AppUpdateRelease.verify(file, digest: digest, size: 4) }
        try expect(try AppUpdateRelease.checksum(Data((digest + "  " + AppUpdateRelease.assetName + "\n").utf8)) == digest)
        fails { _ = try AppUpdateRelease.checksum(Data((digest + "  another.dmg").utf8)) }
        fails { _ = try AppUpdateRelease.checksum(Data((digest + "  " + AppUpdateRelease.assetName + "\nextra").utf8)) }
    }

    @MainActor static func checkerContracts() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ReleaseProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let suite = "OpenGCUA-update-contracts-" + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        var now = Date()
        let updater = AppUpdater(defaults: defaults, service: AppReleaseService(session: session), currentVersion: "0.1.2", now: { now })
        ReleaseProtocol.body = try JSONSerialization.data(withJSONObject: release())
        ReleaseProtocol.requests = []; ReleaseProtocol.status = 200
        updater.checkIfDue()
        while updater.busy { try await Task.sleep(for: .milliseconds(5)) }
        precondition(updater.available?.version == "0.1.10" && updater.showsPrompt)
        precondition(ReleaseProtocol.requests.count == 1)
        let request = ReleaseProtocol.requests[0]
        precondition(request.url == AppReleaseService.endpoint && request.value(forHTTPHeaderField: "Authorization") == nil)
        updater.checkIfDue(); precondition(ReleaseProtocol.requests.count == 1, "Checks must be throttled")
        updater.remindLater(); precondition(!updater.showsPrompt && !updater.shouldPrompt(for: "0.1.10"))
        now = now.addingTimeInterval(25 * 60 * 60)
        precondition(updater.shouldPrompt(for: "0.1.10"))
        updater.skipVersion(); precondition(!updater.shouldPrompt(for: "0.1.10"))
        precondition(updater.shouldPrompt(for: "0.1.11"))
        updater.check()
        while updater.busy { try await Task.sleep(for: .milliseconds(5)) }
        precondition(updater.showsPrompt, "Manual checks must override a skipped version")
        updater.canInstall = { false }; updater.install()
        precondition(updater.state == .idle, "Active tasks must prevent installation")
        updater.automaticChecks = false
        now = now.addingTimeInterval(25 * 60 * 60)
        let count = ReleaseProtocol.requests.count
        updater.checkIfDue(); precondition(ReleaseProtocol.requests.count == count)
        ReleaseProtocol.status = 403
        updater.check()
        while updater.busy { try await Task.sleep(for: .milliseconds(5)) }
        precondition(updater.isError && updater.message.contains("403"))
        ReleaseProtocol.status = 404
        updater.check()
        while updater.busy { try await Task.sleep(for: .milliseconds(5)) }
        precondition(updater.available == nil && !updater.isError)
        updater.stop()

        // Exercise URLSession's actual download-to-file path as well as JSON checks.
        let downloadDirectory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: downloadDirectory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: downloadDirectory) }
        let bytes = Data("abc".utf8)
        let digest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        let item = AppUpdateRelease(version: "0.1.10", downloadURL: URL(string: AppUpdateRelease.repository + "/releases/download/v0.1.10/" + AppUpdateRelease.assetName)!,
                                    releaseURL: URL(string: AppUpdateRelease.repository + "/releases/tag/v0.1.10")!, size: 3, digest: digest, checksumURL: nil)
        ReleaseProtocol.status = 200; ReleaseProtocol.body = bytes
        let download = try await AppReleaseService(session: session).download(item, to: downloadDirectory)
        try expect(try Data(contentsOf: download) == bytes)
    }

    static func fixtureApp(_ url: URL, version: String) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: url.appendingPathComponent("Contents/MacOS"), withIntermediateDirectories: true)
        let info: [String: Any] = ["CFBundleIdentifier": AppUpdateInstaller.bundleID, "CFBundleShortVersionString": version,
                                  "CFBundleExecutable": "Fixture", "CFBundlePackageType": "APPL", "LSMinimumSystemVersion": "14.0"]
        try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
            .write(to: url.appendingPathComponent("Contents/Info.plist"))
        try fm.copyItem(at: URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL,
                        to: url.appendingPathComponent("Contents/MacOS/Fixture"))
        _ = try AppUpdateInstaller.command("/usr/bin/codesign", ["--force", "--sign", "-", url.path], timeout: 30)
    }

    @MainActor static func dialogContracts() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        guard case let .available(update) = try parse(release()) else { preconditionFailure() }
        let alert = AppUpdateDialog.make(release: update, currentVersion: "0.1.2")
        precondition(alert.alertStyle == .informational)
        precondition(alert.buttons.map(\.title) == [L("安装更新"), L("稍后"), L("跳过此版本")])
        precondition(alert.buttons[0].keyEquivalent == "\r" && alert.buttons[1].keyEquivalent == "\u{1b}")
        precondition(alert.informativeText.contains("0.1.2") && alert.informativeText.contains(update.version))
        let notes = alert.accessoryView as! NSTextField
        precondition(notes.attributedStringValue.attribute(.link, at: 0, effectiveRange: nil) as? URL == update.releaseURL)
        let ready = AppUpdateDialog.make(release: update, currentVersion: "0.1.2", ready: true)
        precondition(ready.buttons.count == 2 && ready.buttons[0].title == L("安装并重新启动"))
        let failure = AppUpdateDialog.make(release: update, currentVersion: "0.1.2", error: "Fixture failure")
        precondition(failure.alertStyle == .warning && failure.informativeText == "Fixture failure")
    }

    @MainActor static func installerContracts(_ directory: URL) async throws {
        let fm = FileManager.default
        let installed = directory.appendingPathComponent("Installed app with spaces.app")
        let payload = directory.appendingPathComponent("payload")
        let source = payload.appendingPathComponent("OpenGCUA.app")
        try fixtureApp(installed, version: "0.1.2")
        try fixtureApp(source, version: "0.1.10")
        try AppUpdateInstaller.validate(source, installed: installed, version: "0.1.10")
        fails { try AppUpdateInstaller.validate(source, installed: installed, version: "0.1.11") }
        fails { try AppUpdateInstaller.validate(installed, installed: installed, version: "0.1.2") }
        let alias = directory.appendingPathComponent("alias.app")
        try fm.createSymbolicLink(at: alias, withDestinationURL: source)
        fails { try AppUpdateInstaller.validate(alias, installed: installed, version: "0.1.10") }
        let dmg = directory.appendingPathComponent("fixture.dmg")
        _ = try AppUpdateInstaller.command("/usr/bin/hdiutil", ["create", "-srcfolder", payload.path, "-volname", "OpenGCUAUpdateFixture", "-format", "UDZO", dmg.path], timeout: 120)
        let work = fm.temporaryDirectory.appendingPathComponent("OpenGCUA-update-" + UUID().uuidString)
        try fm.createDirectory(at: work, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        defer { try? fm.removeItem(at: work) }
        let staged = try await Task.detached {
            try AppUpdateInstaller.prepare(dmg: dmg, installed: installed, version: "0.1.10", directory: work)
        }.value
        precondition(fm.fileExists(atPath: staged.path))
        print("DMG prepared; starting helper…"); fflush(stdout)
        let helper = try await AppUpdateInstaller.launchHelper(installed: installed, staged: staged, directory: work)
        precondition(helper.isRunning && fm.fileExists(atPath: work.appendingPathComponent("ready").path))
        try expect(try version(installed) == "0.1.2", "Helper must wait for the running app to exit")
        print("Helper ready and waiting; stopping test helper…"); fflush(stdout)
        helper.terminate(); helper.waitUntilExit()
        print("Checking tampered signature…"); fflush(stdout)
        // A tampered signed app must fail before anything is installed.
        try Data("tampered".utf8).append(to: source.appendingPathComponent("Contents/MacOS/Fixture"))
        do {
            try AppUpdateInstaller.validate(source, installed: installed, version: "0.1.10")
            preconditionFailure("Tampered signature was accepted")
        } catch AppUpdateError.signature {}
        print("Checking atomic swap and rollback…"); fflush(stdout)
        enum FixtureError: Error { case launch }
        do {
            try await AppUpdateInstaller.replaceAndRelaunch(installed: installed, staged: staged) { url in
                try expect(try version(url) == "0.1.10")
                try expect(try version(staged) == "0.1.2")
                throw FixtureError.launch
            }
            preconditionFailure("Failed launch must roll back")
        } catch FixtureError.launch {}
        try expect(try version(installed) == "0.1.2" && version(staged) == "0.1.10")
        try await AppUpdateInstaller.replaceAndRelaunch(installed: installed, staged: staged) { url in
            try expect(try version(url) == "0.1.10")
        }
        try expect(try version(installed) == "0.1.10" && !fm.fileExists(atPath: staged.path))
    }
    static func version(_ app: URL) throws -> String {
        let info = try PropertyListSerialization.propertyList(from: Data(contentsOf: app.appendingPathComponent("Contents/Info.plist")), format: nil) as! [String: Any]
        return info["CFBundleShortVersionString"] as! String
    }
    @MainActor static func main() async throws {
        if CommandLine.arguments.dropFirst().first == "--install-update" { exit(await AppUpdateInstaller.runHelper(CommandLine.arguments)) }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("OpenGCUA-contracts-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        print("Checking release selection and integrity…"); fflush(stdout)
        try releaseContracts(directory)
        print("Checking update scheduling and HTTP failures…"); fflush(stdout)
        try await checkerContracts()
        try dialogContracts()
        print("Checking signed DMG installation and helper handoff…"); fflush(stdout)
        try await installerContracts(directory)
        print("App update contracts passed: release selection, trusted assets, checksums, throttling, manual checks, task guard, signed DMG staging, helper handoff, atomic install and rollback.")
    }
}

private extension Data {
    func append(to url: URL) throws {
        let handle = try FileHandle(forWritingTo: url)
        defer { try? handle.close() }
        try handle.seekToEnd(); try handle.write(contentsOf: self)
    }
}
