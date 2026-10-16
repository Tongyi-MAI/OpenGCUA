import AVFoundation
import Speech

/// Opt-in, on-device dictation. Never starts on launch, focus, or task execution.
@MainActor final class SpeechInput {
    var onText: ((String) -> Void)?
    var onState: ((Bool) -> Void)?
    var onError: ((String) -> Void)?
    private var engine: AVAudioEngine?
    private var task: SFSpeechRecognitionTask?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var generation = UUID()

    func start() async {
        stop()
        let token = generation
        let status = await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { continuation.resume(returning: $0) }
        }
        guard token == generation else { return }
        guard status == .authorized else { onError?("请在系统设置中允许 OpenGCUA 使用语音识别，也可以直接输入任务。"); return }
        let microphone = await AVCaptureDevice.requestAccess(for: .audio)
        guard token == generation else { return }
        guard microphone else { onError?("麦克风未授权，可以直接输入任务。"); return }
        guard let recognizer = SFSpeechRecognizer(locale: Locale.current), recognizer.isAvailable,
              recognizer.supportsOnDeviceRecognition else {
            onError?("当前语言的本机语音识别不可用，请使用键盘输入。"); return
        }
        let engine = AVAudioEngine(), request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = true
        let node = engine.inputNode, format = engine.inputNode.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { onError?("没有可用的麦克风。"); return }
        node.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in request.append(buffer) }
        self.engine = engine; self.request = request
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            Task { @MainActor in
                guard let self, self.generation == token else { return }
                if let result { self.onText?(result.bestTranscription.formattedString) }
                if result?.isFinal == true { self.stop() }
                else if let error { self.stop(); self.onError?(error.localizedDescription) }
            }
        }
        do { engine.prepare(); try engine.start(); onState?(true) }
        catch { stop(); onError?(error.localizedDescription) }
    }
    func stop() {
        generation = UUID()
        if let engine { engine.stop(); engine.inputNode.removeTap(onBus: 0) }
        engine = nil; request?.endAudio(); request = nil; task?.cancel(); task = nil
        onState?(false)
    }
}
