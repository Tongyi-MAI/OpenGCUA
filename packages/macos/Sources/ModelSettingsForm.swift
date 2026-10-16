import AppKit
import SwiftUI

@MainActor struct ModelSettingsForm: View {
    @ObservedObject var editor: SettingsEditor
    var introductory = false
    var body: some View {
        Form {
            if editor.draft.configFilePath.isEmpty {
                Section {
                    TextField("服务地址", text: $editor.draft.endpoint).help("模型服务的 HTTPS 地址；本机服务可使用 HTTP")
                    TextField("模型名称", text: $editor.draft.model)
                    SecureField("API Key", text: $editor.modelKey)
                    Toggle("此服务不需要 API Key", isOn: $editor.draft.allowEmptyKey)
                    if introductory {
                        DisclosureGroup("协议与推理设置") { protocolFields }
                    } else { protocolFields }
                } header: { Text("模型服务") }
                footer: { Text("API Key 保存在 macOS 钥匙串中。执行任务时，相关界面和任务内容会发送到此服务。") }
                Section {
                    Button("选择 JSON 配置…", systemImage: "doc.badge.gearshape", action: editor.chooseConfiguration)
                } header: { Text("已有配置？") }
                footer: { Text("可以使用源代码 harness 的 local.json，应用会在每个新任务前重新读取。") }
            } else {
                Section("配置文件") {
                    HStack(spacing: 12) {
                        Image(systemName: "doc.text").font(.title2).foregroundStyle(.secondary)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(URL(fileURLWithPath: editor.draft.configFilePath).lastPathComponent).font(.headline)
                            Text(editor.draft.configFilePath).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                        }
                        Spacer()
                        Button("更改…", action: editor.chooseConfiguration)
                    }
                    Button("使用手动配置", action: editor.useManualConfiguration)
                }
                Section {
                    if let config = editor.configuration {
                        LabeledContent("服务地址", value: config["llm_base_url"] as? String ?? "").lineLimit(1).help(editor.modelAccount)
                        LabeledContent("模型名称", value: config["model_name"] as? String ?? "")
                        LabeledContent("模型协议", value: config["llm_protocol"] as? String ?? "")
                        LabeledContent("Reasoning effort", value: config["reasoning_effort"] as? String ?? "low")
                        if editor.fileHasKey { Label("配置文件已提供 API Key", systemImage: "key.fill").foregroundStyle(.secondary) }
                        else { SecureField("备用 API Key", text: $editor.modelKey).help("保存在此文件所选服务地址对应的钥匙串项目中") }
                    } else { Label("无法读取配置，请重新选择文件。", systemImage: "exclamationmark.triangle").foregroundStyle(.orange) }
                } header: { Text("当前配置") }
                footer: { Text("文件配置优先；文件中的密钥不会复制到钥匙串。备用 API Key 仅用于文件没有提供密钥时。") }
            }
        }.formStyle(.grouped).textFieldStyle(.roundedBorder)
            .onChange(of: editor.draft.endpoint) { _, _ in editor.endpointChanged() }
    }
    private var protocolFields: some View {
        Group {
            Picker("模型协议", selection: $editor.draft.llmProtocol) {
                Text("OpenAI compatible").tag("openai"); Text("Anthropic Messages").tag("anthropic")
            }
            Picker("Reasoning effort", selection: $editor.draft.reasoningEffort) {
                ForEach(Preferences.reasoningEfforts, id: \.self) { Text($0).tag($0) }
            }.help("默认 low；支持的档位取决于模型服务")
        }
    }
}
