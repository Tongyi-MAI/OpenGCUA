import Foundation
import CoreFoundation
import SwiftUI

struct SkillEntry: Identifiable, Equatable {
    var name: String
    var description: String
    var source: String
    var id: String { name }
}
struct QueuedInput: Identifiable, Equatable {
    var id: String
    var text: String
    var kind: String
}
struct SessionApproval: Identifiable, Equatable {
    var id: String
    var appID: String
    var windowID: String
    var text: String
    var scope: String
}
struct ExecutionCode: Identifiable, Equatable {
    var index: Int
    var step: Int
    var text: String
    var complete = false
    var id: Int { index }
}
struct PendingControl {
    var name: String
    var taskID: String
    var turn: Int
    var approvalID: String?
    var started = Date()
    var memoryID: String?
    // memory_list 请求的页起点；变更收到 control_result 后记录其 generation，目录不得早于它。
    var offset = 0
    var generation: Int?
    // 压缩、Skill 与长期记忆由 host 在轮次之间处理，回执可能晚于本轮；只按 command_id 与 task 匹配。
    var sessionScoped: Bool { ["compaction", "compaction_cancel", "skill_select"].contains(name) || MemoryControlsState.commands.contains(name) }
}
// 启动前的用户选择：unspecified 不下发，交给 host 恢复历史或使用默认值。
enum SkillOverride: Equatable {
    case unspecified, noSkill, named(String)
    var name: String? { if case let .named(name) = self { return name }; return nil }
}

// flat envelope 只作 UI 路由和诊断；不构造业务验证器或执行请求。
struct NativeEventEnvelope {
    let fields: [String: Any]
    var structured: Bool {
        fields["schema_version"] != nil || fields["event_id"] != nil || fields["turn_id"] != nil || fields["origin"] != nil || fields["event"] == nil
    }
    var kind: String {
        // legacy message 的 kind 是消息类型，不是事件名。
        if structured { return fields["kind"] as? String ?? fields["event"] as? String ?? "" }
        return fields["event"] as? String ?? ""
    }
    // 无 schema_version 的 legacy 帧保持既有兼容；带版本即按 v1 完整校验，与 Python validate_event 一致。
    var supported: Bool {
        guard fields["schema_version"] != nil else { return true }
        func text(_ key: String) -> Bool { (fields[key] as? String).map { !$0.isEmpty } ?? false }
        func optionalText(_ key: String) -> Bool { fields[key] is NSNull || fields[key] is String }
        return Self.integer(fields["schema_version"]) == 1
            && (Self.integer(fields["sequence"]) ?? 0) >= 1
            && (Self.integer(fields["policy_revision"]) ?? -1) >= 0
            && ["event_id", "session_id", "kind", "origin", "timestamp"].allSatisfy(text)
            && ["task_id", "turn_id", "step_id"].allSatisfy(optionalText)
            && fields["text"] is String
            && (fields["step"] is NSNull || (Self.integer(fields["step"]) ?? -1) >= 0)
            && (fields["stream_id"] == nil || text("stream_id"))
    }
    // JSON 整数：拒绝 bool 与浮点（含 1.0），超出 Int 范围视为无效。
    static func integer(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              !CFNumberIsFloatType(number) else { return nil }
        return Int(exactly: number)
    }
    static func transportID(_ event: [String: Any]) -> String? {
        event["native_task_id"] as? String ?? event["task_id"] as? String
    }
    static func diagnostic(_ value: Any?) -> String {
        guard let value, !(value is NSNull) else { return "" }
        if let text = value as? String { return text }
        if JSONSerialization.isValidJSONObject(value),
           let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
           let text = String(data: data, encoding: .utf8) { return text }
        return String(describing: value)
    }
    var metadata: [String: String] {
        let keys = ["schema_version", "event_id", "sequence", "session_id", "task_id", "native_task_id", "turn_id", "step_id", "timestamp", "kind", "origin", "policy_revision", "action_id", "sdk_session_id", "resource_id", "observation_id", "journal_sequence", "record_sequence", "stream_id", "cell_request_id", "attempt_id"]
        return keys.reduce(into: [:]) { result, key in
            if let value = fields[key], !(value is NSNull) { result[key] = Self.diagnostic(value) }
        }
    }
}

// action_status.rule_feedback 的只读事实，白名单与 action_feedback.py 一致。
// summary、text 与原生自由文字一律不保存；condition/verification 只接受固定值，任何事实都不升级核验。
struct ActionRuleFacts: Equatable {
    static let evaluations = ["not_dispatched", "not_applied", "outcome_unknown", "execution_failed",
                              "suspected_noop", "partial", "receipt_unverified", "observation_unavailable"]
    static let evidenceKinds = ["exact_window_activation", "native_observation", "device_receipt"]
    static let listLimit = 8
    // 执行端可能调整 AX 诊断措辞；仅精确匹配的同义原文归一为同一条，仍由 Swift 固定标签展示。
    static let diagnosticAliases = ["原生报告 AXValue 不可设置：只说明 ax.set_text 写值路径不可用，不代表控件不能输入，也不证明是静态文本":
                                         "AXValue 不可直接写入；可读文本不等于输入控件"]
    var evaluation: String
    var operation: String?
    var reportedStage: String?
    var execution: String?
    var effect: String?
    var errorCode: String?
    var nativeErrorCode: String?
    var mechanism: String?
    var observationID: String?
    var durationMS: Double?
    var diagnostics: [String] = []
    var evidenceKinds: [String] = []
    var incompleteComponents: Int?
    var truncated = false
    var errorCodes: [String] = []
    // 与 Python _TOKEN 相同：[A-Za-z0-9_.:/-]{1,160}。
    static func token(_ value: Any?) -> String? {
        guard let text = value as? String, (1...160).contains(text.unicodeScalars.count),
              text.unicodeScalars.allSatisfy({ $0.isASCII && (CharacterSet.alphanumerics.contains($0) || "_.:/-".unicodeScalars.contains($0)) })
        else { return nil }
        return text
    }
    // 版本、evaluation、固定 condition/verification 或动作身份不符时整份丢弃；其余字段逐项清洗。
    init?(_ value: Any?, actionID: String) {
        guard let row = value as? [String: Any], NativeEventEnvelope.integer(row["rule_version"]) == 1,
              let evaluation = row["evaluation"] as? String, Self.evaluations.contains(evaluation),
              row["verification"] as? String == "unverified", row["condition"] as? String == "not_checked" else { return nil }
        if let raw = row["action_id"], Self.token(raw) != actionID { return nil }
        self.evaluation = evaluation
        operation = Self.token(row["operation"]); reportedStage = Self.token(row["reported_stage"])
        execution = Self.token(row["execution"]); effect = Self.token(row["effect"])
        errorCode = Self.token(row["error_code"]); nativeErrorCode = Self.token(row["native_error_code"])
        mechanism = Self.token(row["mechanism"]); observationID = Self.token(row["observation_id"])
        if let number = row["duration_ms"] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
           number.doubleValue.isFinite, (0...86_400_000).contains(number.doubleValue) { durationMS = number.doubleValue }
        func bounded(_ key: String, _ accept: (Any) -> String?) -> [String] {
            var result: [String] = []
            for item in (row[key] as? [Any] ?? []).prefix(Self.listLimit) {
                if let value = accept(item), !result.contains(value) { result.append(value) }
            }
            return result
        }
        diagnostics = bounded("diagnostics") { item in
            (item as? String).map { Self.diagnosticAliases[$0] ?? $0 }.flatMap { Self.diagnosticLabel($0) == nil ? nil : $0 }
        }
        evidenceKinds = bounded("evidence_kinds") { item in (item as? String).flatMap { Self.evidenceKinds.contains($0) ? $0 : nil } }
        errorCodes = bounded("error_codes") { Self.token($0) }
        if let count = NativeEventEnvelope.integer(row["incomplete_components"]), count >= 0 {
            incompleteComponents = count; truncated = MemoryJSON.boolean(row["truncated"]) == true
        }
    }
    // Python _PRIORITY：数值越小越强；较弱事实不覆盖结果未知或拒绝等较强事实。
    var priority: Int { evaluation == "outcome_unknown" ? 0 : evaluation == "receipt_unverified" ? 2 : 1 }
    // 技术回执仅表示未核验，不是警告；其余事实都需要核实且不能显示为完成。
    var isWarning: Bool { evaluation != "receipt_unverified" }
    var label: String {
        switch evaluation {
        case "not_dispatched": return L("动作未派发：在派发前被拒绝，未执行")
        case "not_applied": return L("回执报告未应用；不据此推断失败阶段")
        case "outcome_unknown": return L("派发结果未知，可能已有副作用；请先核实，勿直接重试")
        case "execution_failed": return L("调用报告失败；业务结果未确定")
        case "suspected_noop": return L("疑似无效操作；原因未确定，不代表完全未执行")
        case "partial": return L("仅部分生效；业务结果未核验")
        case "observation_unavailable": return L("观察不完整或不可用；不能据此证明目标不存在")
        default: return L("已收到技术回执；预期变化未核验")
        }
    }
    // 气泡只放一行事实；技术回执沿用阶段标签，避免把普通点击显示成失败。
    var shortLabel: String? {
        switch evaluation {
        case "not_dispatched": return L("动作未派发")
        case "not_applied": return L("动作未应用")
        case "outcome_unknown": return L("动作结果未知，请核实")
        case "execution_failed": return L("动作报告失败")
        case "suspected_noop": return L("疑似无效操作")
        case "partial": return L("动作仅部分生效")
        case "observation_unavailable": return L("观察不完整")
        default: return nil
        }
    }
    static func diagnosticLabel(_ raw: String) -> String? {
        switch raw {
        case "AXValue 不可直接写入；可读文本不等于输入控件": return L("AXValue 不可直接写入：这不等于该元素是静态控件，可读文本也不等于输入控件")
        case "同进程多个窗口导致键盘目标歧义": return L("同进程多个窗口导致键盘目标歧义")
        case "元素不在绑定的目标窗口内": return L("元素不在绑定的目标窗口内")
        case "目标应用或窗口已不在前台": return L("目标应用或窗口已不在前台")
        case "原生 AX 窗口映射未解析": return L("原生 AX 窗口映射未解析")
        default: return nil
        }
    }
}

