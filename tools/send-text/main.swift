import AppKit
import Foundation

/// URL-scheme helper:
/// `sendtext://send?key=<normalized_key>&to=<phone>&name=<who>&message=<text>&status=<stage>`
/// or, for a decline, `...&decline_reason=<code>&decline_note=<text>` instead of `status`.
///
/// Shows a confirmation window (recipient + editable message), sends the text
/// through the Messages app — SMS account first (works for any phone when the
/// iPhone forwards texts to this Mac), else iMessage — then logs it on the lead
/// via set-lead-status (or decline-lead), so there's no separate Message sent click.
/// Nothing is sent until Send is clicked in the window.

private let scheme = "sendtext"

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var didHandleURL = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleGetURLEvent(_:withReplyEvent:)),
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )

        for arg in CommandLine.arguments.dropFirst() {
            if let url = URL(string: arg), url.scheme?.lowercased() == scheme {
                didHandleURL = true
                handle(url)
            }
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            guard let self = self, !self.didHandleURL else { return }
            NSApp.terminate(nil)
        }
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls where url.scheme?.lowercased() == scheme {
            didHandleURL = true
            handle(url)
        }
        NSApp.terminate(nil)
    }

    @objc private func handleGetURLEvent(
        _ event: NSAppleEventDescriptor,
        withReplyEvent replyEvent: NSAppleEventDescriptor
    ) {
        guard let urlString = event.paramDescriptor(forKeyword: keyDirectObject)?.stringValue,
              let url = URL(string: urlString)
        else { return }
        didHandleURL = true
        handle(url)
        NSApp.terminate(nil)
    }

    private func handle(_ url: URL) {
        guard let config = loadConfig() else {
            alert("SendText is missing its config (re-run tools/send-text/install.sh).")
            return
        }
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> String {
            items.first(where: { $0.name == name })?.value ?? ""
        }
        let key = value("key")
        let to = value("to").filter { $0.isNumber || $0 == "+" }
        guard !key.isEmpty, !to.isEmpty else {
            alert("sendtext://send requires ?key=<normalized_key>&to=<phone>")
            return
        }
        let name = value("name")
        let declineReason = value("decline_reason")
        let status = value("status")
        if declineReason.isEmpty && status.isEmpty {
            alert("sendtext://send requires &status=<stage> or &decline_reason=<code>")
            return
        }

        guard let text = confirm(to: to, name: name, message: value("message")) else { return }

        if let err = sendViaMessages(to: to, text: text) {
            alert("Messages couldn't send the text — nothing was logged.\n\n\(err)")
            return
        }

        let args: [String]
        let bin: String
        if !declineReason.isEmpty {
            bin = config.declineBinPath
            args = ["--db", config.dbPath, "--key", key, "--reason", declineReason,
                    "--channel", "sms", "--letter-text", text]
                + (value("decline_note").isEmpty ? [] : ["--note", value("decline_note")])
        } else {
            bin = config.statusBinPath
            args = ["--db", config.dbPath, "--key", key, "--status", status,
                    "--sent-channel", "sms", "--sent-message", text]
        }
        if let err = run(bin, args) {
            alert("The text was sent, but logging it on the lead failed — click Message sent in Manage lead.\n\n\(err)")
        }
    }

    /// Recipient + editable message; returns the text to send, or nil on Cancel.
    private func confirm(to: String, name: String, message: String) -> String? {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = "Text \(name.isEmpty ? to : "\(name) (\(to))")?"
        a.informativeText = "Sent from Messages and logged on the lead. Edit below if you like."
        a.addButton(withTitle: "Send")
        a.addButton(withTitle: "Cancel")

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 440, height: 220))
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        let tv = NSTextView(frame: scroll.bounds)
        tv.isRichText = false
        tv.font = NSFont.systemFont(ofSize: 13)
        tv.string = message
        tv.autoresizingMask = [.width]
        tv.isVerticallyResizable = true
        scroll.documentView = tv
        a.accessoryView = scroll
        a.window.initialFirstResponder = tv

        guard a.runModal() == .alertFirstButtonReturn else { return nil }
        let text = tv.string.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }

    private func sendViaMessages(to: String, text: String) -> String? {
        func esc(_ s: String) -> String {
            s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        }
        let source = """
        tell application "Messages"
            set svc to missing value
            try
                set svc to 1st account whose service type = SMS
            end try
            if svc is missing value then set svc to 1st account whose service type = iMessage
            send "\(esc(text))" to participant "\(esc(to))" of svc
        end tell
        """
        var error: NSDictionary?
        NSAppleScript(source: source)?.executeAndReturnError(&error)
        if let error = error {
            return (error[NSAppleScript.errorMessage] as? String) ?? "\(error)"
        }
        return nil
    }

    private func run(_ bin: String, _ args: [String]) -> String? {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: bin)
        proc.arguments = args
        let errPipe = Pipe()
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = errPipe
        do {
            try proc.run()
            proc.waitUntilExit()
        } catch {
            return error.localizedDescription
        }
        if proc.terminationStatus != 0 {
            let data = errPipe.fileHandleForReading.readDataToEndOfFile()
            return (String(data: data, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return nil
    }

    private struct Config {
        let statusBinPath: String
        let declineBinPath: String
        let dbPath: String
    }

    private func loadConfig() -> Config? {
        guard let url = Bundle.main.url(forResource: "config", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: String],
              let status = obj["statusBinPath"], let decline = obj["declineBinPath"], let db = obj["dbPath"]
        else { return nil }
        return Config(statusBinPath: status, declineBinPath: decline, dbPath: db)
    }

    private func alert(_ message: String) {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = "Send text"
        a.informativeText = message
        a.alertStyle = .warning
        a.runModal()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
