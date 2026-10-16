import Foundation
import Combine

@MainActor final class AppModel: ObservableObject {
    var preferences = Preferences()
    var error = ""
    func stopSpeech() {}
    func refreshPermissions() {}
    func refreshAudioInputs() {}
}
enum AppError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case let .message(s) = self { return s }; return nil }
}

@main enum SettingsEditorContracts {
    @MainActor static func main() throws {
        let model = AppModel()
        let original = model.preferences.normalizedEndpoint
        var keys = [original: "original-key"]
        var saved: Preferences?
        let editor = SettingsEditor(model: model, readKey: { keys[$0] ?? "" },
                                    saveKey: { key, endpoint in keys[endpoint] = key },
                                    savePreferences: { saved = $0 })
        editor.load(includeVoice: false)
        precondition(editor.modelKey == "original-key")
        editor.draft.endpoint = "https://second.example/v1/"
        editor.endpointChanged()
        precondition(editor.modelKey.isEmpty)
        try editor.persist(includeVoice: false)
        precondition(keys[original] == "original-key" && keys["https://second.example/v1"] == nil)
        precondition(saved?.endpoint == "https://second.example/v1")
        editor.modelKey = "second-key"
        try editor.validateModel(); try editor.persist(includeVoice: false)
        precondition(keys["https://second.example/v1"] == "second-key" && keys[original] == "original-key")

        let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".json")
        defer { try? FileManager.default.removeItem(at: file) }
        try Data(#"{"llm_base_url":"https://file.example/v1","api_key":"file-only-key"}"#.utf8).write(to: file)
        editor.draft.configFilePath = file.path
        editor.load(includeVoice: false)
        precondition(editor.fileHasKey && editor.modelKey.isEmpty)
        try editor.validateModel(); try editor.persist(includeVoice: false)
        precondition(!keys.values.contains("file-only-key"), "A JSON key must never be copied to Keychain")
        try Data(#"{"llm_base_url":"https://file.example/v1"}"#.utf8).write(to: file)
        editor.modelKey = "file-endpoint-fallback"
        try editor.validateModel(); try editor.persist(includeVoice: false)
        precondition(keys["https://file.example/v1"] == "file-endpoint-fallback")
        precondition(keys["https://second.example/v1"] == "second-key")
        let count = keys.count
        editor.modelKey = "unsaved-draft"
        precondition(keys.count == count && keys["https://file.example/v1"] == "file-endpoint-fallback")
        print("Settings draft, cancellation, endpoint isolation and JSON credential contracts passed")
    }
}