struct NativeActionStatus: Identifiable, Equatable {
    var id: String
    var stage: String
    var execution: String
    var effect: String
    var verification: String
    var metadata: [String: String]
    var facts: ActionRuleFacts?
    // 首次出现时的轮次；旧轮动作的迟到增量只更新诊断，不重新成为当前动作。
    var turn = -1
    init?(_ event: [String: Any], recovery: Bool = false) {
        guard let id = event["action_id"] as? String, !id.isEmpty else { return nil }
        self.id = id
        stage = event[recovery ? "status" : "stage"] as? String ?? ""
        execution = NativeEventEnvelope.diagnostic(event["execution"])
        effect = NativeEventEnvelope.diagnostic(event["effect"])
        let verification = NativeEventEnvelope.diagnostic(event["verification"])
        self.verification = verification.isEmpty ? "unverified" : verification
        metadata = NativeEventEnvelope(fields: event).metadata
        facts = ActionRuleFacts(event["rule_feedback"], actionID: id)
    }
    var progressTitle: String {
        guard !progressing, let short = facts?.shortLabel else { return label }
        return short
    }
    var unresolved: Bool { stage == "outcome_unknown" || facts?.evaluation == "outcome_unknown" }
    var label: String {
        switch stage {
        case "intent_recorded": return L("工具意图已记录，尚未派发")
        case "waiting_resource": return L("工具排队中")
        case "dispatching": return L("工具派发中")
        case "running": return L("工具执行中")
        case "receipt_received", "receipt_unverified": return L("已收到技术回执，尚未核验")
        case "outcome_unknown": return L("动作结果未知，请核实")
        case "rejected": return L("动作被拒绝")
        case "cancelled_before_dispatch": return L("动作在派发前已取消")
        case "verification_failed": return L("动作核验失败")
        case "verified": return L("动作已核验（不代表任务完成）")
        case "not_dispatched": return L("动作尚未派发")
        default: return stage
        }
    }
    var progressing: Bool { ["waiting_resource", "dispatching", "running"].contains(stage) }
    var symbol: String {
        if !progressing, facts?.isWarning == true { return "exclamationmark.triangle" }
        switch stage {
        case "waiting_resource", "intent_recorded", "not_dispatched": return "clock"
        case "dispatching", "running": return "arrow.triangle.2.circlepath"
        case "outcome_unknown", "verification_failed": return "exclamationmark.circle"
        case "rejected", "cancelled_before_dispatch": return "xmark.circle"
        case "verified": return "checkmark.shield"
        default: return "doc.text.magnifyingglass"
        }
    }
}

// context_status 只是执行端的上下文估算诊断：整帧校验后原样呈现。
// App 不自算预算，也不把 components 合计校准成 input_tokens；分项是启发式剖分，不是实际 usage。
struct ContextStatus: Equatable {
    static let tokenLimit = 1_000_000_000, countLimit = 1_000_000, skillLimit = 128
    static let sources = ["heuristic", "usage_delta"]
    static let componentKeys = ["text", "images", "opaque_reasoning", "overhead"]
    var inputTokens: Int
    var reserveTokens: Int
    var totalTokens: Int
    var contextLimit: Int
    var visibleTurns: Int
    var totalTurns: Int
    var summaryCutoff: Int
    var imageCount: Int
    var memoryCount: Int
    var heuristicInputTokens: Int
    var estimateSource: String
    var components: [String: Int]
    var activeSkill: String?
    init?(_ event: [String: Any]) {
        func bounded(_ value: Any?, _ limit: Int) -> Int? {
            NativeEventEnvelope.integer(value).flatMap { (0...limit).contains($0) ? $0 : nil }
        }
        func count(_ key: String, _ limit: Int) -> Int? { bounded(event[key], limit) }
        guard let input = count("input_tokens", Self.tokenLimit), let reserve = count("reserve_tokens", Self.tokenLimit),
              let total = count("total_tokens", Self.tokenLimit), let limit = count("context_limit", Self.tokenLimit), limit >= 1,
              let visible = count("visible_turns", Self.countLimit), let turns = count("total_turns", Self.countLimit),
              let cutoff = count("summary_cutoff", Self.countLimit), let images = count("image_count", Self.countLimit),
              let memories = count("memory_count", Self.countLimit), let heuristic = count("heuristic_input_tokens", Self.tokenLimit),
              let source = event["estimate_source"] as? String, Self.sources.contains(source),
              let raw = event["components"] as? [String: Any] else { return nil }
        var components: [String: Int] = [:]
        for key in Self.componentKeys {
            guard let value = bounded(raw[key], Self.tokenLimit) else { return nil }
            components[key] = value
        }
        // 缺失或 null 都表示未启用 Skill；类型错误或超长整帧丢弃。
        if let value = event["active_skill"], !(value is NSNull) {
            guard let name = MemoryJSON.text(value, limit: Self.skillLimit) else { return nil }
            activeSkill = name.isEmpty ? nil : name
        }
        inputTokens = input; reserveTokens = reserve; totalTokens = total; contextLimit = limit
        visibleTurns = visible; totalTurns = turns; summaryCutoff = cutoff; imageCount = images; memoryCount = memories
        heuristicInputTokens = heuristic; estimateSource = source; self.components = components
    }
    var fraction: Double { min(1, max(0, Double(totalTokens) / Double(contextLimit))) }
    // 不夹紧：超过上限时如实显示超过 100。
    var percentText: String { String(Int((Double(totalTokens) / Double(contextLimit) * 100).rounded())) + "%" }
    var overLimit: Bool { totalTokens > contextLimit }
    var componentsTotal: Int { components.values.reduce(0, +) }
    var sourceLabel: String {
        estimateSource == "usage_delta" ? L("上次实际 usage + 增量估算") : L("字符启发式（未经 usage 校准）")
    }
    static func componentLabel(_ key: String) -> String {
        switch key {
        case "text": return L("文本")
        case "images": return L("图片")
        case "opaque_reasoning": return L("不透明推理")
        case "overhead": return L("固定开销")
        default: return key
        }
    }
}

