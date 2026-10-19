import Foundation
import Combine

/// Stable persisted choices; localized labels never become preference values.
final class AppLanguage: ObservableObject {
    static let shared = AppLanguage()
    @Published private(set) var selection: String
    static let choices = ["system", "en", "zh-Hans"]

    private init() {
        let data = UserDefaults.standard.data(forKey: "preferences")
        let object = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        let saved = object?["language"] as? String ?? "system"
        selection = Self.choices.contains(saved) ? saved : "system"
    }
    func select(_ value: String) { selection = Self.choices.contains(value) ? value : "system" }
}

enum L10n {
    static func resolve(_ selection: String, preferredLanguages: [String] = Locale.preferredLanguages) -> String {
        if selection == "en" || selection == "zh-Hans" { return selection }
        for language in preferredLanguages {
            let code = language.lowercased().replacingOccurrences(of: "_", with: "-")
            if code == "en" || code.hasPrefix("en-") { return "en" }
            if code == "zh" || code.hasPrefix("zh-") { return "zh-Hans" }
        }
        return "en"
    }
    private static let cacheLock = NSLock()
    private static var sourceKeyCache: [URL: [String: String]] = [:]
    private static func sourceKey(_ value: String, bundle: Bundle) -> String {
        cacheLock.lock(); defer { cacheLock.unlock() }
        if sourceKeyCache[bundle.bundleURL] == nil {
            let url = bundle.url(forResource: "Localizable", withExtension: "strings", subdirectory: "en.lproj")
            let values = url.flatMap { try? Data(contentsOf: $0) }.flatMap {
                try? PropertyListSerialization.propertyList(from: $0, format: nil) as? [String: String]
            } ?? [:]
            sourceKeyCache[bundle.bundleURL] = Dictionary(values.map { ($0.value, $0.key) }, uniquingKeysWith: { first, _ in first })
        }
        return sourceKeyCache[bundle.bundleURL]?[value] ?? value
    }
    static var languageCode: String { resolve(AppLanguage.shared.selection) }
    static var locale: Locale { AppLanguage.shared.selection == "system" ? .current : Locale(identifier: languageCode) }
    static func text(_ key: String, arguments: [String] = [], language: String? = nil, bundle: Bundle = .main) -> String {
        let code = language ?? languageCode
        let key = sourceKey(key, bundle: bundle)
        let localized = bundle.path(forResource: code, ofType: "lproj").flatMap(Bundle.init(path:))
        let english = bundle.path(forResource: "en", ofType: "lproj").flatMap(Bundle.init(path:))
        let fallback = english?.localizedString(forKey: key, value: key, table: nil) ?? key
        let value = localized?.localizedString(forKey: key, value: fallback, table: nil) ?? fallback
        guard !arguments.isEmpty else { return value }
        return String(format: value, locale: Locale(identifier: code), arguments: arguments.map { $0 as CVarArg })
    }
}

func L(_ key: String, _ arguments: String...) -> String { L10n.text(key, arguments: arguments) }
