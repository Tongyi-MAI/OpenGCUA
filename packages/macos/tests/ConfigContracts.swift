import Foundation

enum AppError: Error { case message(String) }

@main enum ConfigContracts {
    static func main() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let file = root.appendingPathComponent("local.json")
        var preferences = try JSONDecoder().decode(Preferences.self, from: Data(#"{"model":"old"}"#.utf8))
        precondition(preferences.llmProtocol == "openai" && preferences.configFilePath.isEmpty)
        precondition(preferences.reasoningEffort == "low")
        precondition(preferences.sendWithEnter)
        let suite = "Generalist-setup-test-" + UUID().uuidString
        let defaultsStore = UserDefaults(suiteName: suite)!
        defer { defaultsStore.removePersistentDomain(forName: suite) }
        precondition(FirstLaunch.needsSetup(in: defaultsStore))
        precondition(FirstLaunch.progress(in: defaultsStore) == .welcome)
        FirstLaunch.saveProgress(.permissions, in: defaultsStore)
        precondition(FirstLaunch.progress(in: UserDefaults(suiteName: suite)!) == .permissions)
        // Saving model preferences alone must never mark setup as complete.
        defaultsStore.set(Data(), forKey: "preferences")
        precondition(FirstLaunch.needsSetup(in: defaultsStore))
        FirstLaunch.complete(in: defaultsStore)
        precondition(!FirstLaunch.needsSetup(in: UserDefaults(suiteName: suite)!))
        precondition(FirstLaunch.progress(in: defaultsStore) == .welcome)
        do { try preferences.validateTaskCredentials(manualKey: "", readKey: { _ in "" }); preconditionFailure("Missing key accepted") }
        catch {}
        try preferences.validateTaskCredentials(manualKey: "fixture", readKey: { _ in preconditionFailure("Unnecessary key lookup") })
        try preferences.validateTaskCredentials(manualKey: "", readKey: { _ in "fixture" })
        preferences.allowEmptyKey = true
        try preferences.validateTaskCredentials(manualKey: "", readKey: { _ in preconditionFailure("Unnecessary key lookup") })
        preferences.allowEmptyKey = false
        let defaults = try preferences.taskConfiguration()
        precondition(defaults["reasoning_effort"] as? String == "low")
        preferences.reasoningEffort = "medium"
        preferences.sendWithEnter = false
        preferences.configFilePath = file.path
        try Data(#"{"llm_base_url":"https://first.example/v1/","model_name":"first","llm_protocol":"anthropic","reasoning_effort":"high","api_key":"fixture-secret","max_tokens":4096,"_note":"ignored"}"#.utf8).write(to: file)
        let first = try preferences.taskConfiguration()
        precondition(first["llm_base_url"] as? String == "https://first.example/v1")
        precondition(first["llm_protocol"] as? String == "anthropic")
        precondition(first["reasoning_effort"] as? String == "high")
        precondition(first["max_tokens"] as? Int == 4096 && first["_note"] == nil)
        try preferences.validateTaskCredentials(manualKey: "", readKey: { _ in preconditionFailure("File key should take precedence") })
        let saved = try JSONEncoder().encode(preferences)
        precondition(!String(decoding: saved, as: UTF8.self).contains("fixture-secret"))
        let restored = try JSONDecoder().decode(Preferences.self, from: saved)
        precondition(restored.configFilePath == file.path)
        precondition(restored.reasoningEffort == "medium")
        precondition(!restored.sendWithEnter)
        // A second task must use changed file contents without restarting the app.
        try Data(#"{"llm_base_url":"https://second.example/v1","model_name":"second","llm_protocol":"openai"}"#.utf8).write(to: file)
        let second = try restored.taskConfiguration()
        precondition(second["model_name"] as? String == "second")
        precondition(second["llm_base_url"] as? String == "https://second.example/v1")
        precondition(second["api_key"] == nil)
        precondition(second["reasoning_effort"] as? String == "medium")
        do {
            try restored.validateTaskCredentials(manualKey: "wrong-endpoint", readKey: { endpoint in
                precondition(endpoint == "https://second.example/v1"); return ""
            })
            preconditionFailure("Manual key reused for a different file endpoint")
        } catch {}
        for invalid in ["[]", "not json", #"{"llm_protocol":"unsupported"}"#, #"{"api_key":17}"#,
                        #"{"reasoning_effort":"unsupported"}"#, #"{"reasoning_effort":17}"#,
                        #"{"reasoning_effort":null}"#] {
            try Data(invalid.utf8).write(to: file)
            do { _ = try restored.taskConfiguration(); preconditionFailure("Invalid config accepted") }
            catch {}
        }
        try FileManager.default.removeItem(at: file)
        do { _ = try restored.taskConfiguration(); preconditionFailure("Missing file accepted") }
        catch {}
        preferences.configFilePath = ""
        preferences.llmProtocol = "anthropic"
        let manual = try preferences.taskConfiguration()
        precondition(manual["llm_protocol"] as? String == "anthropic")
        for effort in Preferences.reasoningEfforts {
            preferences.reasoningEffort = effort
            let config = try preferences.taskConfiguration()
            precondition(config["reasoning_effort"] as? String == effort)
        }
        print("Configuration reload, override, validation and credential persistence contracts passed")
    }
}
