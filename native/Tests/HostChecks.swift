import AppKit

@main struct HostChecks {
    static func main() throws {
        setbuf(stdout, nil)
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
        let markdown = MarkdownDocument.render("# Heading\n\n**Bold** [safe](https://example.com) [unsafe](javascript:evil) ![remote](https://example.com/image.png)")
        precondition(markdown.string.hasPrefix("Heading\n\nBold"))
        markdown.enumerateAttributes(in: NSRange(location: 0, length: markdown.length)) { attributes, _, _ in
            precondition(attributes[.attachment] == nil)
            if let link = attributes[.link] as? URL { precondition(link.scheme == "https") }
            precondition(attributes[NSAttributedString.Key("NSImageURL")] == nil)
        }
        let copyTable = DataTable()
        copyTable.update(["columns": ["name", "value"], "rows": [["A", "1"], ["B", "2"]], "offset": 100, "hasMore": true, "nextOffset": 102])
        copyTable.table.selectRowIndexes(IndexSet([0, 1]), byExtendingSelection: false)
        precondition(copyTable.copyText() == "A\t1\nB\t2")
        var nextOffset: Int?
        copyTable.query = { _, _, offset in nextOffset = offset }
        copyTable.next.performClick(nil)
        precondition(nextOffset == 102)
        copyTable.update(["columns": ["name"], "rows": [["A"], ["B"]], "rowKeys": ["key-a", "key-b"],
            "selectionMode": "single", "selectedKey": "key-b", "offset": 0, "hasMore": false])
        precondition(copyTable.table.selectedRow == 1)
        var selectedKey: String?
        copyTable.select = { selectedKey = $0 }
        copyTable.table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        precondition(selectedKey == "key-a")
        guard CommandLine.arguments.count == 6 else {
            throw HostError.message("Pass the café launcher, runtime configuration, artifact directory, linked-views launcher, and sales launcher.")
        }
        let source = URL(fileURLWithPath: CommandLine.arguments[1])
        var configuration = object(try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))))
        configuration["resourceRoot"] = URL(fileURLWithPath: CommandLine.arguments[2]).deletingLastPathComponent().path
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
        let plot = PlotView()
        let plotData = object(object(firstHost.currentView["panes"])["profit"])
        plot.update(plotData, title: "Profit")
        try wait("validated plot decoding") { !plot.isRendering && plot.image.image != nil }
        let exportedPlot = artifacts.appendingPathComponent("exported-plot.png")
        try plot.export(to: exportedPlot)
        let exportedBytes = try Data(contentsOf: exportedPlot)
        let originalBytes = try Data(contentsOf: URL(fileURLWithPath: string(plotData["imagePath"])))
        precondition(exportedBytes == originalBytes)
        plot.scroll.frame = NSRect(x: 0, y: 0, width: 320, height: 200)
        plot.scroll.layoutSubtreeIfNeeded(); plot.fit()
        let fitScale = plot.scroll.magnification
        precondition(fitScale > 0 && fitScale < 1)
        plot.fit(); precondition(abs(plot.scroll.magnification - fitScale) < 0.001)
        plot.zoom(1.25); precondition(plot.scroll.magnification > fitScale)
        plot.fit(); precondition(abs(plot.scroll.magnification - fitScale) < 0.001)
        guard let content = firstHost.window?.contentView,
              let cost = control("bean_increase", in: content) as? NSTextField,
              let mondays = control("open_mondays", in: content) as? NSButton,
              let run = control("summary/run", in: content) as? NSButton else {
            throw HostError.message("The visible café controls are missing.")
        }
        let initialLayout = object(firstHost.workspaceDocument["layout"])
        setenv("OPENAI_API_KEY", "melt-synthetic-terminal-credential", 1)
        firstHost.toggleTerminal()
        guard let terminal = firstHost.terminal else { throw HostError.message("Ghostty terminal is missing") }
        let terminalMarker = artifacts.appendingPathComponent("terminal-ready")
        try? FileManager.default.removeItem(at: terminalMarker)
        let quotedMarker = "'" + terminalMarker.path.replacingOccurrences(of: "'", with: "'\\''") + "'"
        terminal.insertText("test -z \"${OPENAI_API_KEY+x}\" && printf MELT_TERMINAL_READY > " + quotedMarker,
                            replacementRange: NSRange(location: NSNotFound, length: 0))
        let enter = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
            windowNumber: firstHost.window?.windowNumber ?? 0, context: nil, characters: "\r", charactersIgnoringModifiers: "\r", isARepeat: false, keyCode: 36)!
        terminal.keyDown(with: enter)
        try wait("Ghostty keyboard input and credential-free shell") { FileManager.default.fileExists(atPath: terminalMarker.path) }
        let terminalPID = terminal.processID
        precondition(terminalPID > 0)
        let terminalSurface = terminal.surface
        terminal.setMarkedText("日本", selectedRange: NSRange(location: 1, length: 0), replacementRange: NSRange(location: NSNotFound, length: 0))
        precondition(terminal.hasMarkedText() && terminal.selectedRange().location == 1)
        terminal.unmarkText(); precondition(!terminal.hasMarkedText())
        precondition(terminal.accessibilityLabel() == "Ghostty terminal")
        precondition(terminal.accessibilityRole() == .textArea)
        try wait("accessible Ghostty viewport") { terminal.screenText().contains("MELT_TERMINAL_READY") }
        try capture(firstHost, name: "ghostty-terminal")
        firstHost.toggleTerminal() // Keep the surface alive while hiding its host region.
        let daily = firstHost.paneController("daily")!
        let initialTable = findTable(daily.view)!
        firstHost.window?.makeFirstResponder(run)
        (control("daily/move", in: content) as? NSButton)?.performClick(nil)
        try wait("move pane acknowledgement") {
            firstHost.isReady && string(object(object(firstHost.workspaceDocument["layout"])["root"])["kind"]) == "split" &&
            string(object(object(object(firstHost.workspaceDocument["layout"])["root"])["first"])["projection"]) == "daily"
        }
        precondition(firstHost.paneController("daily") === daily && findTable(daily.view) === initialTable)
        precondition(firstHost.terminal === terminal && terminal.surface == terminalSurface)
        precondition(firstHost.window?.firstResponder === run)
        (control("daily/hide", in: content) as? NSButton)?.performClick(nil)
        try wait("hide pane acknowledgement") { firstHost.isReady && array(object(firstHost.workspaceDocument["layout"])["hidden"]).contains { $0 as? String == "daily" } }
        precondition(firstHost.paneController("daily") === daily)
        firstHost.showPane("daily")
        try wait("show pane acknowledgement") { firstHost.isReady && PaneLayout.leaves(object(object(firstHost.workspaceDocument["layout"])["root"])).contains("daily") }
        precondition(findTable(daily.view) === initialTable)
        let identities = object(firstHost.workspaceDocument["projections"]).keys.sorted()
        var wide: Object = ["kind": "pane", "projection": identities[0]]
        for id in identities.dropFirst() { wide = ["kind": "split", "axis": "horizontal", "ratio": 0.5, "first": wide, "second": ["kind": "pane", "projection": id]] }
        firstHost.window?.setContentSize(NSSize(width: 760, height: 520))
        firstHost.replaceLayout(["root": wide, "hidden": []])
        try wait("wide layout acknowledgement") { firstHost.isReady && firstHost.isOverflowing }
        let preservedLayout = jsonSignature(firstHost.workspaceDocument["layout"] ?? [:])
        firstHost.window?.setContentSize(NSSize(width: 1600, height: 900))
        try wait("large window restores saved tree") { !firstHost.isOverflowing }
        firstHost.window?.setContentSize(NSSize(width: 760, height: 520))
        try wait("small window selects overflow") { firstHost.isOverflowing }
        precondition(jsonSignature(firstHost.workspaceDocument["layout"] ?? [:]) == preservedLayout)
        precondition(firstHost.paneController("daily") === daily)
        firstHost.window?.setContentSize(NSSize(width: 1160, height: 760))
        firstHost.replaceLayout(initialLayout)
        try wait("original layout restored") { firstHost.isReady && jsonSignature(firstHost.workspaceDocument["layout"] ?? [:]) == jsonSignature(initialLayout) }
        var ancestor = daily.parent
        var rootSplit: LayoutSplit?
        while let controller = ancestor {
            if let split = controller as? LayoutSplit { rootSplit = split }
            ancestor = controller.parent
        }
        guard let divider = rootSplit else { throw HostError.message("The saved split controller is missing") }
        content.layoutSubtreeIfNeeded()
        divider.view.layoutSubtreeIfNeeded()
        let splitLength = divider.splitView.isVertical ? divider.splitView.bounds.width : divider.splitView.bounds.height
        divider.splitView.setPosition(splitLength * 0.6, ofDividerAt: 0)
        divider.commitDivider()
        try wait("revision-checked divider persistence") {
            firstHost.isReady && jsonSignature(firstHost.workspaceDocument["layout"] ?? [:]) != jsonSignature(initialLayout)
        }
        firstHost.replaceLayout(initialLayout)
        try wait("layout after divider check") { firstHost.isReady && jsonSignature(firstHost.workspaceDocument["layout"] ?? [:]) == jsonSignature(initialLayout) }
        guard let sortColumn = control("daily/table-column", in: content) as? NSPopUpButton,
              let sortDirection = control("daily/table-direction", in: content) as? NSPopUpButton,
              let sortButton = control("daily/table-sort", in: content) as? NSButton,
              let clearButton = control("daily/table-clear", in: content) as? NSButton else { throw HostError.message("Missing native table navigation") }
        sortColumn.selectItem(withTitle: "day"); sortDirection.selectItem(at: 1); sortButton.performClick(nil)
        try wait("native service-backed table sort") {
            let rows = array(object(object(object(firstHost.currentView["panes"])["daily"])["table"])["rows"])
            return firstHost.isReady && (rows.first as? [Any])?.first as? String == "wednesday"
        }
        clearButton.performClick(nil)
        try wait("native sort clear") {
            let pane = object(object(firstHost.currentView["panes"])["daily"])
            return firstHost.isReady && array(object(pane["viewSettings"])["sort"]).isEmpty
        }
        print("Native pane move/hide/show, focus, overflow, table navigation, Markdown, and Ghostty input checks passed.")
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
        var heartbeats: [Double] = []
        var previousHeartbeat = Date.timeIntervalSinceReferenceDate
        let heartbeat = Timer.scheduledTimer(withTimeInterval: 0.01, repeats: true) { _ in
            let now = Date.timeIntervalSinceReferenceDate
            heartbeats.append((now - previousHeartbeat) * 1000); previousHeartbeat = now
        }
        try wait("native scenario publication") { firstHost.isReady && summary(firstHost).contains("€3,280.00") }
        heartbeat.invalidate()
        var patchSamples: [Double] = []
        for index in 0..<25 {
            let title = index == 24 ? "Daily" : "Daily \(index)"
            var definition = object(object(firstHost.workspaceDocument["projections"])["daily"])
            definition["title"] = title
            let revision = object(object(firstHost.currentView["snapshot"])["entityRevisions"])["projections/daily"] ?? 0
            let start = Date.timeIntervalSinceReferenceDate
            firstHost.sendCommand(["method": "workspace.apply", "expected": ["projections/daily": revision],
                "operations": [["op": "entity.replace", "collection": "projections", "id": "daily", "value": definition]]])
            try wait("visible patch acknowledgement") { firstHost.isReady && firstHost.paneController("daily")?.displayedTitle == title }
            patchSamples.append((Date.timeIntervalSinceReferenceDate - start) * 1000)
        }
        let snapshot = object(firstHost.currentView["snapshot"])
        var notes = object(object(firstHost.workspaceDocument["projections"])["break-even"])
        // The café ID is break-even; keep the canonical renderer/source, so
        // adding a fifth view requires no new result or execution.
        if notes.isEmpty { notes = object(object(firstHost.workspaceDocument["projections"])["break_even"]) }
        if notes.isEmpty { notes = object(object(firstHost.workspaceDocument["projections"])["summary"]) }
        notes["title"] = "Saved notes"; notes["controls"] = []
        var fiveLayout = object(firstHost.workspaceDocument["layout"])
        fiveLayout["root"] = ["kind": "split", "axis": "horizontal", "ratio": 0.8,
            "first": object(fiveLayout["root"]), "second": ["kind": "pane", "projection": "saved-notes"]]
        firstHost.sendCommand(["method": "workspace.apply", "expected": ["projections/saved-notes": 0,
            "layout": object(snapshot["entityRevisions"])["layout"] ?? 0], "operations": [
                ["op": "entity.create", "collection": "projections", "id": "saved-notes", "value": notes],
                ["op": "layout.replace", "value": fiveLayout]]])
        try wait("five saved panes") { firstHost.isReady && object(firstHost.workspaceDocument["projections"]).count == 5 }
        let savedView = firstHost.currentView
        try capture(firstHost, name: "before-close")
        let desktop = source.deletingLastPathComponent().appendingPathComponent("Desktop", isDirectory: true)
        try FileManager.default.createDirectory(at: desktop, withIntermediateDirectories: true)
        let savedFile = desktop.appendingPathComponent("Little Hours.meltapp")
        try firstHost.saveLauncher(to: savedFile)
        firstHost.close()
        try wait("first host shutdown") { firstHost.serviceClosed }

        let savedApp = try LocalApp(Data(contentsOf: savedFile))
        let restoreStart = Date.timeIntervalSinceReferenceDate
        let secondHost = try Workspace(app: savedApp, configuration: configuration)
        secondHost.showWindow(nil)
        try wait("saved café restoration") { secondHost.isReady && summary(secondHost).contains("€3,280.00") }
        let restoreMs = (Date.timeIntervalSinceReferenceDate - restoreStart) * 1000
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
        let salesURL = URL(fileURLWithPath: CommandLine.arguments[5])
        let salesApp = try LocalApp(Data(contentsOf: salesURL))
        let salesHost = try Workspace(app: salesApp, configuration: configuration)
        salesHost.showWindow(nil)
        func salesRows(_ host: Workspace) -> Int {
            array(object(object(object(host.currentView["panes"])["sales"])["table"])["rows"]).count
        }
        try wait("unexecuted sales workspace") { salesHost.isReady && !salesHost.workspaceDocument.isEmpty }
        precondition(salesRows(salesHost) == 0)
        let registration = string(object(object(object(salesHost.workspaceDocument["actions"])["analyze-sales"])["implementation"])["registrationId"])
        // Exercise the explicit native permission endpoint; approval is never
        // an authored workspace operation or an implicit restoration effect.
        salesHost.send(["operation": "approve", "registrationId": registration])
        try wait("sales permission acknowledgement") { salesHost.isReady }
        guard let salesContent = salesHost.window?.contentView,
              let salesRun = control("revenue/run", in: salesContent) as? NSButton,
              let minimum = control("minimum", in: salesContent) as? NSTextField else {
            throw HostError.message("The native sales controls are missing")
        }
        salesRun.performClick(nil)
        try wait("three sales rows at minimum zero") { salesHost.isReady && salesRows(salesHost) == 3 }
        minimum.stringValue = "200"
        minimum.delegate?.controlTextDidEndEditing?(Notification(name: NSControl.textDidEndEditingNotification, object: minimum))
        try wait("live sales computation") {
            array(object(salesHost.currentView["snapshot"])["activeRuns"]).contains { string(object($0)["state"]) == "running" }
        }
        (control("revenue/move", in: salesContent) as? NSButton)?.performClick(nil)
        try wait("sales layout committed during execution") {
            salesHost.isReady && string(object(object(object(salesHost.workspaceDocument["layout"])["root"])["first"])["projection"]) == "revenue"
        }
        let salesLayout = jsonSignature(salesHost.workspaceDocument["layout"] ?? [:])
        try wait("two sales rows at minimum 200") { salesHost.isReady && salesRows(salesHost) == 2 }
        precondition(jsonSignature(salesHost.workspaceDocument["layout"] ?? [:]) == salesLayout)
        let salesSavedView = salesHost.currentView
        try capture(salesHost, name: "sales-native")
        salesHost.close(); try wait("sales shutdown") { salesHost.serviceClosed }
        let restoredSales = try Workspace(app: salesApp, configuration: configuration)
        restoredSales.showWindow(nil)
        try wait("offline sales restoration") { restoredSales.isReady && salesRows(restoredSales) == 2 }
        for key in ["document", "parameterStates", "outputGroups"] {
            precondition(jsonSignature(object(restoredSales.currentView["snapshot"])[key] ?? [:]) == jsonSignature(object(salesSavedView["snapshot"])[key] ?? [:]))
        }
        guard let restoredContent = restoredSales.window?.contentView,
              let repeatRun = control("revenue/run", in: restoredContent) as? NSButton else { throw HostError.message("The restored sales action is missing") }
        let oldSource = jsonSignature(object(object(restoredSales.currentView["panes"])["sales"])["source"] ?? [:])
        repeatRun.performClick(nil)
        try wait("explicit sales reinvocation") {
            restoredSales.isReady && salesRows(restoredSales) == 2 &&
            jsonSignature(object(object(restoredSales.currentView["panes"])["sales"])["source"] ?? [:]) != oldSource
        }
        restoredSales.close(); try wait("restored sales shutdown") { restoredSales.serviceClosed }
        print("Native sales hero: three rows to two, layout during execution, offline restore, explicit reinvocation passed.")
        func p95(_ values: [Double]) -> Double {
            let ordered = values.sorted()
            return ordered.isEmpty ? 0 : ordered[min(ordered.count - 1, Int(ceil(Double(ordered.count) * 0.95)) - 1)]
        }
        let measurements: Object = ["format": "melt-native-acceptance/v1",
            "build": "Swift -O; Ghostty ReleaseFast", "conditions": "warm filesystem; fresh service processes",
            "checks": ["pane-continuity", "move-hide-show", "focus", "overflow", "table-query-controls", "split-persistence", "plot-export-zoom",
                "clipboard-text", "marked-text-input", "accessibility", "markdown-policy", "ghostty-surface",
                "native-calculation", "five-pane-restoration", "linked-selection", "sales-hero"],
            "heartbeatSamplesMs": heartbeats, "heartbeatP95Ms": p95(heartbeats),
            "patchSamplesMs": patchSamples, "patchP95Ms": p95(patchSamples),
            "restoredPanes": 5, "restoreMs": restoreMs]
        try JSONSerialization.data(withJSONObject: measurements, options: [.prettyPrinted, .sortedKeys])
            .write(to: artifacts.appendingPathComponent("native-acceptance.json"))
    }
}
