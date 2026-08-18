import AppKit
import Foundation

/// URL-scheme helper: `triageimap://run`
/// Runs job-tracker/scripts/triage_imap_now.py (full triage of the Spexture
/// IMAP mailbox + pending-actions refresh) so a message just sent to
/// shawn.becker@spexture.com gets its real verdict/package immediately,
/// instead of waiting for the next 3-minute or hourly automated tick.

private let scheme = "triageimap"

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
            alert("TriageImap is missing its config (re-run tools/triage-imap-now/install.sh).")
            return
        }

        // The React tab polls pending-actions.json itself and jumps to the
        // new lead once it appears — this helper only needs to run the
        // script and report a failure, never open a browser tab itself.
        let args = [
            config.scriptPath,
            "--wait-lock-seconds", "90",
        ]

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: config.pythonPath)
        proc.arguments = args
        proc.currentDirectoryURL = URL(fileURLWithPath: config.repoRoot)
        let errPipe = Pipe()
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = errPipe

        do {
            try proc.run()
            proc.waitUntilExit()
        } catch {
            alert("Failed to launch triage_imap_now.py:\n\(error.localizedDescription)")
            return
        }

        if proc.terminationStatus == 75 {
            alert(
                "The 3-minute mailbox tick is already running and didn't finish in time.\n"
                    + "Wait a few seconds and click Check inbox now again."
            )
            return
        }

        if proc.terminationStatus != 0 {
            let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
            let errText = String(data: errData, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            alert("triage_imap_now.py failed (exit \(proc.terminationStatus)).\n\(errText)")
            return
        }
    }

    private struct Config {
        let repoRoot: String
        let pythonPath: String
        let scriptPath: String
    }

    private func loadConfig() -> Config? {
        let bundle = Bundle.main
        guard let url = bundle.url(forResource: "config", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: String],
              let repoRoot = obj["repoRoot"],
              let pythonPath = obj["pythonPath"],
              let scriptPath = obj["scriptPath"]
        else { return nil }
        return Config(
            repoRoot: repoRoot,
            pythonPath: pythonPath,
            scriptPath: scriptPath
        )
    }

    private func alert(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Check Inbox Now"
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
