import Foundation

enum SetupStep: Int, CaseIterable { case welcome, model, permissions }

enum FirstLaunch {
    private static let key = "completedSetupVersion"
    static func needsSetup(in defaults: UserDefaults = .standard) -> Bool {
        defaults.integer(forKey: key) < 1
    }
    static func progress(in defaults: UserDefaults = .standard) -> SetupStep {
        SetupStep(rawValue: defaults.integer(forKey: "setupProgress")) ?? .welcome
    }
    static func saveProgress(_ step: SetupStep, in defaults: UserDefaults = .standard) { defaults.set(step.rawValue, forKey: "setupProgress") }
    static func complete(in defaults: UserDefaults = .standard) {
        defaults.set(1, forKey: key); defaults.removeObject(forKey: "setupProgress")
    }
}
