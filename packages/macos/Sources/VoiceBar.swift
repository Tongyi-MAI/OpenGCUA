import SwiftUI

/// A separate input mode, with no editor or send button. Audio never fills a task draft.
struct VoiceBar: View {
    @ObservedObject var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        VStack(spacing: 10) {
            HStack(spacing: 10) {
                Brand(size: 24)
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.speechStatus.isEmpty ? "连接语音" : model.speechStatus).font(.system(size: 12, weight: .medium))
                }
                Spacer(minLength: 4)
                if !model.recording && !model.speechPending {
                    SymbolButton(symbol: "arrow.clockwise", title: "重新连接语音") { model.retryVoice() }
                }
                SymbolButton(symbol: "keyboard", title: "切换为文字输入") { model.stopSpeech() }
                SymbolButton(symbol: "xmark", title: "关闭小窗") { model.delegate?.closeCompact() }
                    .help("关闭小窗并停止收音，电脑任务继续执行")
            }
            HStack(spacing: 8) {
                Menu {
                    Button("系统默认 · " + model.systemInputName) { model.selectAudioInput("") }
                    Divider()
                    ForEach(model.audioInputs) { device in
                        Button { model.selectAudioInput(device.uid) } label: {
                            if model.preferences.audioInputUID == device.uid { Label(device.name, systemImage: "checkmark") }
                            else { Text(device.name) }
                        }
                    }
                    Divider()
                    Button("刷新音源") { model.refreshAudioInputs() }
                } label: {
                    Label(model.selectedInputName, systemImage: "mic.fill").lineLimit(1).font(.system(size: 10))
                }.menuStyle(.borderlessButton).help("切换音源会重新连接语音，电脑任务继续执行")
                    .accessibilityLabel("输入音源").help(model.recording ? "当前采集：" + model.activeInputName + "。切换会重新连接语音。" : "选择默认输入音源")
                Spacer(minLength: 0)
                Text(model.inputSignalText).font(.system(size: 10)).foregroundStyle(model.inputDB > -50 && model.recording ? .green : .secondary)
            }
            TimelineView(.animation(minimumInterval: reduceMotion ? 0.2 : 1.0 / 30)) { timeline in
                let time = reduceMotion ? 0 : timeline.date.timeIntervalSinceReferenceDate
                let level = max(model.voiceOutputLevel, model.voiceLevel)
                HStack(alignment: .center, spacing: 3) {
                    ForEach(0..<42, id: \.self) { index in
                        let center = 1 - abs(Double(index) - 20.5) / 23
                        let wave = 0.3 + 0.7 * abs(sin(time * 4.8 + Double(index) * 0.57))
                        Capsule().fill(Color.accentColor.opacity(model.voiceSpeaking ? 1 : 0.6))
                            .frame(width: 4, height: 3 + 27 * center * min(1, level * 2) * wave)
                    }
                }.frame(maxWidth: .infinity).frame(height: 31)
            }.accessibilityLabel(model.voiceSpeaking ? "助手正在说话，可以打断" : "语音输入音量")
            HStack(spacing: 6) {
                Text("输入").font(.system(size: 9)).foregroundStyle(.secondary)
                GeometryReader { geometry in
                    Capsule().fill(.primary.opacity(0.08))
                    Capsule().fill(model.inputDB > -6 ? Color.orange : Color.green)
                        .frame(width: geometry.size.width * model.voiceLevel)
                }.frame(height: 3).accessibilityLabel("麦克风输入音量")
                    .accessibilityValue(model.hasInputFrames ? "\(Int(model.inputDB)) dB" : "尚无音频数据")
                Text(model.hasInputFrames ? "\(Int(model.inputDB)) dB" : "— dB")
                    .font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary).frame(width: 39, alignment: .trailing)
            }
            Text(model.error.isEmpty ? (model.voiceCaption.isEmpty ? "随时说话，也可以说「停止任务」" : model.voiceCaption) : model.error)
                .font(.system(size: 11)).foregroundStyle(model.error.isEmpty ? Color.secondary : .orange)
                .lineLimit(model.error.isEmpty ? 1 : 3).frame(maxWidth: .infinity, alignment: .center)
                .help(model.error.isEmpty ? model.voiceCaption : model.error)
            if model.showsVoiceTaskProgress {
                Divider().overlay(.primary.opacity(0.04))
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 7) {
                        if [.starting, .running, .pausing, .stopping].contains(model.phase) {
                            ProgressView().controlSize(.mini)
                        } else {
                            Image(systemName: model.phase.symbol).foregroundStyle(model.tint)
                        }
                        Text(model.taskProgressTitle).fontWeight(.medium)
                        Spacer(minLength: 4)
                        if model.step > 0 {
                            Text("第 \(model.step) 步").monospacedDigit().foregroundStyle(.secondary)
                                .help("电脑任务的规划轮次，一步可能包含多个操作")
                        }
                    }.font(.system(size: 11))
                    if model.showsCompactResult { CompactResultView(text: model.compactResultText).id(model.taskID) }
                    else {
                        Text(model.taskProgressDetail).font(.system(size: 11)).foregroundStyle(.secondary)
                            .lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
                            .help(model.taskProgressDetail)
                    }
                }.frame(height: model.showsCompactResult ? 140 : 60, alignment: .top)
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("电脑任务进度")
                    .accessibilityValue(model.taskProgressTitle + "，第 \(model.step) 步，" + model.taskProgressDetail)
            }
        }.padding(.horizontal, 16).padding(.vertical, 13).frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(MaterialView(material: .popover)).clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(Color(nsColor: .separatorColor).opacity(0.5)))
            .preferredColorScheme(model.colorScheme)
            .onChange(of: model.error) { _, _ in model.delegate?.refreshCompact() }
    }
}