// 长期记忆目录只来自 host 的 memory_catalog；字段与 memory_store/memory_runtime 的上限一致。
enum MemoryJSON {
    static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }
    // Python 按码点计长；Swift 的 Character 计数会把组合字符算短。
    static func text(_ value: Any?, limit: Int = .max) -> String? {
        guard let text = value as? String, text.unicodeScalars.count <= limit else { return nil }
        return text
    }
    // 键必须存在：null 为 .some(nil)，类型错误或缺失为 nil。
    static func nullable<T>(_ value: Any?, _ parse: (Any) -> T?) -> T?? {
        guard let value else { return nil }
        if value is NSNull { return .some(nil) }
        guard let parsed = parse(value) else { return nil }
        return .some(parsed)
    }
}
struct MemorySource: Equatable {
    var sessionID: String?
    var turn: Int?
    var step: Int?
    var origin: String
}
struct MemoryEntry: Identifiable, Equatable {
    static let kinds = ["preference", "workflow", "fact"]
    static let titleLimit = 120, contentLimit = 2000, appLimit = 120
    var id: String
    var revision: Int
    var status: String
    var kind: String
    var title: String
    var content: String
    var app: String?
    var source: MemorySource
    var validation: String
    var createdAt: Int
    var updatedAt: Int
    var isCandidate: Bool { status == "candidate" }
    init?(_ value: Any) {
        guard let item = value as? [String: Any],
              let id = MemoryJSON.text(item["id"], limit: 64), !id.isEmpty,
              let revision = NativeEventEnvelope.integer(item["revision"]), revision >= 1,
              let status = MemoryJSON.text(item["status"]), ["candidate", "approved"].contains(status),
              let kind = MemoryJSON.text(item["kind"]), Self.kinds.contains(kind),
              let title = MemoryJSON.text(item["title"], limit: Self.titleLimit),
              let content = MemoryJSON.text(item["content"], limit: Self.contentLimit),
              let app = MemoryJSON.nullable(item["app"], { MemoryJSON.text($0, limit: Self.appLimit) }),
              let source = item["source"] as? [String: Any],
              let session = MemoryJSON.nullable(source["session_id"], { MemoryJSON.text($0) }),
              let turn = MemoryJSON.nullable(source["turn"], { NativeEventEnvelope.integer($0) }),
              let step = MemoryJSON.nullable(source["step"], { NativeEventEnvelope.integer($0) }),
              let origin = MemoryJSON.text(source["origin"]), !origin.isEmpty,
              let validation = MemoryJSON.text(item["validation"]), !validation.isEmpty,
              let created = NativeEventEnvelope.integer(item["created_at"]),
              let updated = NativeEventEnvelope.integer(item["updated_at"]) else { return nil }
        self.id = id; self.revision = revision; self.status = status; self.kind = kind
        self.title = title; self.content = content; self.app = app
        self.source = MemorySource(sessionID: session, turn: turn, step: step, origin: origin)
        self.validation = validation; createdAt = created; updatedAt = updated
    }
}
struct MemoryCatalog {
    static let entryLimit = 64
    // 与 memory_store 上限一致：候选 128 + 已批准 256。
    static let offsetLimit = 384
    var entries: [MemoryEntry]
    var generation: Int
    var offset: Int
    var nextOffset: Int?
    var candidateCount: Int
    var approvedCount: Int
    var omitted: Int
    var root: String
    var available: Bool
    var errorCode: String?
    var sourceCommandID: String?
    // 任何字段不合法都整帧丢弃，不部分应用。
    init?(_ event: [String: Any]) {
        guard let raw = event["entries"] as? [Any], raw.count <= Self.entryLimit,
              let generation = NativeEventEnvelope.integer(event["generation"]), generation >= 0,
              let candidates = NativeEventEnvelope.integer(event["candidate_count"]), (0...128).contains(candidates),
              let approved = NativeEventEnvelope.integer(event["approved_count"]), (0...256).contains(approved),
              let omitted = NativeEventEnvelope.integer(event["omitted"]), omitted >= 0,
              MemoryJSON.text(event["policy"]) == "candidate_review",
              let root = MemoryJSON.text(event["root"]),
              let available = MemoryJSON.boolean(event["available"]),
              let errorCode = MemoryJSON.nullable(event["error_code"], { MemoryJSON.text($0) }),
              let source = MemoryJSON.nullable(event["source_command_id"], { MemoryJSON.text($0).flatMap { $0.isEmpty ? nil : $0 } })
        else { return nil }
        var ids: Set<String> = []
        var entries: [MemoryEntry] = []
        for item in raw {
            guard let entry = MemoryEntry(item), ids.insert(entry.id).inserted else { return nil }
            entries.append(entry)
        }
        let shownCandidates = entries.filter(\.isCandidate).count
        guard shownCandidates <= candidates, entries.count - shownCandidates <= approved,
              candidates + approved - entries.count == omitted else { return nil }
        // 旧 host 不带分页字段：按首页、无下一页兼容；字段一旦出现即严格校验。
        var offset = 0
        if let raw = event["offset"] {
            guard let value = NativeEventEnvelope.integer(raw), (0...Self.offsetLimit).contains(value) else { return nil }
            offset = value
        }
        var nextOffset: Int?
        if let raw = event["next_offset"] {
            guard let value = MemoryJSON.nullable(raw, { NativeEventEnvelope.integer($0) }) else { return nil }
            let end = offset + entries.count, total = candidates + approved
            if let value {
                guard !entries.isEmpty, value == end, value < total, value <= Self.offsetLimit else { return nil }
            } else {
                guard entries.isEmpty || end >= total else { return nil }
            }
            nextOffset = value
        }
        self.entries = entries; self.generation = generation; self.offset = offset; self.nextOffset = nextOffset
        candidateCount = candidates; approvedCount = approved; self.omitted = omitted
        self.root = root; self.available = available; self.errorCode = errorCode; sourceCommandID = source
    }
}
// 连接级数据：跨会话保留目录；状态只随 host 目录更新，从不乐观修改 status。
struct MemoryControlsState {
    static let commands = ["memory_list", "memory_approve", "memory_reject", "memory_forget", "memory_update"]
    var entries: [MemoryEntry] = []
    var generation: Int?
    // 当前页只随已接受的目录变化；翻页请求不乐观移动。
    var offset = 0
    var nextOffset: Int?
    var candidateCount = 0
    var approvedCount = 0
    var omitted = 0
    var root = ""
    var available = false
    var errorCode: String?
    var received = false
    // 变更结果未知（超时）时目录只读保留，直到收到新目录。
    var catalogUnknown = false
    var pending: [String: PendingControl] = [:]
    // 已收到 control_result、等待不早于其 generation 的目录；仍占用写等待，超时或断开后迟到帧按过期处理。
    var awaitingCatalog: [String: PendingControl] = [:]
    var retired: [String] = []
    var status = ""
    var busy: Bool { pending.values.contains { $0.name != "memory_list" } || !awaitingCatalog.isEmpty }
    func isPending(_ name: String) -> Bool { pending.values.contains { $0.name == name } }
    func isPending(memory id: String) -> Bool { (Array(pending.values) + Array(awaitingCatalog.values)).contains { $0.memoryID == id } }
    func owns(_ id: String) -> Bool { pending[id] != nil || awaitingCatalog[id] != nil || retired.contains(id) }
    mutating func retire(_ id: String) {
        pending[id] = nil; awaitingCatalog[id] = nil
        if !retired.contains(id) { retired.append(id); retired = Array(retired.suffix(64)) }
    }
    // generation 是数据库修订号，不与会话事件 sequence 混用；同 generation 可接受（失败回执附当前快照）。
    mutating func accept(_ catalog: MemoryCatalog) -> Bool {
        if let generation, catalog.generation < generation { return false }
        if let id = catalog.sourceCommandID {
            // 未知、已超时或已完成命令的目录一律视为过期。
            guard let command = pending[id] ?? awaitingCatalog[id] else { return false }
            // 读取回复必须是所请求的页；变更目录是首页，且不得早于 control_result 的 generation。
            if command.name == "memory_list" { guard catalog.offset == command.offset else { return false } }
            else { guard catalog.offset == 0, catalog.generation >= command.generation ?? 0 else { return false } }
            if pending[id]?.name == "memory_list" {
                retire(id)
                if !isPending("memory_list") && status == L("正在读取记忆目录…") { status = "" }
            } else if awaitingCatalog[id] != nil { retire(id) }
        } else if catalog.offset != 0 { return false }
        // 不早于结果 generation 的权威目录已包含该变更，可解除写等待（例如手动刷新恢复）。
        for (id, command) in awaitingCatalog where catalog.generation >= command.generation ?? .max { retire(id) }
        entries = catalog.entries; generation = catalog.generation; offset = catalog.offset; nextOffset = catalog.nextOffset
        candidateCount = catalog.candidateCount; approvedCount = catalog.approvedCount; omitted = catalog.omitted
        root = catalog.root; available = catalog.available; errorCode = catalog.errorCode
        received = true; catalogUnknown = false
        return true
    }
    // 会话切换只退役绑定旧 task 的读取；断开连接时目录不再可用，待确认结果未知。
    mutating func carryOver(disconnect: Bool) {
        var lost = false
        for (id, command) in Array(pending) where disconnect || !command.taskID.isEmpty { retire(id); lost = true }
        if disconnect {
            lost = lost || !awaitingCatalog.isEmpty
            for id in Array(awaitingCatalog.keys) { retire(id) }
            if received || lost { status = L("连接已断开：记忆目录与待确认操作均未确认；重新连接后请刷新。") }
            available = false; generation = nil
        } else if lost {
            status = L("记忆目录读取未确认；请刷新。")
        }
    }
    static func kindLabel(_ kind: String) -> String {
        switch kind {
        case "preference": return L("偏好")
        case "workflow": return L("流程")
        case "fact": return L("事实")
        default: return kind
        }
    }
    static func statusLabel(_ status: String) -> String {
        status == "candidate" ? L("候选") : status == "approved" ? L("已批准") : status
    }
    static func validationLabel(_ validation: String) -> String {
        switch validation {
        case "pending_user_review": return L("待用户审核")
        case "user_confirmed": return L("用户已确认（不代表业务已核验）")
        default: return validation
        }
    }
    static func errorText(_ code: String?) -> String {
        switch code ?? "" {
        case "MEMORY_SENSITIVE": return L("疑似包含密码、令牌或凭证，已拒绝保存。")
        case "MEMORY_AUTHORITY": return L("包含永久授权、免确认或自动付款等授权承诺，已拒绝保存。")
        case "MEMORY_STALE_REFERENCE": return L("包含坐标、节点、窗口或句柄等易失定位，不能长期保存。")
        case "MEMORY_INVALID": return L("参数无效。")
        case "MEMORY_TOO_LARGE": return L("内容超过长度上限（标题 120、正文 2000 字符）。")
        case "MEMORY_CANDIDATE_LIMIT": return L("待审核候选已达上限，请先审核或拒绝。")
        case "MEMORY_APPROVED_LIMIT": return L("已批准记忆已达上限，请先遗忘不再需要的条目。")
        case "MEMORY_REVISION_CONFLICT": return L("记忆已被修改，请刷新后重新阅读。")
        case "MEMORY_NOT_FOUND": return L("记忆不存在或已被删除。")
        case "MEMORY_INVALID_STATE": return L("当前状态不允许该操作。")
        case "MEMORY_DUPLICATE": return L("已有相同内容的记忆。")
        case "MEMORY_UNSAFE_PATH": return L("记忆目录不安全，记忆功能已停用。")
        case "MEMORY_SCHEMA_UNSUPPORTED": return L("记忆数据库版本不受支持，记忆功能已停用。")
        case "MEMORY_UNAVAILABLE": return L("记忆存储暂不可用。")
        default: return L("未知错误（%@）。", code ?? "")
        }
    }
}

