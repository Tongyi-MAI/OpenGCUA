import SwiftUI

/// A separate input mode, with no editor or send button. Audio never fills a task draft.
struct VoiceBar: View {
    @ObservedObject var model: AppModel
    var orbAtBottom = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        VStack(spacing: 6) {
            HStack(spacing: 10) {
                HStack(spacing: 7) {
                    Text(model.speechStatus.isEmpty ? L("连接语音") : L(model.speechStatus)).font(.system(size: 12, weight: .medium)).lineLimit(1)
                    Spacer(minLength: 0)
                }.frame(maxWidth: .infinity).overlay(WindowDragHandle())
                if !model.recording && !model.speechPending {
                    SymbolButton(symbol: "arrow.clockwise", title: L("重新连接语音")) { model.retryVoice() }
                }
                SymbolButton(symbol: "arrow.up.left.and.arrow.down.right", title: L("展开任务")) { model.delegate?.showMain() }
                SymbolButton(symbol: "keyboard", title: L("切换为文字输入")) { model.stopSpeech() }
                SymbolButton(symbol: "xmark", title: L("关闭小窗")) { model.delegate?.closeCompact() }
                    .help(L("关闭小窗并停止收音，电脑任务继续执行"))
                if !orbAtBottom { Color.clear.frame(width: 42, height: 32).allowsHitTesting(false) }
            }
            if !model.foregroundWarning.isEmpty {
                Text(model.foregroundWarning).font(.system(size: 10)).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true).help(model.foregroundWarning)
            }
            HStack(spacing: 8) {
                Menu {
                    Button(L("系统默认 · ") + model.systemInputName) { model.selectAudioInput("") }
                    Divider()
                    ForEach(model.audioInputs) { device in
                        Button { model.selectAudioInput(device.uid) } label: {
                            if model.preferences.audioInputUID == device.uid { Label(device.name, systemImage: "checkmark") }
                            else { Text(device.name) }
                        }
                    }
                    Divider()
                    Button(L("刷新音源")) { model.refreshAudioInputs() }
                } label: {
                    Label(model.selectedInputName, systemImage: "mic.fill").lineLimit(1).font(.system(size: 10))
                }.menuStyle(.borderlessButton).help(L("切换音源会重新连接语音，电脑任务继续执行"))
                    .accessibilityLabel(L("输入音源")).help(model.recording ? L("当前采集：") + model.activeInputName + L("。切换会重新连接语音。") : L("选择默认输入音源"))
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
            }.accessibilityLabel(model.voiceSpeaking ? L("助手正在说话，可以打断") : L("语音输入音量"))
            HStack(spacing: 6) {
                Text(L("输入")).font(.system(size: 9)).foregroundStyle(.secondary)
                GeometryReader { geometry in
                    Capsule().fill(.primary.opacity(0.08))
                    Capsule().fill(model.inputDB > -6 ? Color.orange : Color.green)
                        .frame(width: geometry.size.width * model.voiceLevel)
                }.frame(height: 3).accessibilityLabel(L("麦克风输入音量"))
                    .accessibilityValue(model.hasInputFrames ? "\(Int(model.inputDB)) dB" : L("尚无音频数据"))
                Text(model.hasInputFrames ? "\(Int(model.inputDB)) dB" : "— dB")
                    .font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary).frame(width: 39, alignment: .trailing)
            }
            Text(model.error.isEmpty ? (model.voiceCaption.isEmpty ? L("随时说话，也可以说「停止任务」") : model.voiceCaption) : L(model.error))
                .font(.system(size: 11)).foregroundStyle(model.error.isEmpty ? Color.secondary : .orange)
                .lineLimit(model.error.isEmpty ? 1 : 3).frame(maxWidth: .infinity, alignment: .center)
                .help(model.error.isEmpty ? model.voiceCaption : L(model.error))
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
                            Text(L("第 %@ 步", String(describing: model.step))).monospacedDigit().foregroundStyle(.secondary)
                                .help(L("电脑任务的规划轮次，一步可能包含多个操作"))
                        }
                    }.font(.system(size: 11))
                    if model.showsCompactResult { CompactResultView(text: model.compactResultText, height: 84).id(model.taskID) }
                    else {
                        Text(model.taskProgressDetail).font(.system(size: 11)).foregroundStyle(.secondary)
                            .lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
                            .help(model.taskProgressDetail)
                    }
                }.frame(height: model.showsCompactResult ? 104 : 44, alignment: .top)
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(L("电脑任务进度"))
                    .accessibilityValue(model.taskProgressTitle + L("，第 %@ 步，", String(describing: model.step)) + model.taskProgressDetail)
            }
            if !model.controls.result.isEmpty {
                Text(model.controls.result).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1).help(model.controls.result)
            }
            Spacer(minLength: 0)
            if !model.controls.approvals.isEmpty {
                Button(L("查看前台审批（默认拒绝）")) { model.delegate?.showMain() }.font(.caption)
            }
            HStack {
                TaskControls(model: model, compact: true)
                Spacer(minLength: 0)
                if orbAtBottom { Color.clear.frame(width: 42).allowsHitTesting(false) }
            }.frame(height: orbAtBottom ? 32 : 24)
        }.padding(10).frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(MaterialView(material: .popover)).clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(Color(nsColor: .separatorColor).opacity(0.5)))
            .preferredColorScheme(model.colorScheme).environment(\.locale, L10n.locale)
            .onChange(of: model.error) { _, _ in model.delegate?.refreshCompact() }
    }
}
