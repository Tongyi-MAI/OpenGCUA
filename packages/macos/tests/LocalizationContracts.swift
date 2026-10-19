import Foundation

enum AppError: Error { case message(String) }

@main enum LocalizationContracts {
    static func main() throws {
        precondition(L10n.resolve("system", preferredLanguages: ["en-SG", "zh-Hans"]) == "en")
        precondition(L10n.resolve("system", preferredLanguages: ["zh-Hans-CN", "en"]) == "zh-Hans")
        precondition(L10n.resolve("system", preferredLanguages: ["ja-JP", "fr-FR"]) == "en")
        precondition(L10n.resolve("system", preferredLanguages: ["fr-FR", "zh-TW", "en-US"]) == "zh-Hans")
        precondition(L10n.resolve("en", preferredLanguages: ["zh-Hans"]) == "en")
        precondition(L10n.resolve("zh-Hans", preferredLanguages: ["en-US"]) == "zh-Hans")
        precondition(L10n.resolve("invalid", preferredLanguages: ["en-US"]) == "en")
        let legacy = try JSONDecoder().decode(Preferences.self, from: Data(#"{"appearance":"浅色","speechRegion":"新加坡"}"#.utf8))
        precondition(legacy.language == "system" && legacy.appearance == "浅色" && legacy.speechHost == "dashscope-intl.aliyuncs.com")
        var english = legacy; english.language = "en"
        let roundtrip = try JSONDecoder().decode(Preferences.self, from: JSONEncoder().encode(english))
        precondition(roundtrip.language == "en")
        let invalid = try JSONDecoder().decode(Preferences.self, from: Data(#"{"language":"unsupported"}"#.utf8))
        precondition(invalid.language == "system")
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".bundle")
        defer { try? FileManager.default.removeItem(at: directory) }
        let resources = directory.appendingPathComponent("Contents/Resources")
        try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
        let info: [String: Any] = ["CFBundleIdentifier":"app.generalist.localization-tests", "CFBundleDevelopmentRegion":"en", "CFBundleLocalizations":["en","zh-Hans"]]
        try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0).write(to: directory.appendingPathComponent("Contents/Info.plist"))
        let source = URL(fileURLWithPath: CommandLine.arguments[1])
        for code in ["en", "zh-Hans"] {
            try FileManager.default.copyItem(at: source.appendingPathComponent(code + ".lproj"), to: resources.appendingPathComponent(code + ".lproj"))
        }
        let bundle = Bundle(url: directory)!
        precondition(L10n.text("准备就绪", language: "en", bundle: bundle) == "Ready")
        precondition(L10n.text("准备就绪", language: "zh-Hans", bundle: bundle) == "准备就绪")
        precondition(L10n.text("Ready", language: "zh-Hans", bundle: bundle) == "准备就绪", "Cached UI status must switch back from English")
        precondition(L10n.text("第 %@ 步", arguments: ["12"], language: "en", bundle: bundle) == "Step 12")
        precondition(L10n.text("状态：%@；任务：%@；问题：%@；结果：%@", arguments: ["Ready","test","?","done"], language: "en", bundle: bundle) == "State: Ready; Task: test; Question: ?; Result: done")
        precondition(L10n.text("对话记录", language: "unsupported", bundle: bundle) == "Chat History")
        precondition(L10n.text("A user’s original chat message", language: "zh-Hans", bundle: bundle) == "A user’s original chat message")
        AppLanguage.shared.select("en")
        precondition(L10n.languageCode == "en" && (english.speechSession["instructions"] as! String).contains("en"))
        AppLanguage.shared.select("zh-Hans")
        precondition(L10n.languageCode == "zh-Hans")
        print("Localization contracts passed: system preference negotiation, explicit overrides, legacy settings, persisted choice, packaged translations, interpolation, reverse switching and English fallback.")
    }
}
