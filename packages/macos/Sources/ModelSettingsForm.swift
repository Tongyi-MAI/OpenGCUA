import AppKit
import SwiftUI

@MainActor struct ModelSettingsForm: View {
    @ObservedObject var editor: SettingsEditor
    @ObservedObject private var language = AppLanguage.shared
    var introductory = false
    var body: some View {
        let _ = language.selection
        Form {
            if editor.draft.configFilePath.isEmpty {
                Section {
                    TextField(L("服务地址"), text: $editor.draft.endpoint).help(L("模型服务的 HTTPS 地址；本机服务可使用 HTTP"))
                    TextField(L("模型名称"), text: $editor.draft.model)
                    SecureField("API Key", text: $editor.modelKey)
                    Toggle(L("此服务不需要 API Key"), isOn: $editor.draft.allowEmptyKey)
                    if introductory {
                        DisclosureGroup(L("协议与推理设置")) { protocolFields }
                    } else { protocolFields }
                } header: { Text(L("模型服务")) }
                footer: { Text(L("API Key 保存在 macOS 钥匙串中。执行任务时，相关界面和任务内容会发送到此服务。")) }
                Section {
                    Button(L("选择 JSON 配置…"), systemImage: "doc.badge.gearshape", action: editor.chooseConfiguration)
                } header: { Text(L("已有配置？")) }
                footer: { Text(L("可以使用源代码 harness 的 local.json，应用会在每个新任务前重新读取。")) }
            } else {
                Section(L("配置文件")) {
                    HStack(spacing: 12) {
                        Image(systemName: "doc.text").font(.title2).foregroundStyle(.secondary)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(URL(fileURLWithPath: editor.draft.configFilePath).lastPathComponent).font(.headline)
                            Text(editor.draft.configFilePath).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                        }
                        Spacer()
                        Button(L("更改…"), action: editor.chooseConfiguration)
                    }
                    Button(L("使用手动配置"), action: editor.useManualConfiguration)
                }
                Section {
                    if let config = editor.configuration {
                        LabeledContent(L("服务地址"), value: config["llm_base_url"] as? String ?? "").lineLimit(1).help(editor.modelAccount)
                        LabeledContent(L("模型名称"), value: config["model_name"] as? String ?? "")
                        LabeledContent(L("模型协议"), value: config["llm_protocol"] as? String ?? "")
                        LabeledContent("Reasoning effort", value: config["reasoning_effort"] as? String ?? "low")
                        if editor.fileHasKey { Label(L("配置文件已提供 API Key"), systemImage: "key.fill").foregroundStyle(.secondary) }
                        else { SecureField(L("备用 API Key"), text: $editor.modelKey).help(L("保存在此文件所选服务地址对应的钥匙串项目中")) }
                    } else { Label(L("无法读取配置，请重新选择文件。"), systemImage: "exclamationmark.triangle").foregroundStyle(.orange) }
                } header: { Text(L("当前配置")) }
                footer: { Text(L("文件配置优先；文件中的密钥不会复制到钥匙串。备用 API Key 仅用于文件没有提供密钥时。")) }
            }
        }.formStyle(.grouped).textFieldStyle(.roundedBorder)
            .onChange(of: editor.draft.endpoint) { _, _ in editor.endpointChanged() }
    }
    private var protocolFields: some View {
        Group {
            Picker(L("模型协议"), selection: $editor.draft.llmProtocol) {
                Text("OpenAI compatible").tag("openai"); Text("Anthropic Messages").tag("anthropic")
            }
            Picker("Reasoning effort", selection: $editor.draft.reasoningEffort) {
                ForEach(Preferences.reasoningEfforts, id: \.self) { Text($0).tag($0) }
            }.help(L("默认 low；支持的档位取决于模型服务"))
        }
    }
}
