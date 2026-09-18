import Foundation

// QA is a separately compiled bundle and never connects to production or its tunnel.
#if NOTIFIER_QA
let inboxOrigin = "http://127.0.0.1:8877"
let bridgePort: UInt16 = 8878
let notifierDirectory = "/tmp/TelegramMentionInbox-QA"
#else
let inboxOrigin = "http://127.0.0.1:8787"
let bridgePort: UInt16 = 8788
let notifierDirectory = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/TelegramMentionInbox").path
#endif
let bridgeHost = "127.0.0.1:\(bridgePort)"

struct AttentionEvent: Codable {
    let event_id: Int64
    let conversation_id: String
    let title: String
    let topic_title: String
    let preview: String
    let trigger_reason: String
    let unread_count: Int
    let created_at: Double

    enum CodingKeys: String, CodingKey {
        case event_id, conversation_id, title, topic_title, preview, trigger_reason
        case unread_count, created_at
    }

    init(event_id: Int64, conversation_id: String, title: String, topic_title: String,
         preview: String, trigger_reason: String, unread_count: Int = 1, created_at: Double) {
        self.event_id = event_id
        self.conversation_id = conversation_id
        self.title = title
        self.topic_title = topic_title
        self.preview = preview
        self.trigger_reason = trigger_reason
        self.unread_count = unread_count
        self.created_at = created_at
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        event_id = try values.decode(Int64.self, forKey: .event_id)
        conversation_id = try values.decode(String.self, forKey: .conversation_id)
        title = try values.decode(String.self, forKey: .title)
        topic_title = try values.decode(String.self, forKey: .topic_title)
        preview = try values.decode(String.self, forKey: .preview)
        trigger_reason = try values.decode(String.self, forKey: .trigger_reason)
        unread_count = try values.decodeIfPresent(Int.self, forKey: .unread_count) ?? 1
        created_at = try values.decode(Double.self, forKey: .created_at)
    }
}
struct Feed: Codable { let events: [AttentionEvent]; let cursor: Int64 }
struct NotifierState: Codable {
    var enabled = true
    var cursor: Int64? = nil
    var bootstrap = true
    var mute_until: Double? = nil

    func effectiveEnabled(now: Double) -> Bool { enabled && (mute_until ?? 0) <= now }

    func visibleMuteUntil(now: Double) -> Double? {
        guard enabled, let mute_until, mute_until > now else { return nil }
        return mute_until
    }

    mutating func setEnabled(_ value: Bool, now: Double) {
        if value && !enabled {
            bootstrap = true
            mute_until = nil
        } else if value, let mute_until, mute_until > now {
            // Keep a past cutoff so an in-flight page cannot replay events that arrived
            // while snoozed. It is hidden from status once it is no longer active.
            self.mute_until = now
        } else if !value {
            mute_until = nil
        }
        enabled = value
    }
    mutating func snooze(seconds: Int, now: Double) -> Bool {
        guard [600, 1800, 3600, 10800, 21600, 43200].contains(seconds), enabled else { return false }
        mute_until = now + Double(seconds)
        return true
    }
    mutating func consume(_ feed: Feed, now: Double) -> [AttentionEvent] {
        let previous = cursor
        cursor = feed.cursor
        defer { bootstrap = false }
        guard effectiveEnabled(now: now), !bootstrap, let previous, feed.cursor >= previous else { return [] }
        var seen = Set<Int64>()
        var latestByConversation = [String: AttentionEvent]()
        for event in feed.events.sorted(by: { $0.event_id < $1.event_id }) {
            guard event.event_id > previous, event.event_id <= feed.cursor,
                  seen.insert(event.event_id).inserted,
                  notificationIdentifier(event.conversation_id) != nil,
                  event.created_at >= now - 900, event.created_at <= now + 60,
                  mute_until.map({ event.created_at > $0 }) != false else { continue }
            latestByConversation[event.conversation_id] = event
        }
        return latestByConversation.values.sorted { $0.event_id < $1.event_id }
    }
}
func validConversationID(_ conversation: String) -> Bool {
    conversation.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil
}
func notificationIdentifier(_ conversation: String) -> String? {
    validConversationID(conversation) ? "conversation:\(conversation)" : nil
}
func inboxURL(_ conversation: String) -> URL {
    URL(string: inboxOrigin + "/" + (validConversationID(conversation)
        ? "?conversation=\(conversation)" : ""))!
}
func plain(_ value: String, limit: Int) -> String {
    String(value.components(separatedBy: .controlCharacters).joined(separator: " ")
        .split(whereSeparator: { $0.isWhitespace }).joined(separator: " ").prefix(limit))
}
struct BridgeRequest {
    let method: String
    let path: String
    let headers: [String: String]
    let body: Data

