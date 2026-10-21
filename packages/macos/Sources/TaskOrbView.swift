import SwiftUI

/// A stable corner badge; the detail card grows around it without taking focus.
struct MiniWindowView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var presentation: MiniWindowPresentation
    var body: some View {
        // AppKit changes the window origin immediately. Keep this alignment
        // unanimated so only the detail card's opacity changes on hover.
        ZStack(alignment: presentation.expandsUpward ? .bottomTrailing : .topTrailing) {
            if presentation.expanded {
                MiniWindowDetailsView(model: model, orbAtBottom: presentation.expandsUpward,
                                      visible: presentation.detailsVisible)
                    .accessibilityIdentifier("MiniWindowDetails")
            }
            if !presentation.expanded {
                HStack(spacing: 6) {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(model.taskProgressTitle).font(.system(size: 10, weight: .semibold))
                            .lineLimit(1).help(model.taskProgressTitle)
                        Text(model.currentIntent.isEmpty
                             ? (model.step > 0 ? L("第 %@ 步", String(model.step)) : "OpenGCUA")
                             : model.currentIntent)
                            .font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(1)
                            .help(model.currentIntent)
                        if !model.foregroundWarning.isEmpty {
                            Text(model.foregroundWarning).font(.system(size: 9)).foregroundStyle(.orange)
                                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                                .help(model.foregroundWarning).accessibilityIdentifier("ForegroundWarning")
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                        .overlay(WindowDragHandle())
                    Button { model.delegate?.closeCompact() } label: {
                        Image(systemName: "xmark").font(.system(size: 10)).frame(width: 24, height: 24)
                    }.buttonStyle(.plain).help(L("隐藏进度气泡")).accessibilityLabel(L("隐藏进度气泡"))
                    Color.clear.frame(width: FloatingPanel.iconSize)
                }.padding(.leading, 12)
                    .frame(width: FloatingPanel.progressWidth, height: model.progressBubbleHeight)
                    .background(.regularMaterial)
                    .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                    .accessibilityIdentifier("ProgressBubble")
            }
            TaskOrbView(model: model, animates: presentation.requested && !presentation.suppressed)
                .transaction { $0.animation = nil }
        }.frame(maxWidth: .infinity, maxHeight: .infinity,
                alignment: presentation.expandsUpward ? .bottomTrailing : .topTrailing)
            .preferredColorScheme(model.colorScheme).environment(\.locale, L10n.locale)
    }
}

private struct MiniWindowDetailsView: View {
    @ObservedObject var model: AppModel
    var orbAtBottom: Bool
    var visible: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @StateObject private var fade = MiniWindowFade()
    var body: some View {
        CompactView(model: model, orbAtBottom: orbAtBottom)
            .opacity(fade.appeared && visible ? 1 : 0)
            .animation(.easeOut(duration: reduceMotion ? 0 : 0.14), value: fade.appeared && visible)
            .allowsHitTesting(visible).accessibilityHidden(!visible)
            .onAppear { fade.appeared = true }
    }
}

private final class MiniWindowFade: ObservableObject {
    @Published var appeared = false
}

struct TaskOrbView: View {
    @ObservedObject var model: AppModel
    var animates = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    // 动画只是装饰；动作阶段和不确定状态只能来自真实事件。
    private var working: Bool { model.taskProgressIndeterminate }
    private var moving: Bool { animates && !reduceMotion && (working || model.voiceMode) }
    private var color: Color { model.voiceMode && !model.phase.active ? .accentColor : model.tint }
    private var symbol: String? {
        if model.voiceMode && (model.recording || model.speechPending) { return "mic.fill" }
        if model.phase == .running && !model.taskProgressIndeterminate { return model.taskProgressSymbol }
        switch model.phase {
        case .paused: return "pause.fill"
        case .waiting: return "questionmark"
        case .completed: return "checkmark"
        case .failed: return "exclamationmark"
        case .stopped: return "stop.fill"
        default: return nil
        }
    }
    var body: some View {
        Button { model.delegate?.showMain() } label: {
            TimelineView(.animation(minimumInterval: 1.0 / 24, paused: !moving)) { context in
                let time = moving ? context.date.timeIntervalSinceReferenceDate : 0
                let breathing = moving ? sin(time * 2.5) : 0
                ZStack {
                    RoundedRectangle(cornerRadius: 12, style: .continuous).fill(LinearGradient(
                        colors: [Color(red: 0.204, green: 0.243, blue: 0.275),
                                 Color(red: 0.067, green: 0.102, blue: 0.129)],
                        startPoint: .topLeading, endPoint: .bottomTrailing))
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(.white.opacity(0.18), lineWidth: 0.75)
                    OrbCursorMark().fill(.white)
                        .overlay(OrbCursorMark().stroke(.white, style: StrokeStyle(lineWidth: 0.6, lineJoin: .round)))
                        .frame(width: 20, height: 23).offset(x: -1, y: -1)
                    if let symbol {
                        Image(systemName: symbol).font(.system(size: 7, weight: .bold)).foregroundStyle(.white)
                            .frame(width: 12, height: 12).background(color, in: Circle())
                            .overlay(Circle().stroke(Color(red: 0.067, green: 0.102, blue: 0.129), lineWidth: 1))
                            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomTrailing)
                    } else {
                        Circle().fill(model.voiceMode ? color : Color(red: 0.576, green: 0.898, blue: 0.761))
                            .frame(width: 3.5, height: 3.5)
                            .opacity(moving ? 0.65 + breathing * 0.35 : 1).offset(x: 9, y: 10)
                    }
                }.frame(width: 36, height: 36)
                    .shadow(color: .black.opacity(0.18), radius: 2, y: 1)
                    .padding(4)
            }
        }.buttonStyle(.plain).frame(width: FloatingPanel.iconSize, height: FloatingPanel.iconSize)
            .overlay(OrbInteraction(openChat: { model.delegate?.showMain() }))
            .accessibilityLabel(L("任务状态图标"))
            .accessibilityValue(model.voiceMode ? L(model.speechStatus) : model.taskProgressTitle + ". " + model.taskProgressDetail)
            .accessibilityHint(L("悬停查看详情，点击打开对话，拖动移动"))
            .help(L("悬停查看详情，点击打开对话，拖动移动"))
    }
}

/// Deliberately simple silhouette: no raster resampling or tiny internal details.
private struct OrbCursorMark: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.minX + rect.width * 0.12, y: rect.minY + rect.height * 0.06))
        path.addLine(to: CGPoint(x: rect.minX + rect.width * 0.94, y: rect.minY + rect.height * 0.57))
        path.addLine(to: CGPoint(x: rect.minX + rect.width * 0.55, y: rect.minY + rect.height * 0.64))
        path.addLine(to: CGPoint(x: rect.minX + rect.width * 0.38, y: rect.minY + rect.height * 0.95))
        path.closeSubpath()
        return path
    }
}
