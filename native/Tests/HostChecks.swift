import AppKit

@main struct HostChecks {
    static func main() throws {
        _ = NSApplication.shared
        func control(_ id: String, in view: NSView) -> NSControl? {
            if let item = view as? NSControl, item.identifier?.rawValue == id { return item }
            for child in view.subviews { if let found = control(id, in: child) { return found } }
            return nil
        }
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
        let selectPane = Pane(identity: "currency", host: nil)
        let selectPlan: Object = ["renderer": ["id": "melt.document", "contractVersion": 1],
            "controls": [["id": "currency", "kind": "select", "label": "Currency", "parameter": "currency",
                "options": [["value": "eur", "label": "Euros"], ["value": "usd", "label": "US dollars"]]]]]
        let selectDefinition: Object = ["title": "Currency", "kind": "document"]
        selectPane.update(definition: selectDefinition, plan: selectPlan,
            state: ["currency": ["value": "usd", "revision": 1]], data: ["status": "ready", "text": ""])
        guard let popup = control("currency", in: selectPane.view) as? NSPopUpButton else { fatalError("The select control is missing") }
        precondition(popup.titleOfSelectedItem == "US dollars")
        precondition(popup.selectedItem?.representedObject as? String == "usd")
        selectPane.update(definition: selectDefinition, plan: selectPlan,
            state: ["currency": ["value": "eur", "revision": 2]], data: ["status": "ready", "text": ""])
        precondition(popup === control("currency", in: selectPane.view))
        precondition(popup.titleOfSelectedItem == "Euros")
        func matchingControls(_ id: String, in view: NSView) -> [NSControl] {
            let match = (view as? NSControl).flatMap { $0.identifier?.rawValue == id ? $0 : nil }
            return (match.map { [$0] } ?? []) + view.subviews.flatMap { matchingControls(id, in: $0) }
        }
        let duplicatePane = Pane(identity: "duplicates", host: nil)
        let duplicatePlan: Object = ["renderer": ["id": "melt.document", "contractVersion": 1],
            "controls": [["id": "first", "kind": "toggle", "label": "First", "parameter": "shared"],
                         ["id": "second", "kind": "toggle", "label": "Second", "parameter": "shared"]]]
        duplicatePane.update(definition: selectDefinition, plan: duplicatePlan,
            state: ["shared": ["value": true, "revision": 1]], data: ["status": "ready", "text": ""])
        let toggles = matchingControls("shared", in: duplicatePane.view).compactMap { $0 as? NSButton }
        precondition(toggles.count == 2 && toggles.allSatisfy { $0.state == .on })
        duplicatePane.update(definition: selectDefinition, plan: duplicatePlan,
            state: ["shared": ["value": false, "revision": 2]], data: ["status": "ready", "text": ""])
        precondition(toggles.allSatisfy { $0.state == .off })
        print("Native launcher and pane continuity checks passed.")
        guard CommandLine.arguments.count == 5 else {
            throw HostError.message("Pass the café launcher, runtime configuration, artifact directory, and linked-views launcher.")
        }
        let source = URL(fileURLWithPath: CommandLine.arguments[1])
        let configuration = object(try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))))
        let cafe = try LocalApp(Data(contentsOf: source))
        let artifacts = URL(fileURLWithPath: CommandLine.arguments[3], isDirectory: true)
        try FileManager.default.createDirectory(at: artifacts, withIntermediateDirectories: true)

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
        func capture(_ host: Workspace, name: String) throws {
            guard let content = host.window?.contentView else { throw HostError.message("Missing window content") }
            content.layoutSubtreeIfNeeded(); content.displayIfNeeded()
            guard let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds) else { throw HostError.message("Cannot capture the window") }
            content.cacheDisplay(in: content.bounds, to: bitmap)
            guard let png = bitmap.representation(using: .png, properties: [:]) else { throw HostError.message("Cannot encode the window capture") }
            try png.write(to: artifacts.appendingPathComponent(name + ".png"))
        }

        let firstHost = try Workspace(app: cafe, configuration: configuration)
        firstHost.showWindow(nil)
        try wait("initial café restoration") { firstHost.isReady && summary(firstHost).contains("€2,836.00") }
        guard let content = firstHost.window?.contentView,
              let cost = control("bean_increase", in: content) as? NSTextField,
              let mondays = control("open_mondays", in: content) as? NSButton,
              let run = control("summary/run", in: content) as? NSButton else {
            throw HostError.message("The visible café controls are missing.")
        }
        let sharedPane = Pane(identity: "shared-controls", host: firstHost)
        let sharedPlan: Object = ["renderer": ["id": "melt.document", "contractVersion": 1],
            "controls": [["id": "first", "kind": "toggle", "label": "First", "parameter": "open_mondays"],
                         ["id": "second", "kind": "toggle", "label": "Second", "parameter": "open_mondays"]]]
        sharedPane.update(definition: selectDefinition, plan: sharedPlan,
            state: parameters(firstHost), data: ["status": "ready", "text": ""])
        let sharedToggles = matchingControls("open_mondays", in: sharedPane.view).compactMap { $0 as? NSButton }
        precondition(sharedToggles.count == 2)
        // The saved value is false. The first toggle must send its own
        // new value while the second toggle still has the old value.
        sharedToggles[0].performClick(nil)
        try wait("first shared toggle acknowledgement") {
            firstHost.isReady && object(parameters(firstHost)["open_mondays"])["value"] as? Bool == true
        }
        firstHost.setParameter("open_mondays", value: false)
        try wait("restore Monday value") {
            firstHost.isReady && object(parameters(firstHost)["open_mondays"])["value"] as? Bool == false
        }
        cost.stringValue = "0"
        cost.delegate?.controlTextDidEndEditing?(Notification(name: NSControl.textDidEndEditingNotification, object: cost))
        try wait("bean cost acknowledgement") { firstHost.isReady && object(parameters(firstHost)["bean_increase"])["value"] as? Int == 0 }
        mondays.performClick(nil)
        try wait("Monday acknowledgement") { firstHost.isReady && object(parameters(firstHost)["open_mondays"])["value"] as? Bool == true }
        // Queue two edits during a poll. Capture each revision only when
        // its command reaches the service, then run with the final values.
        firstHost.send(["operation": "poll"])
        firstHost.setParameter("bean_increase", value: 10)
        firstHost.setParameter("bean_increase", value: 0)
        run.performClick(nil)
        try wait("native scenario publication") { firstHost.isReady && summary(firstHost).contains("€3,280.00") }
        let savedView = firstHost.currentView
        try capture(firstHost, name: "before-close")
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
        try capture(secondHost, name: "after-reopen")
        let previousRevision = object(parameters(secondHost)["bean_increase"])["revision"] as? Int ?? 0
        secondHost.send(["operation": "poll"])
        secondHost.setParameter("bean_increase", value: 10)
        secondHost.setParameter("bean_increase", value: 0)
        secondHost.close()
        try wait("reopened host shutdown") { secondHost.serviceClosed }
        let drainedHost = try Workspace(app: savedApp, configuration: configuration)
        try wait("queued edits survive window close") {
            let state = object(parameters(drainedHost)["bean_increase"])
            return drainedHost.isReady && state["value"] as? Int == 0 && state["revision"] as? Int == previousRevision + 2
        }
        drainedHost.close()
        try wait("drained host shutdown") { drainedHost.serviceClosed }
        print("Native café edit, run, save, close, and reopen checks passed.")

        let linkedApp = try LocalApp(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[4])))
        let linkedHost = try Workspace(app: linkedApp, configuration: configuration)
        linkedHost.showWindow(nil)
        func selectedSupplier(_ id: String) -> String {
            let pane = object(object(linkedHost.currentView["panes"])[id])
            let record = object(object(pane["observer"])["record"])
            return string(object(record["cells"])["supplier_id"])
        }
        try wait("linked views restoration") { linkedHost.isReady && selectedSupplier("inspector") == "S-73" }
        guard let linkedContent = linkedHost.window?.contentView,
              let supplier = control("network/s-42", in: linkedContent) as? NSButton else { throw HostError.message("The supplier selector is missing") }
        supplier.performClick(nil)
        try wait("native shared selection") {
            linkedHost.isReady && selectedSupplier("inspector") == "S-42" && selectedSupplier("risk-inspector") == "S-42"
        }
        try capture(linkedHost, name: "linked-views")
        linkedHost.close()
        try wait("linked views shutdown") { linkedHost.serviceClosed }
        print("Native selection updates both inspectors through the shared event contract.")
    }
}
