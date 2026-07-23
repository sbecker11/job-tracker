import AppKit
import Foundation

/// URL-scheme helper: `viewcomms://open?company=<enc>&title=<enc>`
/// Shells out to the `export-communications` console script to render this
/// lead's full job_conversations history to a fresh PDF, then opens it —
/// browsers cannot query sqlite or open a PDF from a static file:// page.
/// Used by pending-actions.html's per-title communications-count badge
/// (see titleCellHtml()/commsUrl() in scripts/render_pending_actions.py).

private let scheme = "viewcomms"

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
            alert("ViewCommunications is missing its config (re-run tools/view-communications/install.sh).")
            return
        }

        let queryItems = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> String? {
            queryItems.first(where: { $0.name == name })?.value
        }

        guard let company = value("company"), !company.isEmpty else {
            alert("viewcomms:// call is missing ?company=")
            return
        }
        guard let title = value("title"), !title.isEmpty else {
            alert("viewcomms:// call is missing ?title=")
            return
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: config.binPath)
        proc.arguments = ["--db", config.dbPath, "--company", company, "--title", title]
        let outPipe = Pipe()
        let errPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = errPipe

        do {
            try proc.run()
            proc.waitUntilExit()
        } catch {
            alert("Failed to launch export-communications:\n\(error.localizedDescription)")
            return
        }

        let outData = outPipe.fileHandleForReading.readDataToEndOfFile()
        let outText = String(data: outData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""

        if proc.terminationStatus != 0 {
            let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
            let errText = String(data: errData, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            alert("export-communications failed (exit \(proc.terminationStatus)).\n\(errText)")
            return
        }

        if let pdfPath = extractPdfPath(from: outText) {
            NSWorkspace.shared.open(URL(fileURLWithPath: pdfPath))
        } else {
            // "No conversations logged yet ... — nothing to export." (or an
            // unrecognized message shape) — surface whatever the CLI said
            // rather than silently doing nothing.
            alert(outText.isEmpty ? "export-communications produced no output." : outText)
        }
    }

    /// export_communications.py's success line is fixed:
    /// "Exported N conversation(s) to <path>" — everything after the last
    /// "conversation(s) to " marker is the path, straight through to EOF.
    private func extractPdfPath(from output: String) -> String? {
        guard let marker = output.range(of: "conversation(s) to ", options: .backwards) else {
            return nil
        }
        let path = String(output[marker.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
        return path.isEmpty ? nil : path
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
        alert.messageText = "View Communications"
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
