import Foundation

@main struct CoreTests {
    static func main() throws {
        var checks = 0
        func check(_ value: @autoclosure () -> Bool, _ name: String) {
            precondition(value(), name); checks += 1
        }
        func event(_ id: Int64, time: Double = 1000) -> AttentionEvent {
            AttentionEvent(event_id: id, conversation_id: String(repeating: "a", count: 32),
                           title: "Test", topic_title: "", preview: "Synthetic", trigger_reason: "mention", created_at: time)
        }
        var state = NotifierState()
        check(state.consume(Feed(events: [event(1)], cursor: 1), now: 1000).isEmpty, "cold bootstrap")
        check(state.consume(Feed(events: [event(2), event(2)], cursor: 2), now: 1000).count == 1, "new and duplicate")
        check(state.consume(Feed(events: [event(2)], cursor: 2), now: 1000).isEmpty, "repeated poll")
        state = try JSONDecoder().decode(NotifierState.self, from: JSONEncoder().encode(state))
        check(state.consume(Feed(events: [event(2)], cursor: 2), now: 1000).isEmpty, "restart dedup")
        state.setEnabled(false); state.setEnabled(false)
        check(state.consume(Feed(events: [event(3)], cursor: 3), now: 1000).isEmpty, "OFF suppresses")
        check(state.cursor == 3, "OFF cursor advances")
        state.setEnabled(true)
        check(state.consume(Feed(events: [event(4)], cursor: 4), now: 1000).isEmpty, "ON skips offline backlog")
        state.setEnabled(true)
        check(!state.bootstrap, "idempotent ON")
        check(state.consume(Feed(events: [event(5)], cursor: 5), now: 1000).count == 1, "ON resumes new events")
        check(state.consume(Feed(events: [event(6, time: 0)], cursor: 6), now: 2000).isEmpty, "sleep stale suppression")
        check(state.consume(Feed(events: [], cursor: 0), now: 2000).isEmpty, "backend reset safely rebases")
        check(retryDelay(1) == 8 && retryDelay(100) == 60, "bounded offline backoff")
        check(inboxURL(String(repeating: "a", count: 32)).absoluteString.hasSuffix("?conversation=" + String(repeating: "a", count: 32)), "click route")
        check(inboxURL("https://evil.test").absoluteString == "http://127.0.0.1:8787/", "invalid route fallback")
        check(plain(" hi\n\tthere ", limit: 8) == "hi there", "plain preview")
        let base = ["host": "127.0.0.1:8788", "origin": "http://127.0.0.1:8787"]
        check(BridgeRequest(method: "GET", path: "/status", headers: base).authorized(token: "t"), "status")
        let post = base.merging(["x-notifier-csrf":"t", "content-type":"application/json"]) { _, b in b }
        for path in ["/enable", "/disable"] {
            check(BridgeRequest(method: "POST", path: path, headers: post).authorized(token: "t"), path)
        }
        for (method, path) in [("GET", "/enable"), ("POST", "/exec"), ("POST", "/status"), ("POST", "/disable?cmd=x")] {
            check(!BridgeRequest(method: method, path: path, headers: post).authorized(token: "t"), "route allowlist")
        }
        for key in ["origin", "host", "x-notifier-csrf", "content-type"] {
            var invalid = post; invalid[key] = "wrong"
            check(!BridgeRequest(method: "POST", path: "/enable", headers: invalid).authorized(token: "t"), "reject " + key)
        }
        let options = base.merging(["access-control-request-method":"POST", "access-control-request-headers":"content-type, x-notifier-csrf"]) { _, b in b }
        check(BridgeRequest(method: "OPTIONS", path: "/enable", headers: options).authorized(token: "t"), "preflight")
        check(BridgeRequest.parse(Data("GET /status HTTP/1.1\r\nHost: x\r\nHost: y\r\n\r\n".utf8)) == nil, "duplicate headers rejected")
        print("\(checks) Swift checks passed")
    }
}
