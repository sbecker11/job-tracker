import AppKit
import Foundation

/// URL-scheme helper: `refreshpending://run`
///
/// Runs job-tracker/scripts/render_pending_actions.py.
/// Does **not** open a browser tab by default — the React UI
/// (http://127.0.0.1:3174/) and the static HTML page reload/poll themselves.
/// Pass `?open=1` only for a terminal smoke test that should open the HTML file.

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
            alert("RefreshPending is missing its config (re-run tools/refresh-pending/install.sh).")
            return
        }

        let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        let queryItems = components?.queryItems ?? []
        let pathLower = (components?.path ?? url.path).lowercased()
        let hostLower = (components?.host ?? "").lowercased()

        func flag(_ name: String) -> Bool {
            queryItems.contains { item in
                guard item.name == name else { return false }
                let v = (item.value ?? "1").lowercased()
                return v == "1" || v == "true" || v == "yes"
            }
        }
        // Also accept path/host tokens when Chrome/macOS drops the query string
        // from a custom-scheme iframe / location.href handoff.
        func pathFlag(_ name: String) -> Bool {
            pathLower.contains("/\(name)") || pathLower.hasSuffix("/\(name)") || hostLower == name
        }

        let noRescore = flag("no_rescore") || pathFlag("no_rescore")
        // Default: do NOT open a browser. Opening HTML via NSWorkspace was
        // spawning a second Chrome tab while the React UI tab stayed put.
        // Opt in with ?open=1 (or /open in the path) for terminal smoke tests.
        let wantOpen = flag("open") || pathFlag("open")
        let noOpen = flag("no_open") || pathFlag("no_open") || !wantOpen

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