// 只保存本次连接的状态；会话权限和审批绝不写入 Preferences。
struct SessionControlsState {
    var memory = MemoryControlsState()
    var skills: [SkillEntry] = []
    var catalogAvailable = false
    var catalogStatus = ""
    var catalogSequence: Int?
    // selectedSkill 是期望值（界面所选），effectiveSkill 只随 host 的生效状态更新。
    var selectedSkill: String?
    var skillOverride = SkillOverride.unspecified
    var effectiveSkill: String?
    var skillSource = ""
    var skillDigest = ""
    var skillPending = false
    var sessionID = ""
    var model = ""
    var wireProtocol = ""
    var reasoningEffort = ""
    var foregroundAllowed = false
    var askUserEnabled = true
    var queue: [QueuedInput] = []
    var queueAvailable = false
    var queueSuspended = false
    var approvals: [SessionApproval] = []
    var closedApprovals: Set<String> = []
    var pending: [String: PendingControl] = [:]
    var settled: [String: PendingControl] = [:]
    var terminalCommands: Set<String> = []
    var messageStates: [String: String] = [:]
    var result = ""
    var budgetBefore: String?
    var budgetAfter: String?
    var interruptPending = false
    var interrupted = false
    var workerReset = false
    var compacting = false
    var compactionRunning = false
    var stage = ""
    var stageText = ""
    var attempt = 0
    var maxAttempts = 0
    var timeoutSeconds = 0.0
    var turnStartedAt: Date?
    var turnEndedAt: Date?
    static let previewCharacterLimit = 65_536
    static let previewBlockLimit = 32
    var intentPreview = ""
    var intentBlock = 0
    var intentStep: Int?
    var intentComplete = false
    var reasoning = ""
    var reasoningComplete = false
    var reasoningTruncated = false
    var code: [ExecutionCode] = []
    var codeTruncated = false
    var turnIndex = -1
    var sequence: Int?
    var attemptID: String?
    var retiredAttempts: Set<String> = []
    var retiredAttemptOrder: [String] = []
    var turnClosed = false
    var hasTurnStarted = false
    var awaitingNextTurn = false
    var envelopeSessionID = ""
    var canonicalTurnID: String?
    var retiredTurnIDs: [String] = []
    var envelopeSequence: Int?
    var seenEventIDs: Set<String> = []
    var seenEventOrder: [String] = []
    var lastEnvelope: [String: String] = [:]
    static let recentActionLimit = 64
    static let seenEventLimit = 256
    var recentActions: [NativeActionStatus] = []
    // 仅随会话保留最近一次合法估算；reset 新建状态即清空。
    var context: ContextStatus?
    var activeActionID: String?
    var activeAction: NativeActionStatus? { recentActions.last { $0.id == activeActionID } }
    var recoveryActions: [NativeActionStatus] = []
    var recoveryText = ""
    var recoveryUnverifiedCount = 0
    var recoveryUnknownCount = 0
    var hasRecoveryReport = false
    var recoverySummary: String {
        guard hasRecoveryReport else { return "" }
        return L("恢复报告：未核验 %@ 个，结果未知 %@ 个；请核实，不会自动重试。", String(recoveryUnverifiedCount), String(recoveryUnknownCount))
    }
    mutating func receiveAction(_ event: [String: Any]) {
        guard var action = NativeActionStatus(event) else { return }
        if let previous = recentActions.last(where: { $0.id == action.id }) {
            // SDK 可能只发 stage 增量；保留已报告的原始结果和诊断身份，不推测成功。
            action.metadata = previous.metadata.merging(action.metadata) { _, value in value }
            if event["execution"] == nil { action.execution = previous.execution }
            if event["effect"] == nil { action.effect = previous.effect }
            action.turn = previous.turn
            // 只带 stage 的增量或较弱事实不覆盖已有环境事实（与 Python _PRIORITY 一致）。
            if let old = previous.facts, action.facts.map({ $0.priority > old.priority }) ?? true { action.facts = old }
        }
        if action.turn < 0 { action.turn = turnIndex }
        recentActions.removeAll { $0.id == action.id }
        recentActions.append(action)
        recentActions = Array(recentActions.suffix(Self.recentActionLimit))
        if !turnClosed && !awaitingNextTurn && !interrupted && action.turn >= turnIndex { activeActionID = action.id }
    }
    mutating func receiveRecovery(_ event: [String: Any]) {
        let actions = event["actions"] as? [[String: Any]] ?? []
        var ids: Set<String> = []
        let unique = actions.compactMap { item -> NativeActionStatus? in
            var fields = event; fields["actions"] = nil
            for (key, value) in item { fields[key] = value }
            guard let action = NativeActionStatus(fields, recovery: true), ids.insert(action.id).inserted else { return nil }
            return action
        }
        recoveryActions = Array(unique.suffix(Self.recentActionLimit))
        recoveryUnverifiedCount = unique.filter { $0.stage == "receipt_unverified" }.count
        recoveryUnknownCount = unique.filter { $0.stage == "outcome_unknown" }.count
        recoveryText = String((event["text"] as? String ?? "").prefix(Self.previewCharacterLimit))
        hasRecoveryReport = true
        // 恢复快照不更新 activeAction、phase、模型阶段或会话权限，也不发送指令。
    }
    mutating func acceptDelivery(_ event: [String: Any]) -> Bool {
        let envelope = NativeEventEnvelope(fields: event)
        guard envelope.supported else { return false }
        if let id = event["event_id"] as? String, seenEventIDs.contains(id) { return false }
        if envelope.structured, let value = event["sequence"] as? Int {
            if let last = envelopeSequence, value <= last { return false }
            envelopeSequence = value
        }
        if let id = event["event_id"] as? String, !id.isEmpty {
            seenEventIDs.insert(id); seenEventOrder.append(id)
            if seenEventOrder.count > Self.seenEventLimit { seenEventIDs.remove(seenEventOrder.removeFirst()) }
        }
        lastEnvelope = envelope.metadata
        return true
    }

