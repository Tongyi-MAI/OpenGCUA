import AVFoundation
import CoreAudio

struct AudioInputDevice: Identifiable, Equatable {
    let deviceID: AudioDeviceID
    let uid: String
    let name: String
    var id: String { uid }
}

enum AudioInputDevices {
    private static func address(_ selector: AudioObjectPropertySelector, scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    }
    static func string(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
        var property = address(selector), size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        var value: Unmanaged<CFString>?
        guard AudioObjectGetPropertyData(id, &property, 0, nil, &size, &value) == noErr else { return nil }
        return value?.takeRetainedValue() as String?
    }
    private static func flag(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector, scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> UInt32? {
        var property = address(selector, scope: scope), value: UInt32 = 0, size: UInt32 = 4
        guard AudioObjectGetPropertyData(id, &property, 0, nil, &size, &value) == noErr else { return nil }
        return value
    }
    static var defaultID: AudioDeviceID {
        var property = address(kAudioHardwarePropertyDefaultInputDevice), id: AudioDeviceID = 0
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &property, 0, nil, &size, &id) == noErr else { return 0 }
        return physicalInput(id, depth: 0)
    }
    private static func physicalInput(_ id: AudioDeviceID, depth: Int) -> AudioDeviceID {
        guard id != 0, depth < 4, string(id, kAudioDevicePropertyDeviceUID) != "BuiltInSpeakerDevice" else { return 0 }
        if flag(id, kAudioDevicePropertyIsHidden) != 1, flag(id, kAudioDevicePropertyDeviceCanBeDefaultDevice, scope: kAudioDevicePropertyScopeInput) == 1 { return id }
        var property = address(kAudioAggregateDevicePropertyActiveSubDeviceList), size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(id, &property, 0, nil, &size) == noErr, size > 0 else { return 0 }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(id, &property, 0, nil, &size, &ids) == noErr else { return 0 }
        return ids.lazy.map { physicalInput($0, depth: depth + 1) }.first { $0 != 0 } ?? 0
    }
    static func available() -> [AudioInputDevice] {
        var property = address(kAudioHardwarePropertyDevices), size: UInt32 = 0
        let system = AudioObjectID(kAudioObjectSystemObject)
        guard AudioObjectGetPropertyDataSize(system, &property, 0, nil, &size) == noErr, size > 0 else { return [] }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(system, &property, 0, nil, &size, &ids) == noErr else { return [] }
        return ids.compactMap { id in
            // VPIO creates private aggregate and echo-reference inputs; these are not user microphones.
            guard flag(id, kAudioDevicePropertyIsHidden) != 1,
                  flag(id, kAudioDevicePropertyDeviceCanBeDefaultDevice, scope: kAudioDevicePropertyScopeInput) != 0 else { return nil }
            var streams = address(kAudioDevicePropertyStreams, scope: kAudioDevicePropertyScopeInput), bytes: UInt32 = 0
            guard AudioObjectGetPropertyDataSize(id, &streams, 0, nil, &bytes) == noErr, bytes > 0,
                  let uid = string(id, kAudioDevicePropertyDeviceUID),
                  // VPIO temporarily exposes the built-in speakers as an echo-reference input.
                  uid != "BuiltInSpeakerDevice", let name = string(id, kAudioObjectPropertyName) else { return nil }
            return AudioInputDevice(deviceID: id, uid: uid, name: name)
        }.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }
    static func resolve(uid: String, devices: [AudioInputDevice], defaultID: AudioDeviceID) -> AudioInputDevice? {
        uid.isEmpty ? devices.first { $0.deviceID == defaultID } : devices.first { $0.uid == uid }
    }
    static func resolve(uid: String) throws -> AudioInputDevice {
        guard let device = resolve(uid: uid, devices: available(), defaultID: defaultID) else {
            throw AppError.message(uid.isEmpty ? "没有可用的系统默认麦克风。" : "所选麦克风已断开，请切换输入音源。")
        }
        return device
    }
    /// Changes this engine's capture device only; never writes the system default device.
    static func select(_ device: AudioInputDevice, engine: AVAudioEngine) throws {
        let unit = engine.inputNode.auAudioUnit
        try unit.setDeviceID(device.deviceID)
        guard unit.deviceID == device.deviceID else {
            throw AppError.message("无法启用所选麦克风，请切换音源后重试。")
        }
        // VPIO may replace the hardware ID with a private aggregate. Configure/verify the
        // physical source before enabling it; never overwrite its internal aggregate afterwards.
        try engine.inputNode.setVoiceProcessingEnabled(true)
    }
}

enum VoiceAudioFormat {
    /// VPIO's hardware format may include discrete microphone/echo-reference channels.
    /// Negotiate a mono client stream on BOTH I/O nodes instead of downmixing that layout:
    /// AVAudioConverter can map a discrete multichannel input to [-1] (silent output).
    static func capture(for hardware: AVAudioFormat) throws -> AVAudioFormat {
        guard hardware.sampleRate.isFinite, hardware.sampleRate > 0, hardware.channelCount > 0,
              let mono = AVAudioFormat(standardFormatWithSampleRate: hardware.sampleRate, channels: 1)
        else { throw AppError.message("没有可用的麦克风") }
        return mono
    }
}

/// dBFS scaling makes quiet speech visible without representing silence as activity.
struct VoiceInputMeter {
    let db: Double
    let level: Double
    init(pcm: Data) {
        let count = pcm.count / 2
        guard count > 0 else { db = -96; level = 0; return }
        let sum = pcm.withUnsafeBytes { raw -> Double in
            var sum = 0.0
            for i in 0..<count {
                let sample = Double(Int16(littleEndian: raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self))) / 32768
                sum += sample * sample
            }
            return sum
        }
        db = max(-96, 20 * log10(max(1e-8, sqrt(sum / Double(count)))))
        level = max(0, min(1, (db + 60) / 54))
    }
}
