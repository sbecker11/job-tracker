import AppKit
import Foundation

/// Tiny URL-scheme helper: `revealfolder://reveal?path=/absolute/or/~/path`
/// Opens that directory in Finder, then quits. Used by pending-actions.html
/// company links (browsers cannot open Finder from a static page alone).

private let scheme = "revealfolder"

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var didHandleURL = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleGetURLEvent(_:withReplyEvent:)),
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )

        // Also accept the URL as a CLI arg (useful for install smoke-tests).
        let args = CommandLine.arguments.dropFirst()
        for arg in args {
            if let url = URL(string: arg), url.scheme?.lowercased() == scheme {
                handle(url)
                didHandleURL = true
            }
        }

        // Cold launch with no URL (e.g. first open after install): register
        // with Launch Services, then exit. Don't leave a dock icon hanging.
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
        guard let path = extractPath(from: url) else {
            fputs("revealfolder: missing or invalid ?path= query\n", stderr)
            return
        }
        let expanded = (path as NSString).expandingTildeInPath
        let resolved = URL(fileURLWithPath: expanded).standardizedFileURL.path

        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: resolved, isDirectory: &isDir),
              isDir.boolValue
        else {
            fputs("revealfolder: not a directory: \(resolved)\n", stderr)
            return
        }

        NSWorkspace.shared.open(URL(fileURLWithPath: resolved, isDirectory: true))
    }

    private func extractPath(from url: URL) -> String? {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            return nil
        }
        if let item = components.queryItems?.first(where: { $0.name == "path" }),
           let value = item.value, !value.isEmpty
        {
            return value
        }
        // Fallback: revealfolder:///Users/you/Desktop/foo
        let path = components.path
        return path.isEmpty || path == "/" ? nil : path
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
