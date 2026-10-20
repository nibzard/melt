import AppKit

func jsonSignature(_ value: Any) -> String {
    guard JSONSerialization.isValidJSONObject(value),
          let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { return "" }
    return String(data: data, encoding: .utf8) ?? ""
}

final class LayoutSplit: NSSplitViewController {
    let axis: String, ratio: CGFloat
    private var positioned = false
    var saveRatio: ((Double) -> Void)?
    var firstMinimum: CGFloat = 240, secondMinimum: CGFloat = 240
    init(axis: String, ratio: CGFloat) {
        self.axis = axis; self.ratio = ratio
        super.init(nibName: nil, bundle: nil)
        splitView = DividerSplitView()
        (splitView as? DividerSplitView)?.finishedDragging = { [weak self] in self?.commitDivider() }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }
    override func viewDidLoad() {
        super.viewDidLoad(); splitView.isVertical = axis == "horizontal"
        splitView.dividerStyle = .thin
    }
    override func viewDidLayout() {
        super.viewDidLayout()
        if splitViewItems.count == 2 {
            let length = splitView.isVertical ? splitView.bounds.width : splitView.bounds.height
            let actual = splitView.subviews.first.map { splitView.isVertical ? $0.frame.width : $0.frame.height } ?? 0
            if length > 0 {
                let desired = positioned ? actual : length * ratio
                let position = min(max(firstMinimum, desired), max(firstMinimum, length - secondMinimum - splitView.dividerThickness))
                positioned = true
                if abs(position - actual) > 0.5 { splitView.setPosition(position, ofDividerAt: 0) }
            }
        }
    }
    func commitDivider() {
        guard splitViewItems.count == 2 else { return }
        let length = splitView.isVertical ? splitView.bounds.width : splitView.bounds.height
        let firstView = splitViewItems[0].viewController.view
        let first = splitView.isVertical ? firstView.frame.width : firstView.frame.height
        guard length > 0 else { return }
        let value = min(0.9, max(0.1, first / length))
        if abs(value - ratio) > 0.005 { saveRatio?(Double(value)) }
    }
}

final class Workspace: NSWindowController, NSWindowDelegate {
    let app: LocalApp
    private let bridge: Bridge
    private var timer: Timer?
    private var busy = false
    private var pendingRequests: [() -> Void] = []
    private var shutdownStarted = false
    var isReady: Bool { !busy && pendingRequests.isEmpty && !closing && !panes.values.contains { $0.isRendering } }
    private(set) var closing = false, serviceClosed = false
    var afterClose: (() -> Void)?
    private var snapshot: Object = [:], planned: Object = [:]
    private(set) var currentView: Object = [:]
    var workspaceDocument: Object { object(snapshot["document"]) }
    func paneController(_ id: String) -> Pane? { panes[id] }
    var isOverflowing: Bool { overflowMode }
    private var panes: [String: Pane] = [:]
    private var layoutSignature = ""
    private var rootController: NSViewController?
    private let container = LayoutContainer(), status = label("Opening workspace…", size: 11)
    private let layoutViewport = NSScrollView()
    private let overflow = NSPopUpButton(), hiddenPanes = NSPopUpButton()
    private let terminalBox = NSStackView()
    private(set) var terminal: GhosttyTerminal?
    private var overflowMode = false, arranging = false
    private var overflowSelection = ""