    init(method: String, path: String, headers: [String: String], body: Data = Data()) {
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body
    }

    static func parseResult(_ data: Data) -> BridgeParseResult {
        guard data.count <= 8192 else { return .invalid }
        let marker = Data("\r\n\r\n".utf8)
        guard let boundary = data.range(of: marker) else { return .incomplete }
        let headerData = data[..<boundary.lowerBound]
        guard let rawHeaders = String(data: headerData, encoding: .utf8) else { return .invalid }
        let lines = rawHeaders.components(separatedBy: "\r\n")
        guard !lines.isEmpty else { return .invalid }
        let first = lines[0].split(separator: " ")
        guard first.count == 3, first[2] == "HTTP/1.1" else { return .invalid }
        var headers = [String: String]()
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { return .invalid }
            let key = line[..<colon].lowercased()
            guard !key.isEmpty, headers[key] == nil else { return .invalid }
            headers[key] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
        guard let length = Int(headers["content-length"] ?? "0"), length >= 0, length <= 256 else {
            return .invalid
        }
        let expected = boundary.upperBound + length
        guard data.count >= expected else { return .incomplete }
        guard data.count == expected else { return .invalid }
        return .request(BridgeRequest(
            method: String(first[0]), path: String(first[1]), headers: headers,
            body: data.subdata(in: boundary.upperBound..<expected)
        ))
    }
    static func parse(_ data: Data) -> BridgeRequest? {
        guard case let .request(request) = parseResult(data) else { return nil }
        return request
    }
    func authorized(token: String) -> Bool {
        guard headers["host"] == bridgeHost,
              headers["origin"] == inboxOrigin,
              headers["transfer-encoding"] == nil else { return false }
        if method == "OPTIONS" {
            guard body.isEmpty, headers["content-length"].flatMap(Int.init) ?? 0 == 0 else { return false }
            let requestedMethod = headers["access-control-request-method"] ?? ""
            let requestedHeaders = Set((headers["access-control-request-headers"] ?? "")
                .lowercased().split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) })
            if path == "/status" {
                return requestedMethod == "GET" && requestedHeaders.isEmpty
            }
            return ["/enable", "/disable", "/snooze", "/conversation-opened"].contains(path)
                && requestedMethod == "POST"
                && requestedHeaders == Set(["content-type", "x-notifier-csrf"])
        }
        if method == "GET" && path == "/status" {
            return body.isEmpty && (headers["content-length"].flatMap(Int.init) ?? 0) == 0
        }
        return method == "POST" && ["/enable", "/disable", "/snooze", "/conversation-opened"].contains(path)
            && headers["content-length"] != nil && body.count <= 256
            && headers["x-notifier-csrf"] == token && headers["content-type"] == "application/json"
    }
    func json() -> [String: Any]? {
        (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
    }
}
enum BridgeParseResult {
    case incomplete
    case invalid
    case request(BridgeRequest)
}
func retryDelay(_ failures: Int) -> Double { min(60, 4 * pow(2, Double(min(failures, 4)))) }
