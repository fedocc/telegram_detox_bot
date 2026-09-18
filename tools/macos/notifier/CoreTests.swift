import Foundation

@main struct CoreTests {
    static func main() throws {
        var checks = 0
        func check(_ value: @autoclosure () -> Bool, _ name: String) {
            precondition(value(), name); checks += 1
        }
        let conversationA = String(repeating: "a", count: 32)
        let conversationB = String(repeating: "b", count: 32)
        func event(_ id: Int64, conversation: String = String(repeating: "a", count: 32),
                   time: Double = 1000, preview: String = "Synthetic", unread: Int = 1) -> AttentionEvent {
            AttentionEvent(event_id: id, conversation_id: conversation, title: "Test",
                           topic_title: "", preview: preview, trigger_reason: "mention",
                           unread_count: unread, created_at: time)
        }
        func isIncomplete(_ data: Data) -> Bool {
            if case .incomplete = BridgeRequest.parseResult(data) { return true }
            return false
        }
        func isInvalid(_ data: Data) -> Bool {
            if case .invalid = BridgeRequest.parseResult(data) { return true }
            return false
        }

        var state = NotifierState()
        check(state.consume(Feed(events: [event(1)], cursor: 1), now: 1000).isEmpty,
              "cold bootstrap")
        let grouped = state.consume(Feed(events: [
            event(2, preview: "first", unread: 1),
            event(2, preview: "duplicate", unread: 1),
            event(3, preview: "latest", unread: 2),
            event(4, conversation: conversationB, preview: "other", unread: 1),
        ], cursor: 4), now: 1000)
        check(grouped.count == 2, "one event per conversation")
        check(grouped[0].event_id == 3 && grouped[0].preview == "latest"
              && grouped[0].unread_count == 2, "latest preview and count win")
        check(grouped[1].conversation_id == conversationB, "different conversations stay separate")
        check(state.consume(Feed(events: [event(4, conversation: conversationB)], cursor: 4),
                            now: 1000).isEmpty, "repeated poll")
        state = try JSONDecoder().decode(NotifierState.self, from: JSONEncoder().encode(state))
        check(state.consume(Feed(events: [event(4)], cursor: 4), now: 1000).isEmpty,
              "restart dedup")

        let legacyEvent = Data("""
            {"event_id":9,"conversation_id":"\(conversationA)","title":"T","topic_title":"",
             "preview":"P","trigger_reason":"mention","created_at":1000}
            """.utf8)
        let decodedLegacyEvent = try JSONDecoder().decode(AttentionEvent.self, from: legacyEvent)
        check(decodedLegacyEvent.unread_count == 1,
              "old feed without unread count remains decodable")

        for seconds in [600, 1800, 3600, 10800, 21600, 43200] {
            var snoozed = NotifierState(enabled: true, cursor: 10, bootstrap: false, mute_until: nil)
            check(snoozed.snooze(seconds: seconds, now: 1000), "accept snooze \(seconds)")
            check(snoozed.visibleMuteUntil(now: 1000) == 1000 + Double(seconds),
                  "persist snooze \(seconds)")
        }
        var snoozed = NotifierState(enabled: true, cursor: 10, bootstrap: false, mute_until: nil)
        check(!snoozed.snooze(seconds: 42, now: 1000), "reject unknown snooze")
        check(snoozed.snooze(seconds: 600, now: 1000), "start snooze")
        check(snoozed.consume(Feed(events: [event(11, time: 1100)], cursor: 11),
                              now: 1100).isEmpty, "snooze suppresses")
        check(snoozed.cursor == 11, "snooze cursor advances")
        snoozed = try JSONDecoder().decode(NotifierState.self, from: JSONEncoder().encode(snoozed))
        let afterWake = snoozed.consume(Feed(events: [
            event(12, time: 1500, preview: "muted"),
            event(13, time: 1601, preview: "after mute"),
        ], cursor: 13), now: 1602)
        check(afterWake.map(\.event_id) == [13], "restart and wake do not replay muted backlog")
        check(snoozed.visibleMuteUntil(now: 1602) == nil && snoozed.effectiveEnabled(now: 1602),
              "expired snooze reports enabled")

        var early = NotifierState(enabled: true, cursor: 20, bootstrap: false, mute_until: nil)
        check(early.snooze(seconds: 600, now: 1000), "early-unmute setup")
        early.setEnabled(true, now: 1100)
        let afterEnableNow = early.consume(Feed(events: [
            event(21, time: 1050, preview: "during mute"),
            event(22, time: 1101, preview: "after enable"),
        ], cursor: 22), now: 1102)
        check(afterEnableNow.map(\.event_id) == [22], "enable now has no snooze backlog")
        check(early.visibleMuteUntil(now: 1102) == nil, "enable now clears visible pause")

        var disabled = NotifierState(enabled: true, cursor: 30, bootstrap: false, mute_until: nil)
        disabled.setEnabled(false, now: 1000)
        check(disabled.consume(Feed(events: [event(31)], cursor: 31), now: 1000).isEmpty,
              "OFF suppresses")
        check(disabled.cursor == 31 && disabled.mute_until == nil, "OFF advances and is permanent")
        check(!disabled.snooze(seconds: 600, now: 1000), "OFF cannot snooze")
        disabled.setEnabled(true, now: 1000)
        check(disabled.consume(Feed(events: [event(32)], cursor: 32), now: 1000).isEmpty,
              "OFF to ON skips offline backlog")
        check(disabled.consume(Feed(events: [event(33)], cursor: 33), now: 1000).count == 1,
              "ON resumes new events")
        check(disabled.consume(Feed(events: [event(34, time: 0)], cursor: 34), now: 2000).isEmpty,
              "sleep stale suppression")
        check(disabled.consume(Feed(events: [], cursor: 0), now: 2000).isEmpty,
              "backend reset safely rebases")

        var invalidConversation = NotifierState(enabled: true, cursor: 0, bootstrap: false,
                                                mute_until: nil)
        check(invalidConversation.consume(Feed(events: [event(1, conversation: "bad")], cursor: 1),
                                          now: 1000).isEmpty, "invalid conversation suppressed")
        check(notificationIdentifier(conversationA) == "conversation:\(conversationA)",
              "stable native identifier")
        check(notificationIdentifier("bad") == nil, "invalid native identifier")
        check(retryDelay(1) == 8 && retryDelay(100) == 60, "bounded offline backoff")
        check(inboxURL(conversationA).absoluteString.hasSuffix("?conversation=" + conversationA),
              "click route")
        check(inboxURL("https://evil.test").absoluteString == "http://127.0.0.1:8787/",
              "invalid route fallback")
        check(plain(" hi\n\tthere ", limit: 8) == "hi there", "plain preview")

        let base = ["host": "127.0.0.1:8788", "origin": "http://127.0.0.1:8787"]
        check(BridgeRequest(method: "GET", path: "/status", headers: base).authorized(token: "t"),
              "status")
        let emptyBody = Data("{}".utf8)
        let post = base.merging([
            "x-notifier-csrf": "t", "content-type": "application/json",
            "content-length": String(emptyBody.count),
        ]) { _, b in b }
        for path in ["/enable", "/disable", "/snooze", "/conversation-opened"] {
            check(BridgeRequest(method: "POST", path: path, headers: post,
                                body: emptyBody).authorized(token: "t"), path)
        }
        for (method, path) in [("GET", "/enable"), ("POST", "/exec"), ("POST", "/status"),
                               ("POST", "/disable?cmd=x")] {
            check(!BridgeRequest(method: method, path: path, headers: post,
                                 body: emptyBody).authorized(token: "t"), "route allowlist")
        }
        for key in ["origin", "host", "x-notifier-csrf", "content-type"] {
            var invalid = post; invalid[key] = "wrong"
            check(!BridgeRequest(method: "POST", path: "/enable", headers: invalid,
                                 body: emptyBody).authorized(token: "t"), "reject " + key)
        }
        var transfer = post; transfer["transfer-encoding"] = "chunked"
        check(!BridgeRequest(method: "POST", path: "/enable", headers: transfer,
                             body: emptyBody).authorized(token: "t"), "reject transfer encoding")
        check(!BridgeRequest(method: "GET", path: "/status",
                             headers: base.merging(["content-length": "2"]) { _, b in b },
                             body: emptyBody).authorized(token: "t"), "GET body rejected")
        let options = base.merging([
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type, x-notifier-csrf",
        ]) { _, b in b }
        check(BridgeRequest(method: "OPTIONS", path: "/snooze",
                            headers: options).authorized(token: "t"), "preflight")
        var wrongPreflight = options; wrongPreflight["access-control-request-method"] = "GET"
        check(!BridgeRequest(method: "OPTIONS", path: "/snooze",
                             headers: wrongPreflight).authorized(token: "t"),
              "preflight method matches route")

        let complete = Data(("POST /snooze HTTP/1.1\r\nHost: 127.0.0.1:8788\r\n"
            + "Origin: http://127.0.0.1:8787\r\nContent-Type: application/json\r\n"
            + "X-Notifier-CSRF: t\r\nContent-Length: 15\r\n\r\n{\"seconds\":600}").utf8)
        let parsed = BridgeRequest.parse(complete)
        check(parsed?.path == "/snooze" && parsed?.json()?["seconds"] as? Int == 600,
              "parse complete JSON body")
        let partial = Data("POST /snooze HTTP/1.1\r\nContent-Length: 15\r\n\r\n{}".utf8)
        check(isIncomplete(partial), "fragmented body waits")
        check(isInvalid(complete + Data("x".utf8)), "trailing bytes rejected")
        check(isInvalid(Data("GET /status HTTP/1.1\r\nHost: x\r\nHost: y\r\n\r\n".utf8)),
              "duplicate headers rejected")
        check(isInvalid(Data("POST /x HTTP/1.1\r\nContent-Length: 257\r\n\r\n".utf8)),
              "oversized body rejected")
        check(isInvalid(Data("POST /x HTTP/1.1\r\nContent-Length: nope\r\n\r\n".utf8)),
              "invalid body length rejected")

        print("\(checks) Swift checks passed")
    }
}