    // 仅裁剪界面缓存，不改变 host 的请求、代码或轨迹。
    static func preview(_ text: String, truncated: inout Bool) -> String {
        if text.count > previewCharacterLimit {
            truncated = true
            return String(text.prefix(previewCharacterLimit))
        }
        return text
    }
    mutating func resetExecutionPreview() {
        intentPreview = ""; intentBlock = 0; intentStep = nil; intentComplete = false
        reasoning = ""; reasoningComplete = false; reasoningTruncated = false
        code = []; codeTruncated = false
    }
    func control(for event: [String: Any]) -> PendingControl? {
        (event["command_id"] as? String).flatMap { pending[$0] ?? settled[$0] }
    }
    // 手动压缩在轮次之间运行：command_id/manual 可识别；轮次关闭时 purpose=compaction 也只能来自它。
    func sessionCompaction(_ event: [String: Any]) -> Bool {
        let kind = event["event"] as? String ?? ""
        guard kind == "progress" || kind == "model_status" else { return false }
        if control(for: event)?.name == "compaction" { return true }
        if kind == "progress" && event["phase"] as? String != "compaction" { return false }
        return event["manual"] as? Bool == true || (event["purpose"] as? String == "compaction" && (turnClosed || awaitingNextTurn))
    }
    // 压缩事件只更新压缩显示，不改变本轮 attempt、预览或会话阶段。
    func compactionEvent(_ event: [String: Any]) -> Bool {
        sessionCompaction(event) || (event["event"] as? String == "model_status" && event["purpose"] as? String == "compaction")
    }
    private mutating func retireAttempt(_ id: String) {
        guard retiredAttempts.insert(id).inserted else { return }
        retiredAttemptOrder.append(id)
        if retiredAttemptOrder.count > Self.seenEventLimit { retiredAttempts.remove(retiredAttemptOrder.removeFirst()) }
    }
    mutating func beginTurn(_ index: Int) {
        if index > turnIndex { sequence = nil }
        turnIndex = index; turnClosed = false; hasTurnStarted = false; awaitingNextTurn = false
        for (id, operation) in pending where !operation.taskID.isEmpty && !operation.sessionScoped && operation.turn < index {
            pending[id] = nil
        }
        if pending.isEmpty && L(result) == L("正在等待执行端确认…") { result = "" }
        if let id = canonicalTurnID {
            retiredTurnIDs.append(id); retiredTurnIDs = Array(retiredTurnIDs.suffix(Self.seenEventLimit))
        }
        canonicalTurnID = nil; activeActionID = nil
        if let id = attemptID { retireAttempt(id) }
        attemptID = nil; resetExecutionPreview()
        stage = ""; stageText = ""; attempt = 0; maxAttempts = 0; timeoutSeconds = 0
        interrupted = false; interruptPending = false; workerReset = false; compacting = false; compactionRunning = false
        approvals = []
        turnStartedAt = Date(); turnEndedAt = nil
    }
    mutating func accept(_ event: [String: Any]) -> Bool {
        // 拒绝路径不能先清掉新轮预览或消费 sequence/event_id。
        var next = self
        guard next.acceptDelivery(event), next.acceptTurn(event) else { return false }
        self = next
        return true
    }
    private mutating func acceptTurn(_ event: [String: Any]) -> Bool {
        let kind = event["event"] as? String ?? ""
        // 畸形估算整帧拒绝，不消费 sequence/event_id。
        if kind == "context_status", ContextStatus(event) == nil { return false }
        let sessionEvents = ["skills_catalog", "skill_state", "session_info", "policy", "queue_state", "message", "control_result", "command_ack", "error", "approval_closed", "trajectory", "finished", "skill_loaded", "skill_warning", "action_status", "recovery_report", "context_status"]
        let sessionReply = kind == "control_result" && control(for: event)?.sessionScoped == true
        let turn = event["turn_id"] as? String
        if let turn, retiredTurnIDs.contains(turn), !sessionReply { return false }
        // opaque turn_id 只作相等/退役判断，绝不按字符串字典序比较。
        if let turn, let current = canonicalTurnID, turn != current, !sessionReply {
            if let index = event["turn_index"] as? Int {
                guard index > turnIndex, !["action_status", "recovery_report", "context_status"].contains(kind) else { return false }
            } else {
                guard kind == "turn_started" || (kind == "model_status" && ["waiting", "api_wait"].contains(event["stage"] as? String ?? "")) else { return false }
                if kind != "turn_started" { beginTurn(turnIndex + 1) }
            }
        }
        let index = event["turn_index"] as? Int
        if let index, index < turnIndex {
            // 会话级命令可能比当前轮旧；匹配 pending 后交给 command_id 去重，不推进本轮 sequence。
            return sessionReply
        }
        if index == nil || index == turnIndex {
            if let sequence = event["sequence"] as? Int, let last = self.sequence, sequence <= last { return false }
        }
        if kind == "turn_started" {
            if let index {
                guard index > turnIndex || (index == turnIndex && !hasTurnStarted && !turnClosed) else { return false }
                if index > turnIndex { beginTurn(index) }
            } else if let turn, turn == canonicalTurnID, turnIndex >= 0 {
                guard !hasTurnStarted && !turnClosed && !awaitingNextTurn else { return false }
            } else { beginTurn(turnIndex + 1) }
            hasTurnStarted = true
        } else if let index, index > turnIndex {
            // 动作不是轮次启动信号；只允许真实模型/轮次事件推进轮次。
            guard !["action_status", "recovery_report", "context_status"].contains(kind) else { return false }
            // 新轮可能先发送 session_info/progress；仍不接受旧轮的迟到事件。
            beginTurn(index)
        }
        if let turn, !sessionReply { canonicalTurnID = turn }
        let compaction = compactionEvent(event)
        if !compaction && kind != "turn_started" {
            if index != nil && (turnClosed || awaitingNextTurn), !sessionEvents.contains(kind), kind != "turn_finished" { return false }
            if interrupted && !awaitingNextTurn && !sessionEvents.contains(kind) { return false }
        }
        if kind == "turn_finished", index != nil && (turnClosed || awaitingNextTurn) { return false }
        // finished 是会话终态；task/sequence 已防旧，中止或等待下一轮时也必须接受。
        // 每步新请求（stage=waiting）从 attempt 1 重新计数，不能被上一步的重试次数拦截。
        let newRequest = !compaction && kind == "model_status" && ["waiting", "api_wait"].contains(event["stage"] as? String ?? "")
        if !compaction && (kind == "model_status" || kind == "action_status") && !newRequest, let number = event["attempt"] as? Int, number < attempt { return false }
        if kind == "context_status" {
            // 估算只是诊断：不建立或切换 attempt，压缩后的新估算也不算模型重试；仅拒绝已退役 attempt。
            if let raw = event["attempt_id"], !(raw is NSNull), retiredAttempts.contains(String(describing: raw)) { return false }
        } else if !compaction, let raw = event["attempt_id"], !(raw is NSNull) {
            let id = String(describing: raw)
            guard !retiredAttempts.contains(id) else { return false }
            if let current = attemptID, current != id {
                guard kind == "model_status", newRequest || (event["attempt"] as? Int ?? attempt) >= attempt else { return false }
                retireAttempt(current)
                resetExecutionPreview(); activeActionID = nil
            }
            // 动作可携带 attempt 诊断，但不能替模型事件建立新 attempt。
            if kind != "action_status" { attemptID = id }
        }
        if newRequest { resetExecutionPreview(); activeActionID = nil }
        if let sequence = event["sequence"] as? Int { self.sequence = sequence }
        return true
    }
}

@MainActor extension AppModel {
    var liveSession: Bool { ready && !disconnected && conversationOpen && !taskID.isEmpty }
    var canSwitchSession: Bool {
        !phase.active && !sendingMessage && !controls.compacting && controls.pending.values.allSatisfy { ["skills_list", "skills_reload"].contains($0.name) }
    }
    var canInterrupt: Bool { liveSession && phase.active && phase != .stopping && !controls.interrupted }
    var canCompact: Bool { liveSession && phase != .stopping && !controls.compacting && !controls.interruptPending }
    func controlPending(_ name: String) -> Bool { controls.pending.values.contains { $0.name == name } }
    func approvalPending(_ id: String) -> Bool { controls.pending.values.contains { $0.approvalID == id } }

