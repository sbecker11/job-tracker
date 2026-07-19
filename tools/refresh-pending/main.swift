import AppKit
import Foundation

/// URL-scheme helper: `refreshpending://run` (optional `?no_rescore=1`)
/// Runs job-tracker/scripts/render_pending_actions.py, then reopens the
/// generated HTML so the browser picks up the new snapshot.

private let scheme = "refreshpending"

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
            alert("RefreshPending is missing its config (re-run tools/refresh-pending/install.sh).")
            return
        }

        let queryItems = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func flag(_ name: String) -> Bool {
            queryItems.contains { item in
                guard item.name == name else { return false }
                let v = (item.value ?? "1").lowercased()
                return v == "1" || v == "true" || v == "yes"
            }
        }
        let noRescore = flag("no_rescore")
        // Added so the page's own "Regenerate page" button (see
        // render_pending_actions.py's regen-btn JS) can reload the SAME
        // browser tab itself (location.reload with a cache-busting query
        // string) instead of this helper also calling NSWorkspace.open,
        // which was spawning a second browser window/tab on every click —
        // the exact "why does this open a new window" complaint this was
        // added to fix (2026-07-19). The `open 'refreshpending://run'`
        // terminal smoke test (no page involved) still wants the open-a-
        // browser behavior, so it stays the default; only the in-page
        // button passes no_open=1.
        let noOpen = flag("no_open")

        var args = [config.scriptPath]
        if noRescore { args.append("--no-rescore") }

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
            alert("Failed to launch renderer:\n\(error.localizedDescription)")
            return
        }

        if proc.terminationStatus != 0 {
            let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
            let errText = String(data: errData, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            alert("render_pending_actions.py failed (exit \(proc.terminationStatus)).\n\(errText)")
            return
        }

        // Re-open the HTML so the browser shows the fresh snapshot — skipped
        // when the page's own button already asked for no_open=1, since
        // that button reloads its own tab once this process exits instead.
        if !noOpen {
            NSWorkspace.shared.open(URL(fileURLWithPath: config.htmlPath))
        }
    }

    private struct Config {
        let repoRoot: String
        let pythonPath: String
        let scriptPath: String
        let htmlPath: String
    }

    private func loadConfig() -> Config? {
        let bundle = Bundle.main
        guard let url = bundle.url(forResource: "config", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: String],
              let repoRoot = obj["repoRoot"],
              let pythonPath = obj["pythonPath"],
              let scriptPath = obj["scriptPath"],
              let htmlPath = obj["htmlPath"]
        else { return nil }
        return Config(
            repoRoot: repoRoot,
            pythonPath: pythonPath,
            scriptPath: scriptPath,
            htmlPath: htmlPath
        )
    }

    private func alert(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Refresh Pending Actions"
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
