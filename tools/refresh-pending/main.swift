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

        let noRescore: Bool = {
            guard let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems
            else { return false }
            return items.contains { item in
                guard item.name == "no_rescore" else { return false }
                let v = (item.value ?? "1").lowercased()
                return v == "1" || v == "true" || v == "yes"
            }
        }()

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

        // Re-open the HTML so the browser shows the fresh snapshot.
        // Cmd-R also works if the same tab is already frontmost.
        NSWorkspace.shared.open(URL(fileURLWithPath: config.htmlPath))
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
