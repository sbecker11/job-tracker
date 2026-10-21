import AppKit
import Foundation

/// URL-scheme helper:
/// `recordreply://record?message_id=<unmatched id>&name=<recruiter>&text=<draft>&channel=linkedin`
///
/// LinkedIn never emails a copy of messages Shawn sends, so the pipeline can't
/// see his replies. The "Reply sent" button on an unmatched Pending Actions
/// card opens this window: confirm (or paste) what was actually sent and when,
/// then it runs `record-unmatched-reply`, which files the message on a lead and
/// starts the wait clock on the recruiter.

private let scheme = "recordreply"

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
            alert("RecordReply is missing its config (re-run tools/record-reply/install.sh).")
            return
        }
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> String {
            items.first(where: { $0.name == name })?.value ?? ""
        }
        let messageId = value("message_id")
        guard !messageId.isEmpty else {
            alert("recordreply://record requires ?message_id=<unmatched message id>")
            return
        }
        let name = value("name")
        let channel = value("channel").isEmpty ? "linkedin" : value("channel")

        guard let (text, sentAt) = confirm(name: name, draft: value("text")) else { return }

        var args = ["--db", config.dbPath, "--message-id", messageId, "--text", text,
                    "--channel", channel, "--sent-at", sentAt]
        if !name.isEmpty { args += ["--contact-name", name] }
        if let err = run(config.binPath, args) {
            alert("Couldn't record the reply.\n\n\(err)")
            return
        }
        // Regenerate so the open Pending Actions page moves the card to Wait on its next refresh.
        if let refresh = URL(string: "refreshpending://run") {
            NSWorkspace.shared.open(refresh)
        }
    }

    /// The reply as sent + when; nil on Cancel. Today → now; an earlier day → noon that day.
    private func confirm(name: String, draft: String) -> (String, String)? {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = "Record your reply to \(name.isEmpty ? "the recruiter" : name)"
        a.informativeText = "Make this match what you sent on LinkedIn (paste it if you changed it), and the day you sent it."
        a.addButton(withTitle: "Save")
        a.addButton(withTitle: "Cancel")

        let box = NSView(frame: NSRect(x: 0, y: 0, width: 440, height: 250))
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 30, width: 440, height: 220))
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        let tv = NSTextView(frame: scroll.bounds)
        tv.isRichText = false
        tv.font = NSFont.systemFont(ofSize: 13)
        tv.string = draft
        tv.autoresizingMask = [.width]
        tv.isVerticallyResizable = true
        scroll.documentView = tv
        box.addSubview(scroll)

        let label = NSTextField(labelWithString: "Sent on:")
        label.frame = NSRect(x: 0, y: 4, width: 60, height: 20)
        box.addSubview(label)
        let picker = NSDatePicker(frame: NSRect(x: 64, y: 0, width: 140, height: 24))
        picker.datePickerElements = .yearMonthDay
        picker.datePickerStyle = .textFieldAndStepper
        picker.dateValue = Date()
        picker.maxDate = Date()
        box.addSubview(picker)

        a.accessoryView = box
        a.window.initialFirstResponder = tv
        guard a.runModal() == .alertFirstButtonReturn else { return nil }
        let text = tv.string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }

        let cal = Calendar.current
        var when = Date()
        if !cal.isDateInToday(picker.dateValue) {
            when = cal.date(bySettingHour: 12, minute: 0, second: 0, of: picker.dateValue) ?? picker.dateValue
        }
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime]
        return (text, iso.string(from: when))
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
        let binPath: String
        let dbPath: String
    }

    private func loadConfig() -> Config? {
        guard let url = Bundle.main.url(forResource: "config", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: String],
              let bin = obj["binPath"], let db = obj["dbPath"]
        else { return nil }
        return Config(binPath: bin, dbPath: db)
    }

    private func alert(_ message: String) {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = "Record reply"
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
