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
    let created_at: Double
}
struct Feed: Codable { let events: [AttentionEvent]; let cursor: Int64 }
struct NotifierState: Codable {
    var enabled = true
    var cursor: Int64? = nil
    var bootstrap = true

    mutating func setEnabled(_ value: Bool) {
        if value && !enabled { bootstrap = true }
        enabled = value
    }
    mutating func consume(_ feed: Feed, now: Double) -> [AttentionEvent] {
        let previous = cursor
        cursor = feed.cursor
        defer { bootstrap = false }
        guard enabled, !bootstrap, let previous, feed.cursor >= previous else { return [] }
        var seen = Set<Int64>()
        return feed.events.sorted { $0.event_id < $1.event_id }.filter {
            $0.event_id > previous && $0.event_id <= feed.cursor && seen.insert($0.event_id).inserted
                && $0.created_at >= now - 900 && $0.created_at <= now + 60
        }
    }
}
func inboxURL(_ conversation: String) -> URL {
    let valid = conversation.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil
    return URL(string: inboxOrigin + "/" + (valid ? "?conversation=\(conversation)" : ""))!
}
func plain(_ value: String, limit: Int) -> String {
    String(value.components(separatedBy: .controlCharacters).joined(separator: " ")
        .split(whereSeparator: { $0.isWhitespace }).joined(separator: " ").prefix(limit))
}
struct BridgeRequest {
    let method: String
    let path: String
    let headers: [String: String]
    static func parse(_ data: Data) -> BridgeRequest? {
        guard data.count <= 8192, let raw = String(data: data, encoding: .utf8),
              let boundary = raw.range(of: "\r\n\r\n") else { return nil }
        let lines = raw[..<boundary.lowerBound].components(separatedBy: "\r\n")
        let first = lines[0].split(separator: " ")
        guard first.count == 3, first[2] == "HTTP/1.1" else { return nil }
        var headers = [String: String]()
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { return nil }
            let key = line[..<colon].lowercased()
            guard headers[key] == nil else { return nil }
            headers[key] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
        return BridgeRequest(method: String(first[0]), path: String(first[1]), headers: headers)
    }
    func authorized(token: String) -> Bool {
        guard headers["host"] == bridgeHost,
              headers["origin"] == inboxOrigin,
              headers["transfer-encoding"] == nil,
              [nil, "0", "2"].contains(headers["content-length"]) else { return false }
        if method == "OPTIONS" {
            return ["/status", "/enable", "/disable"].contains(path)
                && ["GET", "POST"].contains(headers["access-control-request-method"] ?? "")
                && (headers["access-control-request-headers"] ?? "").lowercased()
                    .split(separator: ",").allSatisfy {
                        ["content-type", "x-notifier-csrf"].contains($0.trimmingCharacters(in: .whitespaces))
                    }
        }
        if method == "GET" && path == "/status" { return true }
        return method == "POST" && ["/enable", "/disable"].contains(path)
            && headers["x-notifier-csrf"] == token && headers["content-type"] == "application/json"
    }
}
func retryDelay(_ failures: Int) -> Double { min(60, 4 * pow(2, Double(min(failures, 4)))) }
