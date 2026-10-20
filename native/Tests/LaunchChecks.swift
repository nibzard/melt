import AppKit

@main struct LaunchChecks {
    static func main() throws {
        _ = NSApplication.shared
        guard CommandLine.arguments.count == 3 else { throw HostError.message("Pass the application bundle and saved launcher.") }
        let bundle = URL(fileURLWithPath: CommandLine.arguments[1]).standardizedFileURL
        let file = URL(fileURLWithPath: CommandLine.arguments[2]).standardizedFileURL
        let descriptor = try LocalApp(Data(contentsOf: file))
        var application: NSRunningApplication?
        defer {
            if let app = application, !app.isTerminated { app.forceTerminate() }
        }
        func wait(_ message: String, until condition: () -> Bool) throws {
            let deadline = Date().addingTimeInterval(30)
            while !condition(), Date() < deadline {
                RunLoop.current.run(until: Date().addingTimeInterval(0.05))
            }
            guard condition() else { throw HostError.message("Timed out: \(message)") }
        }
        func services() -> [(pid: Int32, parent: Int32)] {
            let process = Process(), pipe = Pipe()
            process.executableURL = URL(fileURLWithPath: "/bin/ps")
            process.arguments = ["-ax", "-o", "pid=,ppid=,args="]
            process.standardOutput = pipe
            do { try process.run() } catch { return [] }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            return String(decoding: data, as: UTF8.self).split(separator: "\n").compactMap { line in
                let parts = line.split(separator: " ", maxSplits: 2, omittingEmptySubsequences: true)
                guard parts.count == 3, parts[2].contains("-m runtime.desktop"),
                      parts[2].contains(descriptor.root), parts[2].contains("--workspace " + descriptor.workspace),
                      let pid = Int32(parts[0]), let parent = Int32(parts[1]) else { return nil }
                return (pid, parent)
            }
        }
        func ownsWorkspace(_ parent: Int32) -> Bool {
            let lock = URL(fileURLWithPath: descriptor.root).appendingPathComponent("melt")
                .appendingPathComponent("workspaces").appendingPathComponent(descriptor.workspace).appendingPathComponent("owner.lock")
            guard let data = try? Data(contentsOf: lock),
                  let holder = try? JSONSerialization.jsonObject(with: data),
                  let pid = object(holder)["pid"] as? Int32 else { return false }
            return services().contains { $0.pid == pid && $0.parent == parent }
        }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = false
        var launchError: Error?
        NSWorkspace.shared.open([file], withApplicationAt: bundle, configuration: configuration) { app, error in
            application = app; launchError = error
        }
        try wait("application launch callback") { application != nil || launchError != nil }
        if let error = launchError { throw error }
        guard let first = application else { throw HostError.message("LaunchServices returned no application.") }
        precondition(first.bundleURL?.standardizedFileURL == bundle)
        try wait("saved workspace opened through LaunchServices") { ownsWorkspace(first.processIdentifier) }
        guard first.terminate() else { throw HostError.message("The native application refused its quit request.") }
        try wait("native quit and service shutdown") { first.isTerminated && services().isEmpty }

        application = nil
        if ProcessInfo.processInfo.environment["MELT_EXPLICIT_REOPEN"] == "1" {
            NSWorkspace.shared.open([file], withApplicationAt: bundle, configuration: configuration) { app, error in
                application = app; launchError = error
            }
        } else {
            guard NSWorkspace.shared.open(file) else { throw HostError.message("The saved file has no default application.") }
        }
        try wait("default file handler launch") {
            application = NSRunningApplication.runningApplications(withBundleIdentifier: "org.melt.localhost").first(where: { !$0.isTerminated })
            return application != nil
        }
        guard let second = application else { throw HostError.message("The default file handler did not launch Melt.") }
        if ProcessInfo.processInfo.environment["MELT_EXPLICIT_REOPEN"] == "1" {
            precondition(second.bundleURL?.standardizedFileURL == bundle)
        }
        try wait("desktop launcher reopened its workspace") { ownsWorkspace(second.processIdentifier) }
        guard second.terminate() else { throw HostError.message("The reopened application refused its quit request.") }
        try wait("reopened application shutdown") { second.isTerminated && services().isEmpty }
        let handler = ProcessInfo.processInfo.environment["MELT_EXPLICIT_REOPEN"] == "1" ? "the relocated application" : "the default file handler"
        print("LaunchServices opens the saved app, quits cleanly, and reopens it through \(handler).")
    }
}
