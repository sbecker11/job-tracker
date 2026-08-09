import AppKit
import Foundation

/// URL-scheme helper: `mps://mark?key=<normalized_key>&channel=email|linkedin`
/// Shells out to `mark-package-sent` so pending-actions can persist a
/// "Mark sent" click (static/React pages cannot write sqlite).

private let scheme = "mps"

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
            alert("MarkPackageSent is missing its config (re-run tools/mark-package-sent/install.sh).")
            return
        }

        let queryItems = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> String? {
            queryItems.first(where: { $0.name == name })?.value
        }

        guard let key = value("key"), !key.isEmpty else {
            alert("mps://mark requires ?key=<normalized_key>")
            return
        }
        let channel = value("channel") ?? "email"

        let args = ["--db", config.dbPath, "--key", key, "--channel", channel]
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
            alert("Failed to launch mark-package-sent:\n\(error.localizedDescription)")
            return
        }

        if proc.terminationStatus != 0 {
            let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
            let errText = String(data: errData, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            alert("mark-package-sent failed (exit \(proc.terminationStatus)).\n\(errText)")
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
        let a = NSAlert()
        a.messageText = "Mark package sent"
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
