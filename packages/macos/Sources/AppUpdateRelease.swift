import Foundation
import CryptoKit

struct AppReleaseVersion: Comparable, Equatable {
    let components: [Int]
    init?(_ value: String) {
        guard value.range(of: #"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"#, options: .regularExpression) != nil else { return nil }
        let numbers = value.split(separator: ".").compactMap { Int($0) }
        guard numbers.count == 3 else { return nil }
        components = numbers
    }
    static func < (lhs: Self, rhs: Self) -> Bool { lhs.components.lexicographicallyPrecedes(rhs.components) }
}

struct AppUpdateRelease: Equatable {
    static let repository = "https://github.com/Tongyi-MAI/OpenGCUA"
    static let assetName = "OpenGCUA-macOS-arm64.dmg"
    let version: String
    let downloadURL: URL
    let releaseURL: URL
    let size: Int64
    let digest: String?
    let checksumURL: URL?

    enum Result: Equatable { case current, preparing(String), available(AppUpdateRelease) }

    private struct Release: Decodable {
        struct Asset: Decodable {
            let name: String
            let state: String
            let size: Int64
            let browser_download_url: String
            let digest: String?
        }
        let tag_name: String
        let html_url: String
        let draft: Bool
        let prerelease: Bool
        let published_at: String?
        let assets: [Asset]
    }

    static func parse(_ data: Data, currentVersion: String) throws -> Result {
        guard let current = AppReleaseVersion(currentVersion) else { throw AppUpdateError.invalidVersion }
        let release = try JSONDecoder().decode(Release.self, from: data)
        guard !release.draft, !release.prerelease, release.published_at != nil,
              release.tag_name.hasPrefix("v"),
              let version = AppReleaseVersion(String(release.tag_name.dropFirst())), version > current else { return .current }
        let tag = release.tag_name
        let prefix = repository + "/releases/download/" + tag + "/"
        guard release.html_url == repository + "/releases/tag/" + tag,
              let page = URL(string: release.html_url) else { throw AppUpdateError.invalidRelease }
        guard let asset = release.assets.first(where: { $0.name == assetName && $0.state == "uploaded" && $0.size > 0 }) else {
            return .preparing(String(tag.dropFirst()))
        }
        guard asset.browser_download_url == prefix + assetName,
              let download = URL(string: asset.browser_download_url) else { throw AppUpdateError.invalidRelease }
        let digest = asset.digest.flatMap { $0.hasPrefix("sha256:") ? String($0.dropFirst(7)).lowercased() : nil }
        if let digest, !isSHA256(digest) { throw AppUpdateError.invalidRelease }
        let checksum = release.assets.first {
            $0.name == assetName + ".sha256" && $0.state == "uploaded" && $0.size > 0 && $0.size <= 1024
                && $0.browser_download_url == prefix + assetName + ".sha256"
        }.flatMap { URL(string: $0.browser_download_url) }
        guard digest != nil || checksum != nil else { return .preparing(String(tag.dropFirst())) }
        return .available(Self(version: String(tag.dropFirst()), downloadURL: download, releaseURL: page,
                               size: asset.size, digest: digest, checksumURL: checksum))
    }

    static func isSHA256(_ value: String) -> Bool { value.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil }
    static func checksum(_ data: Data) throws -> String {
        guard data.count <= 1024, let text = String(data: data, encoding: .utf8) else { throw AppUpdateError.checksum }
        let fields = text.split(whereSeparator: \.isWhitespace).map(String.init)
        guard fields.count == 2, isSHA256(fields[0].lowercased()), fields[1] == assetName else { throw AppUpdateError.checksum }
        return fields[0].lowercased()
    }
    static func verify(_ file: URL, digest: String, size: Int64) throws {
        let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
        guard (attributes[.size] as? NSNumber)?.int64Value == size else { throw AppUpdateError.checksum }
        let handle = try FileHandle(forReadingFrom: file)
        defer { try? handle.close() }
        var hash = SHA256()
        while let block = try handle.read(upToCount: 1024 * 1024), !block.isEmpty { hash.update(data: block) }
        guard hash.finalize().map({ String(format: "%02x", $0) }).joined() == digest else { throw AppUpdateError.checksum }
    }
}

enum AppUpdateError: LocalizedError {
    case invalidVersion, invalidRelease, checksum, request(Int), installLocation, invalidApp, signature, installation, timeout
    var errorDescription: String? {
        switch self {
        case .invalidVersion: return L("无法识别当前应用版本。")
        case .invalidRelease: return L("更新发布信息无效，请稍后重试。")
        case .checksum: return L("更新文件校验失败，请重新下载。")
        case let .request(status): return L("更新服务暂不可用（%@），请稍后重试。", String(status))
        case .installLocation: return L("请将 OpenGCUA 移到可写的“应用程序”文件夹后重试更新。")
        case .invalidApp: return L("更新与此应用或 macOS 版本不兼容。")
        case .signature: return L("更新的应用签名校验失败。")
        case .installation: return L("更新未能安装，原应用已保留。")
        case .timeout: return L("更新操作超时，请重试。")
        }
    }
}

struct AppReleaseService {
    static let endpoint = URL(string: "https://api.github.com/repos/Tongyi-MAI/OpenGCUA/releases/latest")!
    let session: URLSession
    init(session: URLSession? = nil) {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 15 * 60
        self.session = session ?? URLSession(configuration: configuration)
    }
    func latest(currentVersion: String) async throws -> AppUpdateRelease.Result {
        var request = URLRequest(url: Self.endpoint, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("OpenGCUA/" + currentVersion, forHTTPHeaderField: "User-Agent")
        let (data, response) = try await session.data(for: request)
        if (response as? HTTPURLResponse)?.statusCode == 404 { return .current }
        try Self.validate(response)
        guard data.count <= 1024 * 1024 else { throw AppUpdateError.invalidRelease }
        return try AppUpdateRelease.parse(data, currentVersion: currentVersion)
    }
    func download(_ release: AppUpdateRelease, to directory: URL) async throws -> URL {
        let digest: String
        if let known = release.digest { digest = known }
        else if let url = release.checksumURL {
            let (data, response) = try await session.data(from: url)
            try Self.validate(response)
            digest = try AppUpdateRelease.checksum(data)
        } else { throw AppUpdateError.checksum }
        let (temporary, response) = try await session.download(from: release.downloadURL)
        defer { try? FileManager.default.removeItem(at: temporary) }
        try Self.validate(response)
        try Task.checkCancellation()
        try AppUpdateRelease.verify(temporary, digest: digest, size: release.size)
        let destination = directory.appendingPathComponent(AppUpdateRelease.assetName)
        try FileManager.default.moveItem(at: temporary, to: destination)
        return destination
    }
    private static func validate(_ response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse, http.statusCode == 200, http.url?.scheme == "https" else {
            throw AppUpdateError.request((response as? HTTPURLResponse)?.statusCode ?? 0)
        }
    }
}
