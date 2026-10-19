import AppKit
import Combine
import UniformTypeIdentifiers

@MainActor final class SettingsEditor: ObservableObject {
    @Published var draft: Preferences
    @Published var modelKey = ""
    @Published var speechKey = ""
    @Published var error = ""
    private var loadedModelAccount = ""
    private var loadedSpeechAccount = ""
    let model: AppModel
    private let readKey: (String) throws -> String
    private let saveKey: (String, String) throws -> Void
    private let savePreferences: (Preferences) -> Void

    init(model: AppModel, readKey: @escaping (String) throws -> String = CredentialStore.read,
         saveKey: @escaping (String, String) throws -> Void = CredentialStore.save,
         savePreferences: @escaping (Preferences) -> Void = { $0.save() }) {
        self.model = model; draft = model.preferences
        self.readKey = readKey; self.saveKey = saveKey; self.savePreferences = savePreferences
    }
    var configuration: [String: Any]? { try? draft.taskConfiguration() }
    var modelAccount: String { configuration?["llm_base_url"] as? String ?? draft.normalizedEndpoint }
    var fileHasKey: Bool { !(configuration?["api_key"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    var sharesSpeechKey: Bool { draft.speechCredentialAccount == modelAccount }

    func load(includeVoice: Bool) {
        model.stopSpeech(); model.refreshPermissions()
        loadModelKey()
        if includeVoice { model.refreshAudioInputs(); loadSpeechKey() }
    }
    func loadModelKey() {
        do { modelKey = try readKey(modelAccount); loadedModelAccount = modelAccount }
        catch { modelKey = ""; loadedModelAccount = ""; self.error = error.localizedDescription }
    }
    func loadSpeechKey() {
        speechKey = ""; loadedSpeechAccount = ""
        guard !sharesSpeechKey else { return }
        do { speechKey = try readKey(draft.speechCredentialAccount); loadedSpeechAccount = draft.speechCredentialAccount }
        catch { self.error = error.localizedDescription }
    }
    func endpointChanged() {
        if modelAccount != loadedModelAccount { modelKey = ""; loadedModelAccount = "" }
    }
    func chooseConfiguration() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.json]; panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false; panel.prompt = L("选择配置")
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            var candidate = draft; candidate.configFilePath = url.path
            _ = try candidate.taskConfiguration()
            draft = candidate; error = ""; loadModelKey()
        } catch { self.error = error.localizedDescription }
    }
    func useManualConfiguration() { draft.configFilePath = ""; error = ""; loadModelKey() }
    func validateModel() throws {
        // A JSON file can select a different endpoint from the manual defaults.
        // An edited fallback key belongs only to the effective endpoint shown.
        try draft.validateTaskCredentials(manualKey: "", readKey: { endpoint in
            if endpoint == self.modelAccount && !self.modelKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return self.modelKey }
            return try self.readKey(endpoint)
        })
    }
    func persist(includeVoice: Bool) throws {
        try draft.validate(); _ = try draft.taskConfiguration()
        draft.endpoint = draft.normalizedEndpoint
        if loadedModelAccount == modelAccount || !modelKey.isEmpty {
            try saveKey(modelKey.trimmingCharacters(in: .whitespacesAndNewlines), modelAccount)
            loadedModelAccount = modelAccount
        }
        if includeVoice && !sharesSpeechKey && (loadedSpeechAccount == draft.speechCredentialAccount || !speechKey.isEmpty) {
            try saveKey(speechKey.trimmingCharacters(in: .whitespacesAndNewlines), draft.speechCredentialAccount)
            loadedSpeechAccount = draft.speechCredentialAccount
        }
        model.stopSpeech(); savePreferences(draft); model.preferences = draft; model.error = ""; error = ""
    }
}
