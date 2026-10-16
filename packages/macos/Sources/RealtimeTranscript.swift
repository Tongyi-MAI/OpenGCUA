import Foundation

/// Only user ASR enters the draft. Model answers and ambient speech are ignored.
struct RealtimeTranscript {
    private var order: [String] = []
    private var text: [String: String] = [:]
    private var finalized: Set<String> = []
    var value: String { order.compactMap { text[$0] }.filter { !$0.isEmpty }.joined(separator: "\n") }
    mutating func receive(_ event: [String: Any]) -> Bool {
        guard let type = event["type"] as? String, let id = event["item_id"] as? String else { return false }
        if type == "input_audio_buffer.speech_stopped", event["reason"] as? String == "turn_invalid" {
            text[id] = nil; return true
        }
        guard type == "conversation.item.input_audio_transcription.delta" || type == "conversation.item.input_audio_transcription.completed" else { return false }
        if !order.contains(id) { order.append(id) }
        if type.hasSuffix(".completed") {
            text[id] = event["transcript"] as? String ?? text[id] ?? ""
            finalized.insert(id)
        } else if !finalized.contains(id) {
            // Qwen sends the stable text plus a replaceable tentative suffix.
            text[id] = (event["text"] as? String ?? "") + (event["stash"] as? String ?? "")
        }
        return true
    }
}