    init(app: LocalApp, configuration: Object) throws {
        let configuration = try resolvedRuntime(configuration)
        self.app = app; bridge = try Bridge(app: app, configuration: configuration)
        GhosttyEngine.resources = string(configuration["ghosttyResources"])
        GhosttyEngine.credentialNames = Set(array(configuration["credentialNames"]).compactMap { $0 as? String })
        GhosttyEngine.credentialSuffixes = array(configuration["credentialSuffixes"]).compactMap { $0 as? String }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1160, height: 760),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        super.init(window: window)
        window.contentView?.wantsLayer = true
        window.contentView?.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        window.delegate = self; window.title = app.workspace; window.minSize = NSSize(width: 760, height: 520)
        window.center(); window.isReleasedWhenClosed = false
        let root = NSStackView(); root.orientation = .vertical; root.spacing = 8
        let bar = NSStackView(); bar.orientation = .horizontal
        bar.addArrangedSubview(ClickButton("Save to Desktop…") { [weak self] in self?.saveLauncher() })
        bar.addArrangedSubview(ClickButton("Allow calculation…") { [weak self] in self?.approve() })
        bar.addArrangedSubview(ClickButton("Apply change…") { [weak self] in self?.applyChange() })
        bar.addArrangedSubview(ClickButton("Cancel run") { [weak self] in self?.cancelRun() })
        bar.addArrangedSubview(ClickButton("Refresh") { [weak self] in self?.send(["operation": "snapshot"]) })
        bar.addArrangedSubview(ClickButton("Terminal") { [weak self] in self?.toggleTerminal() })
        overflow.target = self; overflow.action = #selector(overflowChanged(_:))
        overflow.setAccessibilityLabel("Visible pane in a small window"); overflow.isHidden = true
        hiddenPanes.target = self; hiddenPanes.action = #selector(showHidden(_:))
        hiddenPanes.setAccessibilityLabel("Show a hidden pane"); hiddenPanes.isHidden = true
        bar.addArrangedSubview(hiddenPanes)
        root.addArrangedSubview(overflow)
        root.addArrangedSubview(bar); root.addArrangedSubview(status); root.addArrangedSubview(container)
        terminalBox.orientation = .vertical; terminalBox.isHidden = true
        root.addArrangedSubview(terminalBox)
        window.contentView?.addSubview(root); root.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 10),
            root.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -10),
            root.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 10),
            root.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor, constant: -10),
            container.widthAnchor.constraint(equalTo: root.widthAnchor),
            container.heightAnchor.constraint(greaterThanOrEqualToConstant: 160),
            terminalBox.widthAnchor.constraint(equalTo: root.widthAnchor),
            terminalBox.heightAnchor.constraint(greaterThanOrEqualToConstant: 160)
        ])
        layoutViewport.hasVerticalScroller = true; layoutViewport.hasHorizontalScroller = true
        layoutViewport.autoresizingMask = [.width, .height]
        layoutViewport.frame = container.bounds; container.addSubview(layoutViewport)
        container.resized = { [weak self] in
            guard let self = self else { return }
            self.layoutViewport.frame = self.container.bounds
            self.layoutViewport.documentView?.setFrameSize(self.layoutViewport.contentSize)
            self.arrangeLayout()
        }
        send(["operation": "snapshot"])
        timer = Timer.scheduledTimer(withTimeInterval: 0.7, repeats: true) { [weak self] _ in
            guard let self = self, !self.busy, !self.closing else { return }
            self.send(["operation": "poll"], quiet: true)
        }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }

    func send(_ message: Object, quiet: Bool = false, completion: ((Object) -> Void)? = nil) {
        sendWhenReady({ message }, quiet: quiet, completion: completion)
    }
    private func sendWhenReady(_ message: @escaping () -> Object, quiet: Bool = false,
                               completion: ((Object) -> Void)? = nil) {
        guard !closing else { return }
        pendingRequests.append { [weak self] in
            self?.performSend(message(), quiet: quiet, completion: completion)
        }
        sendNext()
    }
    private func sendNext() {
        guard !busy else { return }
        if !pendingRequests.isEmpty {
            pendingRequests.removeFirst()()
        } else if closing, !shutdownStarted {
            shutdownStarted = true
            bridge.close { [weak self] in
                self?.serviceClosed = true
                self?.afterClose?()
                self?.afterClose = nil
            }
        }
    }
    private func performSend(_ message: Object, quiet: Bool, completion: ((Object) -> Void)?) {
        busy = true
        if !quiet { status.textColor = .secondaryLabelColor; status.stringValue = "Working… Changes are not yet acknowledged." }
        var message = message
        if string(message["operation"]) == "propose" {
            message["activeEdits"] = panes.filter { $0.value.hasActiveEdit }.map { $0.key }
        }
        bridge.request(message) { [weak self] result in
            guard let self = self else { return }
            self.busy = false
            switch result {
            case .failure(let error): self.showError(error.localizedDescription)
            case .success(let response):
                if let view = response["view"] as? Object { self.update(view) }
                if response["ok"] as? Bool != true { self.showError(String(describing: response["errors"] ?? "The change failed.")) }
                completion?(response)
            }
            self.sendNext()
        }
    }
    func sendCommand(_ command: Object) {
        sendCommandWhenReady { command }
    }
    private func sendCommandWhenReady(_ command: @escaping () -> Object) {
        sendWhenReady({
            var message = command(); message["operation"] = "command"; message["requestId"] = UUID().uuidString
            return message
        }) { [weak self] response in
            if response["ok"] as? Bool == true { self?.send(["operation": "drive"]) }
        }
    }
    private func update(_ view: Object) {
        currentView = view
        snapshot = object(view["snapshot"]); planned = object(view["plan"])
        window?.title = string(workspaceDocument["title"])
        guard !planned.isEmpty else {
            showError("The saved workspace needs recovery: \(String(describing: view["planErrors"] ?? []))")
            return
        }
        let definitions = object(workspaceDocument["projections"]), entries = object(object(planned["render"])["projections"])
        let data = object(view["panes"]), states = object(snapshot["parameterStates"])
        var replacedController = false
        for (id, raw) in entries {
            if panes[id] == nil || panes[id]?.projectionKind != string(object(definitions[id])["kind"]) {
                panes[id] = Pane(identity: id, host: self); replacedController = true
            }
            panes[id]?.update(definition: object(definitions[id]), plan: object(raw), state: states, data: object(data[id]))
        }
        let layout = object(planned["layout"])
        hiddenPanes.removeAllItems(); hiddenPanes.addItem(withTitle: "Show pane…")
        for id in array(layout["hidden"]).compactMap({ $0 as? String }) {
            hiddenPanes.addItem(withTitle: string(object(definitions[id])["title"]))
            hiddenPanes.lastItem?.representedObject = id
        }
        hiddenPanes.isHidden = hiddenPanes.numberOfItems < 2
        arrangeLayout(force: replacedController)
        status.textColor = .secondaryLabelColor
        let active = array(snapshot["activeRuns"])
        let groups = object(snapshot["outputGroups"])
        let issues = groups.values.map(object).filter { string($0["status"]) != "current" }
        let details = issues.map { "\(string($0["status"]))\(($0["resultIsStale"] as? Bool == true) ? " · saved result uses earlier inputs" : "")" }
        status.stringValue = active.isEmpty ? (details.isEmpty ? "Saved on this Mac" : details.joined(separator: "; ")) : "Calculation queued or running · previous results remain visible"
        if !array(snapshot["diagnostics"]).isEmpty { status.stringValue += " · recovery diagnostics are available" }
    }
    private func arrangeLayout(force: Bool = false) {
        guard !arranging, !planned.isEmpty, container.bounds.width > 0, container.bounds.height > 0 else { return }
        if let editor = window?.firstResponder as? NSTextView, editor.hasMarkedText() { return }
        let layout = object(planned["layout"]), node = object(layout["root"])
        let minimum = PaneLayout.minimum(node), leaves = PaneLayout.leaves(node)
        let compact = container.bounds.width < minimum.width || container.bounds.height < minimum.height
        let signature = jsonSignature(layout)
        guard force || signature != layoutSignature || compact != overflowMode else { return }
        arranging = true; defer { arranging = false }
        let responder = window?.firstResponder
        let editor = (responder as? NSTextView)?.delegate as? NSView
        layoutViewport.documentView = nil
        PaneLayout.detach(rootController); rootController = nil
        for pane in panes.values { pane.view.removeFromSuperview(); pane.removeFromParent() }
        let entries = object(object(planned["render"])["projections"])
        panes = panes.filter { entries[$0.key] != nil }
        overflowMode = compact; overflow.isHidden = !compact || leaves.isEmpty
        overflow.removeAllItems()
        for id in leaves {
            overflow.addItem(withTitle: string(object(object(workspaceDocument["projections"])[id])["title"]))
            overflow.lastItem?.representedObject = id
        }
        if !leaves.contains(overflowSelection) { overflowSelection = leaves.first ?? "" }
        if let item = overflow.itemArray.first(where: { $0.representedObject as? String == overflowSelection }) { overflow.select(item) }
        let controller = compact ? panes[overflowSelection] : makeLayout(node)
        if let controller = controller {
            rootController = controller
            // A viewport separates window minimums from the split subtree's
            // intrinsic constraints, so a large saved tree cannot force the
            // window to stay large and bypass the overflow selector.
            controller.view.translatesAutoresizingMaskIntoConstraints = true
            controller.view.autoresizingMask = [.width, .height]
            controller.view.frame = NSRect(origin: .zero, size: layoutViewport.contentSize)
            layoutViewport.documentView = controller.view
        }
        layoutSignature = signature
        if let focus = editor ?? responder as? NSView, focus.isDescendant(of: container) { window?.makeFirstResponder(focus) }
    }
    @objc private func overflowChanged(_ sender: NSPopUpButton) {
        overflowSelection = string(sender.selectedItem?.representedObject); arrangeLayout(force: true)
    }
    @objc private func showHidden(_ sender: NSPopUpButton) {
        guard let id = sender.selectedItem?.representedObject as? String else { return }
        showPane(id)
    }
    func hidePane(_ id: String) {
        var layout = object(workspaceDocument["layout"])
        guard PaneLayout.leaves(object(layout["root"])).contains(id) else { return }
        layout["root"] = PaneLayout.removing(id, from: object(layout["root"])) ?? NSNull()
        layout["hidden"] = array(layout["hidden"]) + [id]
        replaceLayout(layout)
    }
    func movePaneFirst(_ id: String) {
        var layout = object(workspaceDocument["layout"])
        guard let remaining = PaneLayout.removing(id, from: object(layout["root"])) else { return }
        layout["root"] = ["kind": "split", "axis": "horizontal", "ratio": 0.5,
            "first": ["kind": "pane", "projection": id], "second": remaining]
        replaceLayout(layout)
    }
    func showPane(_ id: String) {
        var layout = object(workspaceDocument["layout"])
        let hidden = array(layout["hidden"]).compactMap { $0 as? String }
        guard hidden.contains(id) else { return }
        let leaf: Object = ["kind": "pane", "projection": id]
        let root = object(layout["root"])
        layout["root"] = root.isEmpty ? leaf : ["kind": "split", "axis": "horizontal", "ratio": 0.5, "first": root, "second": leaf]
        layout["hidden"] = hidden.filter { $0 != id }; replaceLayout(layout)
    }
    func replaceLayout(_ layout: Object) {
        sendCommand(["method": "workspace.apply", "expected": ["layout": object(snapshot["entityRevisions"])["layout"] ?? 0],
                     "operations": [["op": "layout.replace", "value": layout]]])
    }
    private func makeLayout(_ node: Object, path: [String] = []) -> NSViewController? {
        if string(node["kind"]) == "pane" { return panes[string(node["projection"])] }
        guard string(node["kind"]) == "split", let first = makeLayout(object(node["first"]), path: path + ["first"]), let second = makeLayout(object(node["second"]), path: path + ["second"]) else { return nil }
        let split = LayoutSplit(axis: string(node["axis"]), ratio: CGFloat(node["ratio"] as? Double ?? 0.5))
        for (child, key) in [(first, "first"), (second, "second")] {
            let item = NSSplitViewItem(viewController: child)
            let size = PaneLayout.minimum(object(node[key]))
            let minimum = split.axis == "horizontal" ? size.width : size.height
            if key == "first" { split.firstMinimum = minimum } else { split.secondMinimum = minimum }
            split.addSplitViewItem(item)
        }
        let displayed = object(workspaceDocument["layout"])
        let revision = object(snapshot["entityRevisions"])["layout"] ?? 0
        split.saveRatio = { [weak self] ratio in
            var layout = displayed
            layout["root"] = PaneLayout.replacingRatio(ratio, at: path, in: object(displayed["root"]))
            self?.sendCommand(["method": "workspace.apply", "expected": ["layout": revision],
                "operations": [["op": "layout.replace", "value": layout]]])
        }
        return split
    }
    func setParameter(_ id: String, value: Any) {
        sendCommandWhenReady { [self] in
            let revision = object(object(snapshot["parameterStates"])[id])["revision"] as? Int ?? 0
            return ["method": "workspace.apply", "expected": ["state/\(id)": revision],
                    "operations": [["op": "parameter.set", "id": id, "value": value]]]
        }
    }
    func queryTable(_ id: String, sort: [Object], filters: [Object], offset: Int) {
        let pane = object(object(currentView["panes"])[id])
        send(["operation": "table.query", "projectionId": id,
              "source": pane["source"] ?? NSNull(),
              "expectedRevision": object(pane["viewSettings"])["revision"] ?? 0,
              "expectedProjectionRevision": object(snapshot["entityRevisions"])["projections/\(id)"] ?? 0,
              "sort": sort, "filters": filters, "offset": offset])
    }
    func pinPane(_ id: String, pinned: Bool) {
        let pane = object(object(currentView["panes"])[id])
        send(["operation": "view.pin", "projectionId": id, "pinned": pinned,
            "expectedRevision": object(pane["viewSettings"])["revision"] ?? 0,
            "expectedProjectionRevision": object(snapshot["entityRevisions"])["projections/\(id)"] ?? 0])
    }
    func selectTableRow(_ id: String, key: String?) {
        let pane = object(object(currentView["panes"])[id])
        send(["operation": "table.select", "projectionId": id, "source": pane["source"] ?? NSNull(),
            "key": key as Any? ?? NSNull(),
            "expectedRevision": object(pane["viewSettings"])["revision"] ?? 0,
            "expectedProjectionRevision": object(snapshot["entityRevisions"])["projections/\(id)"] ?? 0])
    }
    func toggleTerminal() {
        if terminal != nil { terminalBox.isHidden.toggle(); return }
        do {
            let view = try GhosttyTerminal(directory: app.root)
            terminal = view; terminalBox.addArrangedSubview(view)
            view.translatesAutoresizingMaskIntoConstraints = false
            view.widthAnchor.constraint(equalTo: terminalBox.widthAnchor).isActive = true
            view.heightAnchor.constraint(greaterThanOrEqualToConstant: 160).isActive = true
            terminalBox.isHidden = false; window?.makeFirstResponder(view)
        } catch { showError(error.localizedDescription) }
    }
    func activate(projection: String, control: String) {
        let revisions = object(snapshot["entityRevisions"])
        var expected = ["projections/\(projection)": revisions["projections/\(projection)"] as? Int ?? 0]
        for (id, raw) in object(workspaceDocument["bindings"]) {
            let event = object(object(raw)["event"])
            if string(event["kind"]) == "control.activate", string(event["projection"]) == projection, string(event["control"]) == control {
                expected["bindings/\(id)"] = revisions["bindings/\(id)"] as? Int ?? 0
            }
        }
        sendCommand(["method": "control.activate", "payload": ["projectionId": projection, "controlId": control], "expected": expected])
    }
    private func approve() {
        let actions = object(workspaceDocument["actions"])
        let alert = NSAlert(); alert.messageText = "Allow this calculation?"
        alert.informativeText = "Registered Python runs with your account's file and network access. Choose the calculation you trust. This permission covers its current registration."
        let choices = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 320, height: 28))
        choices.addItems(withTitles: actions.keys.sorted()); alert.accessoryView = choices
        alert.addButton(withTitle: "Allow calculation"); alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn, let id = choices.titleOfSelectedItem else { return }
        let registration = string(object(object(actions[id])["implementation"])["registrationId"])
        send(["operation": "approve", "registrationId": registration]) { [weak self] response in
            if response["ok"] as? Bool == true { self?.send(["operation": "drive"]) }
        }
    }
    private func cancelRun() {
        guard let run = array(snapshot["activeRuns"]).first else { return }
        sendCommand(["method": "run.cancel", "payload": ["runId": string(object(run)["runId"])]])
    }
    private func saveLauncher() {
        let panel = NSSavePanel(); panel.directoryURL = FileManager.default.urls(for: .desktopDirectory, in: .userDomainMask).first
        panel.nameFieldStringValue = (window?.title ?? "Melt") + ".meltapp"
        panel.message = "Save a local reference. The workspace stays in its existing data directory."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do { try saveLauncher(to: url) } catch { showError(error.localizedDescription) }
    }
    func saveLauncher(to url: URL) throws {
        try app.data.write(to: url, options: .atomic)
        if let icon = NSImage(systemSymbolName: "cup.and.saucer.fill", accessibilityDescription: "Saved Melt app") {
            NSWorkspace.shared.setIcon(icon, forFile: url.path, options: [])
        }
    }
    private func applyChange() {
        let panel = NSOpenPanel(); panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        panel.message = "Choose a JSON array of Melt operations. Preview the change before applying it."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            let operations = try JSONSerialization.jsonObject(with: Data(contentsOf: url))
            guard let rows = operations as? [Object] else { throw HostError.message("Expected an array of operations.") }
            let revisions = object(snapshot["entityRevisions"])
            var expected: [String: Int] = [:]
            for row in rows {
                let op = string(row["op"])
                if op.hasPrefix("entity.") {
                    let key = string(row["collection"]) + "/" + string(row["id"])
                    expected[key] = revisions[key] as? Int ?? 0
                } else if op == "layout.replace" { expected["layout"] = revisions["layout"] as? Int ?? 0 }
                else if op == "parameter.set" {
                    let id = string(row["id"])
                    expected["state/\(id)"] = object(object(snapshot["parameterStates"])[id])["revision"] as? Int ?? 0
                }
            }
            let message: Object = ["operation": "propose", "method": "workspace.preview", "expected": expected, "operations": rows]
            send(message) { [weak self] response in
                guard response["ok"] as? Bool == true, let self = self else { return }
                let alert = NSAlert(); alert.messageText = "Apply this change?"
                alert.informativeText = String(describing: object(response["result"])["result"] ?? "Preview is available.")
                alert.addButton(withTitle: "Apply"); alert.addButton(withTitle: "Cancel")
                if alert.runModal() == .alertFirstButtonReturn {
                    var apply = message; apply["method"] = "workspace.apply"; apply["requestId"] = UUID().uuidString
                    self.send(apply)
                }
            }
        } catch { showError(error.localizedDescription) }
    }
    func showError(_ message: String) {
        status.stringValue = message; status.textColor = .systemRed
        #if MELT_NATIVE_TESTS
        print("Native host error: \(message)")
        #endif
    }
    func windowWillClose(_ notification: Notification) {
        closing = true; timer?.invalidate()
        terminal?.stop()
        sendNext()
    }
}

