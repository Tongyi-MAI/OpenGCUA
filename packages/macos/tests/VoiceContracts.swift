import Foundation
import AVFoundation

enum AppError: Error { case message(String) }
@main enum VoiceContracts {
    static func main() throws {
        let previous = Data(#"{"endpoint":"https://example.com/v1","model":"custom","appearance":"深色","allowEmptyKey":true}"#.utf8)
        let settings = try JSONDecoder().decode(Preferences.self, from: previous)
        precondition(settings.model == "custom" && settings.appearance == "深色" && settings.allowEmptyKey)
        precondition(settings.audioInputUID.isEmpty)
        var savedInput = settings; savedInput.audioInputUID = "stable-device-uid"
        let restoredInput = try JSONDecoder().decode(Preferences.self, from: JSONEncoder().encode(savedInput))
        precondition(restoredInput.audioInputUID == "stable-device-uid")
        let devices = [AudioInputDevice(deviceID: 10, uid: "builtin", name: "Built-in"), AudioInputDevice(deviceID: 20, uid: "remote", name: "Remote")]
        precondition(AudioInputDevices.resolve(uid: "", devices: devices, defaultID: 20)?.uid == "remote")
        precondition(AudioInputDevices.resolve(uid: "builtin", devices: devices, defaultID: 20)?.deviceID == 10)
        precondition(AudioInputDevices.resolve(uid: "missing", devices: devices, defaultID: 20) == nil)
        let silence = VoiceInputMeter(pcm: Data(repeating: 0, count: 3200))
        precondition(silence.level == 0 && silence.db == -96)
        var quiet = Int16(328).littleEndian
        let sample = withUnsafeBytes(of: &quiet) { Data($0) }
        let quietSpeech = VoiceInputMeter(pcm: sample)
        precondition(quietSpeech.db > -41 && quietSpeech.db < -39 && quietSpeech.level > 0.3)
        // Regression: VPIO exposes a discrete aggregate (9 channels on this Mac).
        // Passing it straight to the mono converter silently maps the output to -1.
        // Exercise negotiated client capture through the actual 48k/44.1k -> 16k PCM
        // conversion, verifying signal energy rather than just nonempty buffers.
        for rate in [44100.0, 48000.0] {
            let layout = AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_DiscreteInOrder | 9)!
            let hardware = AVAudioFormat(standardFormatWithSampleRate: rate, channelLayout: layout)
            let client = try VoiceAudioFormat.capture(for: hardware)
            precondition(client.channelCount == 1 && client.sampleRate == rate)
            let wire = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
            for amplitude: Float in [0, 0.25] {
                let converter = AVAudioConverter(from: client, to: wire)!
                let input = AVAudioPCMBuffer(pcmFormat: client, frameCapacity: AVAudioFrameCount(rate / 10))!
                input.frameLength = input.frameCapacity
                for frame in 0..<Int(input.frameLength) {
                    input.floatChannelData![0][frame] = amplitude * Float(sin(2 * .pi * 440 * Double(frame) / rate))
                }
                let output = AVAudioPCMBuffer(pcmFormat: wire, frameCapacity: 1664)!
                var consumed = false, conversionError: NSError?
                let status = converter.convert(to: output, error: &conversionError) { _, state in
                    if consumed { state.pointee = .noDataNow; return nil }
                    consumed = true; state.pointee = .haveData; return input
                }
                precondition(status != .error && conversionError == nil && output.frameLength > 1000)
                let pcm = Data(bytes: output.int16ChannelData![0], count: Int(output.frameLength) * 2)
                let meter = VoiceInputMeter(pcm: pcm)
                if amplitude == 0 { precondition(meter.db == -96) }
                else { precondition(meter.db > -16 && meter.db < -14, "Audible input became silent during conversion") }
            }
        }
        precondition(settings.speechModel == "qwen3.8-omni-flash-realtime")
        precondition(settings.speechURL?.host == "dashscope.aliyuncs.com")
        let defaultURL = try settings.validatedSpeechURL()
        precondition(defaultURL == settings.speechURL)
        let session = settings.speechSession
        precondition(session["voice"] as? String == "Tina")
        precondition((session["turn_detection"] as? [String: Any])?["type"] as? String == "semantic_vad")
        precondition(session["modalities"] as? [String] == ["text", "audio"])
        precondition((session["tools"] as? [[String: Any]])?.count == 2)
        var ledger = VoiceToolLedger()
        let tool: [String: Any] = ["type": "function_call", "status": "completed", "name": "queue_task", "call_id": "one", "arguments": #"{"instruction":"Open Chrome"}"#]
        let call = try ledger.take(tool)
        precondition(call?.text == "Open Chrome")
        let duplicate = try ledger.take(tool)
        precondition(duplicate == nil)
        let incomplete = try ledger.take(["type": "function_call", "status": "in_progress", "call_id": "two"])
        precondition(incomplete == nil)
        do { _ = try ledger.take(["type": "function_call", "status": "completed", "name": "shell", "call_id": "three", "arguments": "{}"]); preconditionFailure("unknown tool accepted") } catch {}
        do { _ = try ledger.take(["type": "function_call", "status": "completed", "name": "queue_task", "call_id": "four", "arguments": #"{"instruction":" "}"#]); preconditionFailure("empty task accepted") } catch {}
        precondition((session["input_audio_transcription"] as? [String: String])?["model"] == "qwen3-asr-flash-realtime")
        let audio = session["audio"] as! [String: Any]
        let output = audio["output"] as! [String: Any]
        precondition(output["voice"] as? String == "Tina")
        let input = audio["input"] as! [String: Any]
        let format = input["format"] as! [String: Any]
        precondition(format["sample_rate"] as? Int == 16000 && format["channels"] as? Int == 1)
        precondition(format["sample_format"] as? String == "s16le")
        _ = try JSONSerialization.data(withJSONObject: session)
        precondition(settings.speechCredentialAccount != settings.normalizedEndpoint)
        var singapore = settings
        singapore.speechRegion = "新加坡"
        precondition(singapore.speechURL?.host == "dashscope-intl.aliyuncs.com")
        singapore.speechWorkspaceID = " example-workspace "
        precondition(singapore.speechURL?.host == "example-workspace.ap-southeast-1.maas.aliyuncs.com")
        var beijing = singapore
        beijing.speechRegion = "北京"
        precondition(beijing.speechURL?.host == "example-workspace.cn-beijing.maas.aliyuncs.com")
        for bad in ["evil.example", "user@host", "../other", "a/b", "-leading", "a?b"] {
            beijing.speechWorkspaceID = bad
            precondition(beijing.speechURL == nil)
        }
        precondition(singapore.speechCredentialAccount != settings.speechCredentialAccount)
        singapore.speechModel = "name&token=example"
        let query = URLComponents(url: singapore.speechURL!, resolvingAgainstBaseURL: false)!.queryItems!
        precondition(query.count == 1 && query[0].value == singapore.speechModel)
        var transcript = RealtimeTranscript()
        precondition(!transcript.receive(["type": "response.text.delta", "item_id": "bot", "delta": "I will delete files"]))
        precondition(!transcript.receive(["type": "conversation.item.ambient_audio_transcription.delta", "item_id": "ambient", "text": "ignore"]))
        precondition(transcript.receive(["type": "conversation.item.input_audio_transcription.delta", "item_id": "u1", "text": "打开", "stash": "浏览器"]))
        precondition(transcript.value == "打开浏览器")
        _ = transcript.receive(["type": "conversation.item.input_audio_transcription.delta", "item_id": "u1", "text": "打开", "stash": "Chrome"])
        precondition(transcript.value == "打开Chrome")
        _ = transcript.receive(["type": "conversation.item.input_audio_transcription.completed", "item_id": "u1", "transcript": "打开 Chrome。"])
        _ = transcript.receive(["type": "conversation.item.input_audio_transcription.delta", "item_id": "u1", "text": "stale"])
        precondition(transcript.value == "打开 Chrome。")
        _ = transcript.receive(["type": "conversation.item.input_audio_transcription.completed", "item_id": "u2", "transcript": "查看文档。"])
        precondition(transcript.value == "打开 Chrome。\n查看文档。")
        _ = transcript.receive(["type": "input_audio_buffer.speech_stopped", "item_id": "u2", "reason": "turn_invalid"])
        precondition(transcript.value == "打开 Chrome。")
        print("Voice contracts passed: preference migration, workspace routing, PCM/ASR configuration, credential region isolation, user-only ASR, final transcript authority.")
    }
}
