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
        let enData = try Data(contentsOf: source.appendingPathComponent("en.lproj/Localizable.strings"))
        let zhData = try Data(contentsOf: source.appendingPathComponent("zh-Hans.lproj/Localizable.strings"))
        let en = try PropertyListSerialization.propertyList(from: enData, format: nil) as! [String: String]
        let zh = try PropertyListSerialization.propertyList(from: zhData, format: nil) as! [String: String]
        precondition(Set(en.keys) == Set(zh.keys), "两种语言的本地化键必须完整对齐")
        for key in ["会话", "不选", "允许前台操作（仅本实时会话）", "允许助手提问", "前台操作审批", "中止本轮", "继续队列", "手动压缩上下文", "显示推理", "显示代码", "诊断", "实际模型"] {
            precondition(en[key]?.isEmpty == false && zh[key]?.isEmpty == false)
        }
        for (key, value) in en {
            precondition(key.components(separatedBy: "%@").count == value.components(separatedBy: "%@").count, "插值参数必须保持一致：" + key)
            precondition(key.components(separatedBy: "%@").count == zh[key]!.components(separatedBy: "%@").count, "中文插值参数必须保持一致：" + key)
        }
        let literalKey = try NSRegularExpression(pattern: #"\bL\(("(?:\\.|[^"\\])*")"#)
        let swiftSources = source.deletingLastPathComponent().appendingPathComponent("Sources")
        for file in try FileManager.default.contentsOfDirectory(at: swiftSources, includingPropertiesForKeys: nil) where file.pathExtension == "swift" {
            let text = try String(contentsOf: file, encoding: .utf8)
            for match in literalKey.matches(in: text, range: NSRange(text.startIndex..<text.endIndex, in: text)) {
                let range = Range(match.range(at: 1), in: text)!
                let key = try JSONDecoder().decode(String.self, from: Data(text[range].utf8))
                precondition(en[key]?.isEmpty == false && zh[key]?.isEmpty == false, "原生 UI 本地化缺失：" + file.lastPathComponent + " · " + key)
            }
        }
        for code in ["en", "zh-Hans"] {
            try FileManager.default.copyItem(at: source.appendingPathComponent(code + ".lproj"), to: resources.appendingPathComponent(code + ".lproj"))
        }
        let bundle = Bundle(url: directory)!
        precondition(L10n.text("中止本轮", language: "en", bundle: bundle) == "Interrupt turn")
        precondition(L10n.text("消息队列（%@）", arguments: ["2"], language: "en", bundle: bundle) == "Message queue (2)")
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