#if !MELT_NATIVE_TESTS
@main
#endif
final class MeltApp: NSObject, NSApplicationDelegate {
    private var workspaces: [Workspace] = []
    private var configuration: Object = [:]
    private var opened = false
    private var terminating = false
    static func main() {
        let app = NSApplication.shared
        let delegate = MeltApp(); app.delegate = delegate; app.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { app.run() }
    }
    func applicationWillFinishLaunching(_ notification: Notification) {
        do {
            guard let url = Bundle.main.url(forResource: "Runtime", withExtension: "json") else { throw HostError.message("The runtime configuration is missing. Rebuild this app from native/build.sh.") }
            configuration = object(try JSONSerialization.jsonObject(with: Data(contentsOf: url)))
            let menu = NSMenu(), item = NSMenuItem(), appMenu = NSMenu()
            appMenu.addItem(withTitle: "Open Melt app…", action: #selector(openDocument(_:)), keyEquivalent: "o")
            appMenu.addItem(.separator())
            appMenu.addItem(withTitle: "Quit Melt", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
            item.submenu = appMenu; menu.addItem(item); NSApp.mainMenu = menu
            let editItem = NSMenuItem(), edit = NSMenu(title: "Edit")
            for (title, action, key) in [("Undo", "undo:", "z"), ("Redo", "redo:", "Z"),
                ("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
                edit.addItem(withTitle: title, action: NSSelectorFromString(action), keyEquivalent: key)
            }
            editItem.submenu = edit; menu.addItem(editItem)
        } catch { report(error) }
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.activate(ignoringOtherApps: true)
        if !opened { openDocument(nil) }
    }
    @objc func openDocument(_ sender: Any?) {
        let panel = NSOpenPanel(); panel.allowedFileTypes = ["meltapp"]
        guard panel.runModal() == .OK, let url = panel.url else { return }; open(url)
    }
    func application(_ sender: NSApplication, openFiles filenames: [String]) {
        for filename in filenames { open(URL(fileURLWithPath: filename)) }
        sender.reply(toOpenOrPrint: .success)
    }
    private func open(_ url: URL) {
        do {
            let app = try LocalApp(Data(contentsOf: url))
            if let retiring = workspaces.first(where: { $0.app.root == app.root && $0.app.workspace == app.workspace && $0.closing && !$0.serviceClosed }) {
                retiring.afterClose = { [weak self] in self?.open(url) }
                return
            }
            if let existing = workspaces.first(where: { $0.app.root == app.root && $0.app.workspace == app.workspace && $0.window?.isVisible == true }) {
                existing.showWindow(nil); return
            }
            let controller = try Workspace(app: app, configuration: configuration)
            workspaces.append(controller); opened = true; controller.showWindow(nil)
        } catch { report(error) }
    }
    private func report(_ error: Error) {
        let alert = NSAlert(); alert.messageText = "Cannot open Melt"; alert.informativeText = error.localizedDescription; alert.runModal()
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if workspaces.allSatisfy({ $0.serviceClosed }) { return .terminateNow }
        if !terminating {
            terminating = true
            for workspace in workspaces where !workspace.serviceClosed {
                workspace.afterClose = { [weak self] in
                    guard let self = self else { return }
                    if self.workspaces.allSatisfy({ $0.serviceClosed }) {
                        NSApp.reply(toApplicationShouldTerminate: true)
                    }
                }
                if !workspace.closing { workspace.close() }
            }
        }
        return .terminateLater
    }
}
