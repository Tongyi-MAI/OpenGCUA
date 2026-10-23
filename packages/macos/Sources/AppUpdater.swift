import AppKit
import Combine
import Foundation

@MainActor final class AppUpdater: ObservableObject {
    enum State { case idle, checking, downloading, preparing, ready, relaunching }
    @Published private(set) var state: State = .idle
    @Published private(set) var available: AppUpdateRelease?
    @Published private(set) var message = ""
    @Published private(set) var isError = false
    @Published private(set) var showsPrompt = false
    @Published var automaticChecks: Bool {
        didSet {
            defaults.set(automaticChecks, forKey: "automaticAppUpdateChecks")
            if automaticChecks { checkIfDue() }
        }
    }
    let currentVersion: String
    private let defaults: UserDefaults
    private let service: AppReleaseService
    private let now: () -> Date
    private var timer: Timer?
    private var operation: Task<Void, Never>?
    private var staged: URL?
    private var directory: URL?
    var onChange: (() -> Void)?
    var canInstall: () -> Bool = { false }
    var onRelaunch: (() -> Void)?
    var busy: Bool { [.checking, .downloading, .preparing, .relaunching].contains(state) }
    static let interval: TimeInterval = 6 * 60 * 60

    init(defaults: UserDefaults = .standard, service: AppReleaseService = AppReleaseService(),
         currentVersion: String = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "",
         now: @escaping () -> Date = Date.init) {
        self.defaults = defaults; self.service = service; self.currentVersion = currentVersion; self.now = now
        automaticChecks = defaults.object(forKey: "automaticAppUpdateChecks") as? Bool ?? true
        if let previous = defaults.string(forKey: "lastAppUpdateError") {
            message = L(previous); isError = true
            defaults.removeObject(forKey: "lastAppUpdateError")
        }
    }
    func start() {
        guard timer == nil else { return }
        timer = Timer.scheduledTimer(withTimeInterval: 30 * 60, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.checkIfDue() }
        }
        checkIfDue()
    }
    func stop() {
        timer?.invalidate(); timer = nil; operation?.cancel()
        if state != .relaunching {
            let candidate = staged, work = directory
            Task.detached(priority: .utility) {
                if let candidate { try? FileManager.default.removeItem(at: candidate) }
                if let work { try? FileManager.default.removeItem(at: work) }
            }
        }
    }
    func checkIfDue() {
        guard automaticChecks else { return }
        if state == .ready, let available {
            showsPrompt = shouldPrompt(for: available.version); onChange?(); return
        }
        let last = defaults.object(forKey: "lastAppUpdateCheck") as? Date ?? .distantPast
        guard now().timeIntervalSince(last) >= Self.interval else { return }
        check(manual: false)
    }
    func check(manual: Bool = true) {
        if manual && (state == .ready || busy) { showsPrompt = available != nil; onChange?(); return }
        guard !busy, state != .ready else { return }
        state = .checking
        if manual { message = L("正在检查更新…"); isError = false }
        defaults.set(now(), forKey: "lastAppUpdateCheck")
        operation = Task { [weak self] in
            guard let self else { return }
            defer { state = .idle; onChange?() }
            do {
                let result = try await service.latest(currentVersion: currentVersion)
                try Task.checkCancellation()
                switch result {
                case .current:
                    available = nil; showsPrompt = false
                    if manual { message = L("当前没有可用的应用更新。"); isError = false }
                case let .preparing(version):
                    available = nil; showsPrompt = false
                    if manual { message = L("版本 %@ 的 macOS 安装包尚未就绪，请稍后检查。", version); isError = false }
                case let .available(release):
                    available = release
                    showsPrompt = manual || shouldPrompt(for: release.version)
                    message = L("OpenGCUA %@ 可用", release.version); isError = false
                }
            } catch is CancellationError {} catch {
                // Offline and rate-limit failures do not interrupt background work.
                if manual { message = L("检查更新失败：%@", error.localizedDescription); isError = true }
            }
        }
    }
    func shouldPrompt(for version: String) -> Bool {
        guard defaults.string(forKey: "skippedAppUpdateVersion") != version else { return false }
        return defaults.string(forKey: "snoozedAppUpdateVersion") != version
            || now() >= (defaults.object(forKey: "appUpdateRemindAfter") as? Date ?? .distantPast)
    }
    func requestPrompt() {
        guard available != nil, !busy else { return }
        showsPrompt = true; onChange?()
    }
    func remindLater() {
        guard let available else { return }
        defaults.set(available.version, forKey: "snoozedAppUpdateVersion")
        defaults.set(now().addingTimeInterval(24 * 60 * 60), forKey: "appUpdateRemindAfter")
        showsPrompt = false; onChange?()
    }
    func skipVersion() {
        guard let available, state == .idle else { return }
        defaults.set(available.version, forKey: "skippedAppUpdateVersion")
        showsPrompt = false; onChange?()
    }
    func install() {
        guard !busy, let release = available, canInstall() else { return }
        if state == .ready { relaunch(); return }
        state = .downloading; isError = false; showsPrompt = true
        message = L("正在下载更新…")
        let installed = Bundle.main.bundleURL.resolvingSymlinksInPath()
        operation = Task { [weak self] in
            guard let self else { return }
            var work: URL?
            var candidate: URL?
            do {
                try AppUpdateInstaller.checkLocation(installed)
                let root = FileManager.default.temporaryDirectory.appendingPathComponent("OpenGCUA-update-" + UUID().uuidString, isDirectory: true)
                try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
                work = root
                let dmg = try await service.download(release, to: root)
                state = .preparing; message = L("正在验证并准备更新…")
                let prepared = try await Task.detached(priority: .utility) {
                    try AppUpdateInstaller.prepare(dmg: dmg, installed: installed, version: release.version, directory: root)
                }.value
                candidate = prepared
                try Task.checkCancellation()
                staged = prepared; directory = root
                state = .ready; message = L("更新已就绪，安装后将重新启动。")
                // A task may have started while the download was in progress.
                // Keep the prepared update until the user can explicitly restart.
                if canInstall() { relaunch() } else { onChange?() }
            } catch {
                if let candidate { try? FileManager.default.removeItem(at: candidate) }
                if let work { try? FileManager.default.removeItem(at: work) }
                state = .idle
                if !(error is CancellationError) { message = L("更新失败：%@", error.localizedDescription); isError = true }
                onChange?()
            }
        }
    }
    private func relaunch() {
        guard state == .ready, canInstall(), let staged, let directory else { return }
        state = .relaunching; message = L("正在安装并重新启动…")
        operation = Task { [weak self] in
            guard let self else { return }
            do {
                let helper = try await AppUpdateInstaller.launchHelper(installed: Bundle.main.bundleURL.resolvingSymlinksInPath(), staged: staged, directory: directory)
                // Recheck after the helper handshake; never quit a newly started task.
                guard canInstall() else {
                    helper.terminate()
                    state = .ready; message = L("更新已就绪，安装后将重新启动。")
                    onChange?()
                    return
                }
                onRelaunch?()
            } catch {
                state = .ready; message = L("更新失败：%@", error.localizedDescription); isError = true; onChange?()
            }
        }
    }
}