    func resetSessionControls(clearCatalog: Bool = false) {
        var next = SessionControlsState()
        // 长期记忆目录属于连接；跨会话保留，断开时只清待确认与可用性。
        next.memory = controls.memory
        next.memory.carryOver(disconnect: clearCatalog)
        if !clearCatalog {
            next.skills = controls.skills; next.catalogAvailable = controls.catalogAvailable
            next.catalogStatus = controls.catalogStatus; next.catalogSequence = controls.catalogSequence
            // Skill 选择与生效状态属于会话；跨会话沿用会绕过历史 Skill 的摘要校验。
            // 空闲目录请求属于连接而非会话；切换会话后仍等待同一回执与超时。
            next.pending = controls.pending.filter { $0.value.taskID.isEmpty }
            next.settled = controls.settled.filter { $0.value.taskID.isEmpty }
            next.terminalCommands = controls.terminalCommands.intersection(Set(next.settled.keys))
            if !next.pending.isEmpty { next.result = L("正在等待执行端确认…") }
            if next.pending.isEmpty && L(next.catalogStatus) == L("正在读取 Skill 目录…") { next.catalogStatus = "" }
        }
        controls = next
    }
    func prepareControlTurn() {
        controls.awaitingNextTurn = true
        controls.interrupted = false; controls.interruptPending = false
        controls.resetExecutionPreview(); controls.activeActionID = nil; controls.stage = ""; controls.stageText = ""
        controls.turnStartedAt = Date(); controls.turnEndedAt = nil
    }
    func completeControlTurn(keepingSession: Bool) {
        // 压缩是会话命令，host 在轮次结束后才执行；只有会话结束才放弃等待。
        removePendingControls(keepingSession ? ["interrupt", "approval_reply"] : ["interrupt", "approval_reply", "compaction", "compaction_cancel"])
        controls.turnClosed = true; controls.awaitingNextTurn = false; controls.turnEndedAt = Date(); controls.activeActionID = nil
        if !keepingSession { controls.compactionRunning = false }
        controls.interruptPending = false; controls.compacting = controls.compactionRunning; controls.approvals = []
        if !keepingSession {
            controls.foregroundAllowed = false; controls.askUserEnabled = true
            controls.queue = []; controls.queueSuspended = false; controls.queueAvailable = false
        }
    }
    @discardableResult func sendControl(_ name: String, fields: [String: Any] = [:], idle: Bool = false, approvalID: String? = nil) -> String? {
        guard ready, !disconnected, idle || liveSession else {
            controls.result = L("此操作需要已连接的实时会话。")
            return nil
        }
        guard !controlPending(name) || name == "approval_reply" else { controls.result = L("正在等待执行端确认…"); return nil }
        let id = UUID().uuidString
        var request = fields
        request["command"] = name; request["command_id"] = id
        let scopeTask = liveSession ? taskID : (idle ? "" : taskID)
        if !scopeTask.isEmpty { request["task_id"] = scopeTask }
        controls.pending[id] = PendingControl(name: name, taskID: scopeTask, turn: controls.turnIndex, approvalID: approvalID)
        controls.result = L("正在等待执行端确认…")
        do { try bridge.send(request) }
        catch {
            controls.pending[id] = nil; controls.result = error.localizedDescription
            self.error = error.localizedDescription
            return nil
        }
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 15_000_000_000)
            self?.expireControl(id)
        }
        return id
    }
    func expireControl(_ id: String) {
        guard let pending = controls.pending.removeValue(forKey: id) else { return }
        guard pending.taskID.isEmpty || (pending.taskID == taskID && (pending.turn == controls.turnIndex || pending.sessionScoped)) else { return }
        controls.result = L("未收到确认，状态未知；请刷新后核实，不要自动重发。")
        if pending.name == "interrupt" { controls.interruptPending = false }
        if pending.name == "skill_select" {
            controls.skillPending = false; controls.selectedSkill = controls.effectiveSkill
        }
        if pending.name == "skills_list" || pending.name == "skills_reload" { controls.catalogStatus = controls.result }
        delegate?.refreshCompact()
    }
    func refreshSkills(reload: Bool = false) {
        controls.catalogStatus = L("正在读取 Skill 目录…")
        if sendControl(reload ? "skills_reload" : "skills_list", idle: true) == nil {
            controls.catalogStatus = controls.result
        }
    }
    func selectSkill(_ name: String?) {
        guard name == nil || (controls.catalogAvailable && controls.skills.contains { $0.name == name }) else {
            controls.catalogStatus = L("Skill 目录尚不可用，请刷新。")
            return
        }
        if !conversationOpen {
            // 未启动的新会话或历史会话：只记录显式覆盖，start 时再下发。
            let choice = name.map { SkillOverride.named($0) } ?? .noSkill
            guard controls.skillOverride != choice else { return }
            controls.skillOverride = choice; controls.selectedSkill = name; controls.skillPending = false
            controls.catalogStatus = L("将在新会话启动时使用所选 Skill。")
            return
        }
        guard controls.selectedSkill != name else { return }
        if sendControl("skill_select", fields: ["name": name.map { $0 as Any } ?? NSNull()]) != nil {
            controls.selectedSkill = name; controls.skillPending = phase.active
        }
    }
    func setForegroundAllowed(_ value: Bool) {
        _ = sendControl("set_policy", fields: ["foreground_allowed": value])
    }
    func setAskUserEnabled(_ value: Bool) {
        if !conversationOpen { controls.askUserEnabled = value; return }
        _ = sendControl("set_policy", fields: ["ask_user_enabled": value])
    }
    func interruptTurn(force: Bool = false) {
        guard canInterrupt else { controls.result = L("当前没有可中止的轮次。"); return }
        if controls.interruptPending && !force { return }
        if force {
            // 强制请求可以替代尚未确认的普通中止，但旧回执不能清除新 pending。
            for (id, pending) in controls.pending where pending.name == "interrupt" { controls.pending[id] = nil }
        }
        if sendControl("interrupt", fields: ["force": force]) != nil { controls.interruptPending = true }
        delegate?.refreshCompact()
    }
    func refreshQueue() { _ = sendControl("queue_list") }
    func resumeQueue() { _ = sendControl("queue_resume") }
    func clearQueue() { _ = sendControl("queue_clear") }
    func compactContext() { if canCompact { _ = sendControl("compaction") } }
    func cancelCompaction() { _ = sendControl("compaction_cancel") }
    // 取消已确认但压缩回执已丢失时，结束本地 running，避免会话切换被锁死。
    private func settleCompactionCancel() {
        if !controlPending("compaction") { controls.compactionRunning = false; controls.compacting = false }
    }
    func replyToApproval(_ id: String, choice: String = "reject") {
        guard ["allow", "reject", "cancel"].contains(choice),
              controls.approvals.contains(where: { $0.id == id }), !approvalPending(id) else { return }
        _ = sendControl("approval_reply", fields: ["approval_id": id, "choice": choice], approvalID: id)
    }
    func removePendingControls(_ names: [String], approvalID: String? = nil) {
        for (id, pending) in controls.pending where names.contains(pending.name) && (approvalID == nil || pending.approvalID == approvalID) {
            controls.settled[id] = pending
            controls.pending[id] = nil
        }
        if controls.pending.isEmpty && L(controls.result) == L("正在等待执行端确认…") { controls.result = "" }
        if controls.settled.count > 64 { controls.settled = [:]; controls.terminalCommands = [] }
    }
    func receiveCatalog(_ event: [String: Any]) {
        if NativeEventEnvelope.transportID(event) == nil, let sequence = event["sequence"] as? Int {
            if let last = controls.catalogSequence, sequence <= last { return }
            controls.catalogSequence = sequence
        }
        var names: Set<String> = []
        controls.skills = (event["skills"] as? [[String: Any]] ?? []).compactMap { item in
            guard let name = item["name"] as? String, !name.isEmpty, names.insert(name).inserted else { return nil }
            return SkillEntry(name: name, description: item["description"] as? String ?? "", source: item["source"] as? String ?? "")
        }
        controls.catalogAvailable = true
        // 无会话时保留用户本地选择；host 的目录默认项不能覆盖它。
        if conversationOpen && NativeEventEnvelope.transportID(event) != nil && !controlPending("skill_select") && !controls.skillPending {
            controls.selectedSkill = event["selected_name"] as? String
        } else if !conversationOpen, let selected = controls.skillOverride.name, !names.contains(selected) {
            controls.skillOverride = .unspecified; controls.selectedSkill = nil
        }
        controls.catalogStatus = controls.skills.isEmpty ? L("没有可用 Skill；可选择不选。") : ""
        removePendingControls(["skills_list", "skills_reload"])
    }
    @discardableResult func receiveControlEvent(_ event: [String: Any]) -> Bool {
        let kind = event["event"] as? String ?? ""
        switch kind {
        case "action_status": controls.receiveAction(event)
        case "recovery_report": controls.receiveRecovery(event)
        // 只投影估算；不改 phase、授权、队列、错误或发送状态。
        case "context_status": if let status = ContextStatus(event) { controls.context = status }
        case "skills_catalog": receiveCatalog(event)
        case "skill_state":
            let name = event["name"] as? String
            let command = controls.control(for: event)
            if event["pending"] as? Bool ?? false {
                // 待生效只更新期望值；skill_select 仍等待 applied 或终态回执。
                controls.selectedSkill = name; controls.skillPending = true
                return true
            }
            controls.effectiveSkill = name
            controls.skillSource = event["source"] as? String ?? ""
            controls.skillDigest = event["digest"] as? String ?? ""
            if command?.name == "skill_select", let id = event["command_id"] as? String {
                controls.selectedSkill = name; controls.skillPending = false
                if controls.pending[id] != nil { controls.settled[id] = controls.pending.removeValue(forKey: id) }
                if controls.pending.isEmpty && L(controls.result) == L("正在等待执行端确认…") { controls.result = "" }
            } else if !controls.skillPending && !controlPending("skill_select") {
                // 模型本轮自行加载只同步生效值；有待生效选择时不覆盖用户期望。
                controls.selectedSkill = name
            }
        case "session_info":
            controls.sessionID = event["session_id"] as? String ?? ""
            controls.model = event["model"] as? String ?? ""
            controls.wireProtocol = event["protocol"].map { String(describing: $0) } ?? ""
            controls.reasoningEffort = event["reasoning_effort"] as? String ?? ""
            controls.foregroundAllowed = event["foreground_allowed"] as? Bool ?? false
            controls.askUserEnabled = event["ask_user_enabled"] as? Bool ?? true
            removePendingControls(["set_policy"])
            if !controlPending("queue_list") { refreshQueue() }
        case "policy":
            controls.foregroundAllowed = event["foreground_allowed"] as? Bool ?? false
            controls.askUserEnabled = event["ask_user_enabled"] as? Bool ?? true
            removePendingControls(["set_policy"])
        case "queue_state":
            var ids: Set<String> = []
            controls.queue = (event["items"] as? [[String: Any]] ?? []).compactMap { item in
                guard let id = item["id"] as? String, !id.isEmpty, ids.insert(id).inserted else { return nil }
                return QueuedInput(id: id, text: item["text"] as? String ?? "", kind: item["kind"] as? String ?? "")
            }
            controls.queueAvailable = true; controls.queueSuspended = event["suspended"] as? Bool ?? false
            removePendingControls(["queue_list", "queue_resume", "queue_clear"])
        case "approval":
            guard let id = event["approval_id"] as? String, !controls.closedApprovals.contains(id) else { return true }
            let approval = SessionApproval(id: id, appID: String(describing: event["app_id"] ?? ""),
                                           windowID: String(describing: event["window_id"] ?? ""),
                                           text: event["text"] as? String ?? "", scope: event["scope"] as? String ?? "session")
            if !controls.approvals.contains(where: { $0.id == id }) { controls.approvals.append(approval) }
        case "approval_closed":
            guard let id = event["approval_id"] as? String else { return true }
            controls.closedApprovals.insert(id); controls.approvals.removeAll { $0.id == id }
            removePendingControls(["approval_reply"], approvalID: id)
            // allow 的真实 policy 由 host 发出；关闭卡片不授予任何本地权限。
        case "interrupted":
            controls.interrupted = true; controls.interruptPending = false
            controls.workerReset = event["worker_reset"] as? Bool ?? false
            sendingMessage = false
            controls.stage = "interrupted"; controls.stageText = event["text"] as? String ?? L("本轮已中止，可在同一会话发送纠正。")
            controls.turnClosed = true; controls.turnEndedAt = Date()
            // 中止只结束本轮；已排队的会话压缩仍等待自己的 control_result。
            controls.approvals = []; controls.compacting = controls.compactionRunning
            removePendingControls(["interrupt"])
            finish(.stopped, text: controls.stageText, keepingConversation: true)
        case "model_status":
            controls.stage = event["stage"] as? String ?? ""
            controls.stageText = event["text"] as? String ?? ""
            if controls.compactionEvent(event) {
                if controls.sessionCompaction(event) { controls.compactionRunning = true; controls.compacting = true }
                return true
            }
            controls.attempt = event["attempt"] as? Int ?? 0; controls.maxAttempts = event["max_attempts"] as? Int ?? 0
            controls.timeoutSeconds = (event["timeout_seconds"] as? NSNumber)?.doubleValue ?? 0
            if controls.stage == "waiting" || controls.stage == "retrying" || controls.stage == "cancelled" || controls.stage == "error" {
                intent = ""
            }
        case "intent_delta":
            let block = event["block_index"] as? Int ?? controls.intentBlock
            let sourceStep = event["step"] as? Int
            if event["start"] as? Bool == true || block != controls.intentBlock || sourceStep != controls.intentStep {
                controls.intentPreview = ""; controls.intentComplete = false
                controls.intentBlock = block; controls.intentStep = sourceStep
            }
            guard !controls.intentComplete else { return true }
            controls.intentPreview = String((controls.intentPreview + (event["text"] as? String ?? "")).prefix(4096))
        case "intent_end": controls.intentComplete = true
        case "reasoning_delta":
            guard !controls.reasoningComplete else { return true }
            if event["start"] as? Bool == true { controls.reasoning = ""; controls.reasoningTruncated = false }
            let text = controls.reasoning + (event["text"] as? String ?? "")
            controls.reasoning = SessionControlsState.preview(text, truncated: &controls.reasoningTruncated)
        case "reasoning_end": controls.reasoningComplete = true
        case "code_reset": controls.resetExecutionPreview(); intent = ""
        case "code_start", "code_delta", "code_end":
            let index = event["block_index"] as? Int ?? 0
            let text = event["text"] as? String ?? ""
            if kind == "code_start" {
                if controls.code.contains(where: { $0.index == index && $0.complete }) { return true }
                controls.code.removeAll { $0.index == index }
                guard controls.code.count < SessionControlsState.previewBlockLimit else { controls.codeTruncated = true; return true }
                let preview = SessionControlsState.preview(text, truncated: &controls.codeTruncated)
                controls.code.append(ExecutionCode(index: index, step: event["step"] as? Int ?? step, text: preview))
            } else if let position = controls.code.firstIndex(where: { $0.index == index }), !controls.code[position].complete {
                if kind == "code_end" {
                    if !text.isEmpty { controls.code[position].text = SessionControlsState.preview(text, truncated: &controls.codeTruncated) }
                    controls.code[position].complete = true
                } else {
                    let combined = controls.code[position].text + text
                    controls.code[position].text = SessionControlsState.preview(combined, truncated: &controls.codeTruncated)
                }
            }
        case "control_result":
            guard let id = event["command_id"] as? String, !controls.terminalCommands.contains(id),
                  let pending = controls.pending[id] ?? controls.settled[id] else { return true }
            let matchesScope = pending.taskID.isEmpty ? NativeEventEnvelope.transportID(event) == nil : (pending.taskID == taskID && NativeEventEnvelope.transportID(event) == taskID)
            guard matchesScope, pending.turn == controls.turnIndex || pending.taskID.isEmpty || pending.sessionScoped else { return true }
            let status = event["status"] as? String ?? ""
            let inProgress = ["pending", "started", "accepted", "running"].contains(status)
            // 中间回执不能解除操作等待或丢掉超时；状态事件先到时也不能重新打开旧 pending。
            if !inProgress {
                controls.pending[id] = nil; controls.settled[id] = pending
                controls.terminalCommands.insert(id)
            }
            controls.result = (event["text"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? L("控制结果：%@", status)
            if let before = event["budget_before"] { controls.budgetBefore = String(describing: before) }
            if let after = event["budget_after"] { controls.budgetAfter = String(describing: after) }
            if ["error", "failed", "rejected", "cancelled"].contains(status) {
                if pending.name == "interrupt" { controls.interruptPending = false }
                if pending.name == "compaction" { controls.compactionRunning = false; controls.compacting = false }
                if pending.name == "skills_list" || pending.name == "skills_reload" { controls.catalogStatus = controls.result }
                if pending.name == "skill_select" { controls.skillPending = false; controls.selectedSkill = controls.effectiveSkill }
            } else if pending.name == "compaction", controls.pending[id] != nil || !inProgress {
                controls.compactionRunning = inProgress; controls.compacting = inProgress
            } else if pending.name == "compaction_cancel", !inProgress { settleCompactionCancel() }
            else if pending.name == "skill_select", !inProgress {
                // 终态回执后期望值收敛到 host 报告的生效值。
                controls.skillPending = false; controls.selectedSkill = controls.effectiveSkill
            }
            if pending.name == "interrupt", controls.terminalCommands.contains(id) { controls.interruptPending = false }
        default: return false
        }
        return true
    }
    func receiveControlAcknowledgement(_ event: [String: Any]) -> Bool {
        guard let id = event["command_id"] as? String, let pending = controls.pending[id] else { return false }
        if let task = NativeEventEnvelope.transportID(event), task != pending.taskID { return true }
        guard pending.taskID.isEmpty || pending.taskID == taskID, pending.turn == controls.turnIndex || pending.taskID.isEmpty || pending.sessionScoped else { return true }
        if event["event"] as? String == "error" {
            controls.pending[id] = nil; controls.result = event["text"] as? String ?? L("执行端拒绝")
            if pending.name == "interrupt" { controls.interruptPending = false }
            if pending.name == "compaction_cancel" { settleCompactionCancel() }
            if pending.name == "skill_select" { controls.skillPending = false; controls.selectedSkill = controls.effectiveSkill }
            if pending.name == "skills_list" || pending.name == "skills_reload" { controls.catalogStatus = controls.result }
        }
        // command_ack 只表示接收；等待状态事件/control_result 或超时，不能虚构成功。
        return true
    }

    func memoryPending(_ name: String) -> Bool { controls.memory.isPending(name) }
    // 只有目录读取在实时会话中带 task_id；变更走连接级，避免会话切换使 task 过期。
    // 独立于 controls.pending：不影响 phase、发送、前台授权、Skill 或会话切换。
    @discardableResult func sendMemoryCommand(_ name: String, fields: [String: Any] = [:], memoryID: String? = nil) -> String? {
        guard ready, !disconnected else { controls.memory.status = L("未连接执行端，记忆状态未确认。"); return nil }
        guard name == "memory_list" ? !memoryPending(name) : !controls.memory.busy else {
            controls.memory.status = L("上一条记忆操作仍在等待执行端确认。"); return nil
        }
        let id = UUID().uuidString
        var request = fields
        request["command"] = name; request["command_id"] = id
        let scopeTask = name == "memory_list" && liveSession ? taskID : ""
        if !scopeTask.isEmpty { request["task_id"] = scopeTask }
        let command = PendingControl(name: name, taskID: scopeTask, turn: controls.turnIndex, memoryID: memoryID,
                                     offset: NativeEventEnvelope.integer(fields["offset"]) ?? 0)
        controls.memory.pending[id] = command
        do { try bridge.send(request) }
        catch {
            controls.memory.pending[id] = nil
            controls.memory.status = L("记忆命令未送达：%@ 状态未确认，请刷新后核实。", error.localizedDescription)
            return nil
        }
        scheduleMemoryExpiry(id, issued: command.started)
        return id
    }
    private func scheduleMemoryExpiry(_ id: String, issued: Date) {
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 15_000_000_000)
            self?.expireMemoryControl(id, issued: issued)
        }
    }
    // issued 标识触发本次超时的阶段；收到 control_result 后等待目录重新计时，发送阶段的旧定时器失效。
    // 超时只退役并提示刷新，不自动重发；变更结果未知时目录只读保留。
    func expireMemoryControl(_ id: String, issued: Date? = nil) {
        if let pending = controls.memory.pending[id] {
            guard issued == nil || issued == pending.started else { return }
            controls.memory.retire(id)
            if pending.name != "memory_list" { controls.memory.catalogUnknown = true }
            controls.memory.status = L("未收到记忆操作确认，状态未确认；请刷新后核实，不要自动重发。")
        } else if let awaiting = controls.memory.awaitingCatalog[id] {
            guard issued == nil || issued == awaiting.started else { return }
            controls.memory.retire(id)
            controls.memory.catalogUnknown = true
            controls.memory.status = L("已收到记忆操作结果，但未收到更新后的目录；目录状态未确认，仅可阅读。请刷新后核实，不要自动重发。")
        } else { return }
        delegate?.refreshCompact()
    }
    // 只请求指定页；当前页与条目等待目录回写。首页不带 offset，与旧 host 兼容。
    func refreshMemory(offset: Int? = nil) {
        guard !memoryPending("memory_list") else { return }
        let offset = offset ?? controls.memory.offset
        guard (0...MemoryCatalog.offsetLimit).contains(offset) else { return }
        if sendMemoryCommand("memory_list", fields: offset > 0 ? ["offset": offset] : [:]) != nil { controls.memory.status = L("正在读取记忆目录…") }
    }
    func previousMemoryPage() {
        if controls.memory.offset > 0 { refreshMemory(offset: max(0, controls.memory.offset - MemoryCatalog.entryLimit)) }
    }
    func nextMemoryPage() { if let next = controls.memory.nextOffset { refreshMemory(offset: next) } }
    // 只对用户正在阅读的当前版本操作；目录已变化时要求重新阅读，不替用户确认新内容。
    private func reviewedMemory(_ entry: MemoryEntry, candidate: Bool? = nil) -> MemoryEntry? {
        guard !controls.memory.catalogUnknown else {
            controls.memory.status = L("记忆目录状态未确认，仅可阅读；请刷新后再操作。")
            return nil
        }
        guard controls.memory.available, controls.memory.entries.first(where: { $0.id == entry.id }) == entry else {
            controls.memory.status = L("记忆已在执行端变化，请重新阅读当前内容后再操作。")
            return nil
        }
        if let candidate, entry.isCandidate != candidate { controls.memory.status = MemoryControlsState.errorText("MEMORY_INVALID_STATE"); return nil }
        return entry
    }
    private func mutateMemory(_ name: String, _ entry: MemoryEntry, fields: [String: Any] = [:]) {
        var request = fields
        request["memory_id"] = entry.id; request["revision"] = entry.revision
        if sendMemoryCommand(name, fields: request, memoryID: entry.id) != nil { controls.memory.status = L("正在等待执行端确认…") }
    }
    func approveMemory(_ entry: MemoryEntry) {
        if let entry = reviewedMemory(entry, candidate: true) { mutateMemory("memory_approve", entry) }
    }
    func rejectMemory(_ entry: MemoryEntry) {
        if let entry = reviewedMemory(entry, candidate: true) { mutateMemory("memory_reject", entry) }
    }
    func forgetMemory(_ entry: MemoryEntry) {
        if let entry = reviewedMemory(entry) { mutateMemory("memory_forget", entry) }
    }
    // 任何编辑都由 host 退回候选；只发送改动字段，空 app 表示不限应用。
    func updateMemory(_ entry: MemoryEntry, title: String, content: String, kind: String, app: String) {
        guard let entry = reviewedMemory(entry) else { return }
        let title = title.trimmingCharacters(in: .whitespacesAndNewlines)
        let content = content.trimmingCharacters(in: .whitespacesAndNewlines)
        let app = app.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty, !content.isEmpty, MemoryEntry.kinds.contains(kind),
              title.unicodeScalars.count <= MemoryEntry.titleLimit, content.unicodeScalars.count <= MemoryEntry.contentLimit,
              app.unicodeScalars.count <= MemoryEntry.appLimit else {
            controls.memory.status = L("标题与正文不能为空，标题最多 120 字符、正文最多 2000 字符。")
            return
        }
        var fields: [String: Any] = [:]
        if title != entry.title { fields["title"] = title }
        if content != entry.content { fields["content"] = content }
        if kind != entry.kind { fields["kind"] = kind }
        if app != (entry.app ?? "") { fields["app"] = app.isEmpty ? NSNull() : app }
        guard !fields.isEmpty else { controls.memory.status = L("内容没有变化，未发送。"); return }
        mutateMemory("memory_update", entry, fields: fields)
    }
    // 记忆帧专用路由：调用方已完成严格信封与 task/session 围栏；这里只做 event_id/sequence 去重，
    // 不进入轮次过滤，不改 turn 身份、phase、发送状态、授权或 controls.result。畸形或过期帧整帧不提交。
    func receiveMemoryFrame(_ event: [String: Any], kind: String) -> Bool {
        let id = event["command_id"] as? String
        guard kind == "memory_catalog" || (id.map { controls.memory.owns($0) } == true && ["command_ack", "error", "control_result"].contains(kind)) else { return false }
        let transport = NativeEventEnvelope.transportID(event)
        var next = controls
        var catalogWait: Date?
        guard next.acceptDelivery(event) else { return true }
        switch kind {
        case "memory_catalog":
            guard let catalog = MemoryCatalog(event), next.memory.accept(catalog) else { return true }
        case "control_result":
            // 重复或超时后的结果只吞掉；结果不改条目，条目只随不早于其 generation 的目录更新。
            // generation 缺失或无效时整帧忽略，继续等待合法结果或超时。
            guard let id, var pending = next.memory.pending[id], pending.name != "memory_list",
                  pending.taskID.isEmpty ? transport == nil : transport == pending.taskID,
                  let status = event["status"] as? String,
                  let generation = NativeEventEnvelope.integer(event["generation"]), generation >= 0 else { return true }
            // 写等待持续到目录回写，防止用旧 revision 重复批准/拒绝；等待目录阶段重新计时。
            pending.generation = generation; pending.started = Date(); catalogWait = pending.started
            next.memory.pending[id] = nil; next.memory.awaitingCatalog[id] = pending
            if status == "completed" {
                switch pending.name {
                case "memory_approve": next.memory.status = L("记忆已确认；用户确认不代表业务结果已核验。")
                case "memory_reject": next.memory.status = L("候选记忆已拒绝。")
                case "memory_forget": next.memory.status = L("记忆已遗忘。")
                default: next.memory.status = L("已保存为候选，需要重新确认。")
                }
            } else if status == "failed" {
                next.memory.status = L("记忆操作失败：%@ 请刷新后核实。", MemoryControlsState.errorText(event["error_code"] as? String))
            } else {
                next.memory.status = L("记忆操作状态未知（%@）；请刷新后核实。", status)
            }
        case "error":
            guard let id, let pending = next.memory.pending[id], transport == nil || transport == pending.taskID else { return true }
            next.memory.retire(id)
            next.memory.status = L("执行端拒绝记忆命令：%@ 状态未改变，请刷新后核实。", L(event["text"] as? String ?? "执行端拒绝"))
        default:
            // command_ack 只表示受理；等待 control_result/目录或超时。
            return true
        }
        controls = next
        if let id, let catalogWait { scheduleMemoryExpiry(id, issued: catalogWait) }
        return true
    }
}
