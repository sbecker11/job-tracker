import AppKit
import Foundation

/// URL-scheme helper: `dlr://dismiss?kind=lead|unmatched&key=...&message_id=...`
/// Shells out to `dismiss-linkedin-reply` so pending-actions.html can persist
/// a "Dismiss / marked replied" click (static file:// pages cannot write sqlite).

private let scheme = "dlr"

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var didHandleURL = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleGetURLEvent(_:withReplyEvent:)),
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )

        let args = CommandLine.arguments.dropFirst()
        for arg in args {
            if let url = URL(string: arg), url.scheme?.lowercased() == scheme {
                handle(url)
                didHandleURL = true
            }
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            // Explicit `self = self` for Swift < 5.8 (mini2 CommandLineTools = 5.3).
            guard let self = self, !self.didHandleURL else { return }
            NSApp.terminate(nil)
        }
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls where url.scheme?.lowercased() == scheme {
            handle(url)
            didHandleURL = true
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
        handle(url)
        didHandleURL = true
        NSApp.terminate(nil)
    }

    private func handle(_ url: URL) {
        guard let config = loadConfig() else {
            alert("DismissLinkedInReply is missing its config (re-run tools/dismiss-linkedin-reply/install.sh).")
            return
        }

        let queryItems = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> String? {
            queryItems.first(where: { $0.name == name })?.value
        }

        guard let kind = value("kind"), ["lead", "unmatched"].contains(kind) else {
            alert("dlr:// call needs ?kind=lead or ?kind=unmatched")
            return
        }
        let key = value("key") ?? ""
        let messageId = value("message_id") ?? ""
        if kind == "lead" && key.isEmpty {
            alert("dlr://dismiss?kind=lead requires &key=<normalized_key>")
            return
        }
        if kind == "unmatched" && messageId.isEmpty {
            alert("dlr://dismiss?kind=unmatched requires &message_id=<id>")
            return
        }

        var args = ["--db", config.dbPath, "--kind", kind]
        if !key.isEmpty { args += ["--key", key] }
        if !messageId.isEmpty { args += ["--message-id", messageId] }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: config.binPath)
        proc.arguments = args
        let errPipe = Pipe()
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = errPipe

        do {
            try proc.run()
            proc.waitUntilExit()
        } catch {
            alert("Failed to launch dismiss-linkedin-reply:\n\(error.localizedDescription)")
            return
        }

        if proc.terminationStatus != 0 {
            let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
            let errText = String(data: errData, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            alert("dismiss-linkedin-reply failed (exit \(proc.terminationStatus)).\n\(errText)")
        }
    }

    private struct Config {
        let binPath: String
        let dbPath: String
    }

    private func loadConfig() -> Config? {
        let bundle = Bundle.main
        guard let url = bundle.url(forResource: "config", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: String],
              let binPath = obj["binPath"],
              let dbPath = obj["dbPath"]
        else { return nil }
        return Config(binPath: binPath, dbPath: dbPath)
    }

    private func alert(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Dismiss LinkedIn Reply"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
