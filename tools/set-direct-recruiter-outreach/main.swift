import AppKit
import Foundation

/// URL-scheme helper: `setdro://set?key=<normalized_key>&value=<yes|no|undecided>`
/// Shells out to `set-direct-recruiter-outreach` (job_tracker.cli) to persist
/// a lead's direct_recruiter_outreach tri-state directly from
/// pending-actions.html's inline <select> (browsers cannot write to sqlite
/// from a static file:// page). Silent on success; shows a native alert on
/// failure (bad key, missing --value, DB locked, etc.) since the page
/// can't get a return value back from a fire-and-forget custom-scheme call.

private let scheme = "setdro"

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
            guard let self, !self.didHandleURL else { return }
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
            alert("SetDirectRecruiterOutreach is missing its config (re-run tools/set-direct-recruiter-outreach/install.sh).")
            return
        }

        let queryItems = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> String? {
            queryItems.first(where: { $0.name == name })?.value
        }

        guard let key = value("key"), !key.isEmpty else {
            alert("setdro:// call is missing ?key=")
            return
        }
        guard let val = value("value"), ["yes", "no", "undecided"].contains(val) else {
            alert("setdro:// call has a missing/invalid ?value= (must be yes, no, or undecided)")
            return
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: config.binPath)
        proc.arguments = ["--db", config.dbPath, "--key", key, "--value", val]
        let errPipe = Pipe()
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = errPipe

        do {
            try proc.run()
            proc.waitUntilExit()
        } catch {
            alert("Failed to launch set-direct-recruiter-outreach:\n\(error.localizedDescription)")
            return
        }

        if proc.terminationStatus != 0 {
            let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
            let errText = String(data: errData, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            alert("set-direct-recruiter-outreach failed (exit \(proc.terminationStatus)).\n\(errText)")
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
        alert.messageText = "Set Direct Recruiter Outreach"
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
