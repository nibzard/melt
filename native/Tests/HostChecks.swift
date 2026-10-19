import AppKit

@main struct HostChecks {
    static func main() throws {
        _ = NSApplication.shared
        let descriptor = Data(#"{"format":"melt-launcher/v1","root":"/tmp/data with spaces","workspaceId":"little-hours"}"#.utf8)
        let app = try LocalApp(descriptor)
        let reopened = try LocalApp(app.data)
        precondition(reopened.root == "/tmp/data with spaces")
        let invalid = Data(#"{"format":"melt-launcher/v1","root":"relative","workspaceId":"../other"}"#.utf8)
        do { _ = try LocalApp(invalid); fatalError("An invalid launcher was accepted") } catch {}
        let pane = Pane(identity: "daily", host: nil)
        let definition: Object = ["title": "Daily results", "kind": "table"]
        let plan: Object = ["renderer": ["id": "melt.table", "contractVersion": 1], "controls": []]
        let first: Object = ["status": "ready", "table": ["columns": ["day", "profit"], "rows": [["Mon", "28"]]]]
        pane.update(definition: definition, plan: plan, state: [:], data: first)
        let view = pane.view
        func findTable(_ view: NSView) -> NSTableView? {
            if let table = view as? NSTableView { return table }
            for child in view.subviews { if let found = findTable(child) { return found } }
            return nil
        }
        guard let table = findTable(view) else { fatalError("The table is missing") }
        pane.update(definition: definition, plan: plan, state: [:], data: ["status": "ready", "table": ["columns": ["day", "profit"], "rows": [["Mon", "30"], ["Tue", "50"]]]])
        precondition(pane.view === view)
        precondition(findTable(view) === table)
        precondition(table.numberOfRows == 2)
        pane.update(definition: definition, plan: plan, state: [:], data: ["status": "unavailable", "message": "Missing artifact"])
        pane.update(definition: definition, plan: plan, state: [:], data: first)
        precondition(findTable(pane.view)?.numberOfRows == 1)
        print("Native launcher and pane continuity checks passed.")
        guard CommandLine.arguments.count == 3 else {
            throw HostError.message("Pass the café launcher and runtime configuration to run the native workflow checks.")
        }
        let source = URL(fileURLWithPath: CommandLine.arguments[1])
        let configuration = object(try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))))
        let cafe = try LocalApp(Data(contentsOf: source))

        func wait(_ message: String, until condition: () -> Bool) throws {
            let deadline = Date().addingTimeInterval(30)
            while !condition(), Date() < deadline {
                RunLoop.current.run(until: Date().addingTimeInterval(0.02))
            }
            guard condition() else { throw HostError.message("Timed out: \(message)") }
        }
        func parameters(_ host: Workspace) -> Object {
            object(object(host.currentView["snapshot"])["parameterStates"])
        }
        func summary(_ host: Workspace) -> String {
            string(object(object(host.currentView["panes"])["summary"])["text"])
        }

        let firstHost = try Workspace(app: cafe, configuration: configuration)
        firstHost.showWindow(nil)
        try wait("initial café restoration") { firstHost.isReady && summary(firstHost).contains("€2,836.00") }
        firstHost.setParameter("bean_increase", value: 0)
        try wait("bean cost acknowledgement") { firstHost.isReady && object(parameters(firstHost)["bean_increase"])["value"] as? Int == 0 }
        firstHost.setParameter("open_mondays", value: true)
        try wait("Monday acknowledgement") { firstHost.isReady && object(parameters(firstHost)["open_mondays"])["value"] as? Bool == true }
        firstHost.activate(projection: "summary", control: "run")
        try wait("native scenario publication") { firstHost.isReady && summary(firstHost).contains("€3,280.00") }
        let savedView = firstHost.currentView
        let desktop = source.deletingLastPathComponent().appendingPathComponent("Desktop", isDirectory: true)
        try FileManager.default.createDirectory(at: desktop, withIntermediateDirectories: true)
        let savedFile = desktop.appendingPathComponent("Little Hours.meltapp")
        try firstHost.saveLauncher(to: savedFile)
        firstHost.close()
        try wait("first host shutdown") { firstHost.serviceClosed }

        let savedApp = try LocalApp(Data(contentsOf: savedFile))
        let secondHost = try Workspace(app: savedApp, configuration: configuration)
        secondHost.showWindow(nil)
        try wait("saved café restoration") { secondHost.isReady && summary(secondHost).contains("€3,280.00") }
        let reopenedView = secondHost.currentView
        for key in ["document", "parameterStates", "outputGroups"] {
            precondition(jsonSignature(object(savedView["snapshot"])[key] ?? [:]) == jsonSignature(object(reopenedView["snapshot"])[key] ?? [:]), "Changed during restoration: \(key)")
        }
        precondition(jsonSignature(savedView["plan"] ?? [:]) == jsonSignature(reopenedView["plan"] ?? [:]))
        precondition(jsonSignature(savedView["panes"] ?? [:]) == jsonSignature(reopenedView["panes"] ?? [:]))
        secondHost.close()
        try wait("reopened host shutdown") { secondHost.serviceClosed }
        print("Native café edit, run, save, close, and reopen checks passed.")
    }
}
