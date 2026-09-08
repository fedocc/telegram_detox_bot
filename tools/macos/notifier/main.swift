import AppKit
import Foundation
import Network
import UserNotifications
import Darwin

// All state transitions run on the main queue. No Telegram client or credential access.
final class Notifier: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate,
                      URLSessionTaskDelegate {
    let root = URL(fileURLWithPath: notifierDirectory)
    var state = NotifierState()
    let token = UUID().uuidString + UUID().uuidString
    var listener: NWListener?
    var lockFD: Int32 = -1
    var failures = 0
    var connected = false
    var permission = "unknown"
    var submitted = 0
    var delivered = 0
    var generation = 0
    var timer: DispatchWorkItem?
    var connections = 0
    lazy var session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 10
        config.timeoutIntervalForResource = 15
        config.urlCache = nil
        config.httpShouldSetCookies = false
        config.connectionProxyDictionary = [:]
        return URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }()
    var stateURL: URL { root.appendingPathComponent("notifier_state.json") }
    func log(_ message: String) { print("\(ISO8601DateFormatter().string(from: Date())) \(message)"); fflush(stdout) }
    func save() throws {
        let data = try JSONEncoder().encode(state)
        try data.write(to: stateURL, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateURL.path)
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            lockFD = Darwin.open(root.appendingPathComponent("notifier.lock").path, O_CREAT | O_RDWR, 0o600)
            guard lockFD >= 0, flock(lockFD, LOCK_EX | LOCK_NB) == 0 else { exit(0) }
            if FileManager.default.fileExists(atPath: stateURL.path) {
                do { state = try JSONDecoder().decode(NotifierState.self, from: Data(contentsOf: stateURL)) }
                catch { state.enabled = false; log("Invalid state; notifications disabled") }
            }
            try save()
            let center = UNUserNotificationCenter.current()
            center.delegate = self
            center.requestAuthorization(options: [.alert, .sound]) { _, _ in self.refreshPermission() }
            try startBridge()
            log("Notifier started; bridge=\(bridgeHost)")
            poll()
        } catch { log("Notifier initialization failed"); exit(1) }
    }
    func refreshPermission() {
        UNUserNotificationCenter.current().getDeliveredNotifications { notifications in
            DispatchQueue.main.async { self.delivered = notifications.count }
        }
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            DispatchQueue.main.async {
                self.permission = settings.authorizationStatus == .authorized ? "authorized" : "denied"
            }
        }
    }
    func schedule(_ delay: Double) {
        timer?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.poll() }
        timer = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }
    func poll() {
        let epoch = generation
        let suffix = state.bootstrap || state.cursor == nil ? "" : "?after=\(state.cursor!)"
        let url = URL(string: "\(inboxOrigin)/api/notifications\(suffix)")!
        session.dataTask(with: url) { data, response, error in
            DispatchQueue.main.async {
                guard epoch == self.generation else { self.schedule(0); return }
                guard error == nil, let http = response as? HTTPURLResponse, http.statusCode == 200,
                      let data, data.count <= 1_048_576,
                      let feed = try? JSONDecoder().decode(Feed.self, from: data), feed.cursor >= 0 else {
                    self.connected = false
                    if self.failures == 0 { self.log("Feed unavailable; backing off") }
                    self.failures += 1; self.schedule(retryDelay(self.failures)); return
                }
                if self.failures > 0 { self.log("Feed connection restored") }
                self.failures = 0; self.connected = true
                let previous = self.state
                let events = self.state.consume(feed, now: Date().timeIntervalSince1970)
                do { try self.save() }
                catch {
                    self.state = previous; self.log("State write failed; banners suppressed")
                    self.schedule(60); return
                }
                // Persist cursor before submitting: at-most-once across process crashes.
                for event in events { self.notify(event) }
                self.schedule(4)
            }
        }.resume()
    }
    func notify(_ event: AttentionEvent) {
        let content = UNMutableNotificationContent()
        content.title = "Telegram — " + plain(event.title, limit: 100)
        content.subtitle = event.trigger_reason == "direct_reply" ? "Ответ на ваше сообщение" : "Упоминание"
        if !event.topic_title.isEmpty { content.subtitle += " · " + plain(event.topic_title, limit: 60) }
        content.body = plain(event.preview, limit: 240)
        content.sound = .default
        content.userInfo = ["conversation": event.conversation_id]
        UNUserNotificationCenter.current().add(UNNotificationRequest(
            identifier: "attention-\(event.event_id)", content: content, trigger: nil
        )) { error in
            DispatchQueue.main.async {
                if error != nil { self.log("Native notification submission failed") }
                else { self.submitted += 1 }
            }
        }
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completion: @escaping (UNNotificationPresentationOptions) -> Void) {
        DispatchQueue.main.async { completion(self.state.enabled ? [.banner, .sound] : []) }
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completion: @escaping () -> Void) {
        if response.actionIdentifier == UNNotificationDefaultActionIdentifier {
            let conversation = response.notification.request.content.userInfo["conversation"] as? String ?? ""
            DispatchQueue.main.async { NSWorkspace.shared.open(inboxURL(conversation)) }
        }
        completion()
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil) // Notifier only talks to the fixed localhost API.
    }
    func startBridge() throws {
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: bridgePort)!)
        listener = try NWListener(using: parameters)
        listener?.stateUpdateHandler = { state in
            if case .failed = state { self.log("Bridge bind failed"); exit(1) }
        }
        listener?.newConnectionHandler = { connection in
            guard self.connections < 32 else { connection.cancel(); return }
            self.connections += 1
            connection.start(queue: .main)
            let timeout = DispatchWorkItem { connection.cancel() }
            DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: timeout)
            self.receive(connection, buffer: Data(), timeout: timeout)
        }
        listener?.start(queue: .main)
    }
    func receive(_ connection: NWConnection, buffer: Data, timeout: DispatchWorkItem) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 8193 - buffer.count) { data, _, done, error in
            var accumulated = buffer; if let data { accumulated.append(data) }
            if accumulated.count > 8192 || done || error != nil {
                timeout.cancel(); connection.cancel(); self.connections -= 1; return
            }
            guard accumulated.range(of: Data("\r\n\r\n".utf8)) != nil else {
                self.receive(connection, buffer: accumulated, timeout: timeout); return
            }
            timeout.cancel()
            self.respond(connection, request: BridgeRequest.parse(accumulated))
        }
    }
    func respond(_ connection: NWConnection, request: BridgeRequest?) {
        var code = 403
        var body: [String: Any] = ["error": "Forbidden"]
        var cors = ""
        if let request, request.authorized(token: token) {
            cors = "Access-Control-Allow-Origin: \(inboxOrigin)\r\nVary: Origin\r\n"
                + "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                + "Access-Control-Allow-Headers: Content-Type, X-Notifier-CSRF\r\n"
            code = 200
            if request.method == "POST" {
                let previous = state
                state.setEnabled(request.path == "/enable")
                do {
                    try save()
                    if state.enabled != previous.enabled { generation += 1 }
                    if !state.enabled {
                        UNUserNotificationCenter.current().removeAllPendingNotificationRequests()
                        UNUserNotificationCenter.current().removeAllDeliveredNotifications()
                    }
                } catch { state = previous; code = 503 }
            }
            refreshPermission()
            body = ["enabled": state.enabled, "csrf": token, "permission": permission,
                    "connected": connected, "submitted": submitted, "delivered": delivered]
        }
        let payload = (try? JSONSerialization.data(withJSONObject: body)) ?? Data()
        let headers = "HTTP/1.1 \(code) \(code == 200 ? "OK" : "Error")\r\n"
            + "Content-Type: application/json\r\nCache-Control: no-store\r\n"
            + "X-Content-Type-Options: nosniff\r\nConnection: close\r\n"
            + cors + "Content-Length: \(payload.count)\r\n\r\n"
        connection.send(content: Data(headers.utf8) + payload, completion: .contentProcessed { _ in
            connection.cancel(); self.connections -= 1
        })
    }
}
let app = NSApplication.shared
let delegate = Notifier()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
