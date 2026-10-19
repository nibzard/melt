import AppKit

func jsonSignature(_ value: Any) -> String {
    guard JSONSerialization.isValidJSONObject(value),
          let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { return "" }
    return String(data: data, encoding: .utf8) ?? ""
}

final class LayoutSplit: NSSplitViewController {
    let axis: String, ratio: CGFloat
    private var positioned = false
    init(axis: String, ratio: CGFloat) {
        self.axis = axis; self.ratio = ratio
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }
    override func viewDidLoad() {
        super.viewDidLoad(); splitView.isVertical = axis == "horizontal"
        splitView.dividerStyle = .thin
    }
    override func viewDidLayout() {
        super.viewDidLayout()
        if !positioned, splitViewItems.count == 2 {
            let length = splitView.isVertical ? splitView.bounds.width : splitView.bounds.height
            if length > 0 { splitView.setPosition(length * ratio, ofDividerAt: 0); positioned = true }
        }
    }
}

final class Workspace: NSWindowController, NSWindowDelegate {
    let app: LocalApp
    private let bridge: Bridge
    private var timer: Timer?
    private var busy = false
    var isReady: Bool { !busy && !closing }
    private(set) var closing = false, serviceClosed = false
    var afterClose: (() -> Void)?
    private var snapshot: Object = [:], planned: Object = [:]
    private(set) var currentView: Object = [:]
    var document: Object { object(snapshot["document"]) }
    private var panes: [String: Pane] = [:]
    private var layoutSignature = ""
    private var rootController: NSViewController?
    private let container = NSView(), status = label("Opening workspace…", size: 11)

    init(app: LocalApp, configuration: Object) throws {
        self.app = app; bridge = try Bridge(app: app, configuration: configuration)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1160, height: 760),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        super.init(window: window)
        window.delegate = self; window.title = app.workspace; window.minSize = NSSize(width: 760, height: 520)
        window.center(); window.isReleasedWhenClosed = false
        let root = NSStackView(); root.orientation = .vertical; root.spacing = 8
        let bar = NSStackView(); bar.orientation = .horizontal
        bar.addArrangedSubview(ClickButton("Save to Desktop…") { [weak self] in self?.saveLauncher() })
        bar.addArrangedSubview(ClickButton("Allow calculation…") { [weak self] in self?.approve() })
        bar.addArrangedSubview(ClickButton("Apply change…") { [weak self] in self?.applyChange() })
        bar.addArrangedSubview(ClickButton("Cancel run") { [weak self] in self?.cancelRun() })
        bar.addArrangedSubview(ClickButton("Refresh") { [weak self] in self?.send(["operation": "snapshot"]) })
        root.addArrangedSubview(bar); root.addArrangedSubview(status); root.addArrangedSubview(container)
        window.contentView?.addSubview(root); root.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 10),
            root.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -10),
            root.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 10),
            root.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor, constant: -10),
            container.widthAnchor.constraint(equalTo: root.widthAnchor),
            container.heightAnchor.constraint(greaterThanOrEqualToConstant: 300)
        ])
        send(["operation": "snapshot"])
        timer = Timer.scheduledTimer(withTimeInterval: .7, repeats: true) { [weak self] _ in
            guard let self = self, !self.busy, !self.closing else { return }
            self.send(["operation": "poll"], quiet: true)
        }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }

    func send(_ message: Object, quiet: Bool = false, completion: ((Object) -> Void)? = nil) {
        guard !closing else { return }
        guard !busy else {
            if !quiet { showError("Wait for the current change to finish, then try again.") }; return
        }
        busy = true
        if !quiet { status.textColor = .secondaryLabelColor; status.stringValue = "Working… Changes are not yet acknowledged." }
        bridge.request(message) { [weak self] result in
            guard let self = self, !self.closing else { return }
            self.busy = false
            switch result {
            case .failure(let error): self.showError(error.localizedDescription)
            case .success(let response):
                if let view = response["view"] as? Object { self.update(view) }
                if response["ok"] as? Bool != true { self.showError(String(describing: response["errors"] ?? "The change failed.")) }
                completion?(response)
            }
        }
    }
    func sendCommand(_ command: Object) {
        var message = command; message["operation"] = "command"; message["requestId"] = UUID().uuidString
        send(message) { [weak self] response in
            if response["ok"] as? Bool == true { self?.send(["operation": "drive"]) }
        }
    }
    private func update(_ view: Object) {
        currentView = view
        snapshot = object(view["snapshot"]); planned = object(view["plan"])
        window?.title = string(document["title"])
        guard !planned.isEmpty else {
            showError("The saved workspace needs recovery: \(String(describing: view["planErrors"] ?? []))")
            return
        }
        let definitions = object(document["projections"]), entries = object(object(planned["render"])["projections"])
        let data = object(view["panes"]), states = object(snapshot["parameterStates"])
        for (id, raw) in entries {
            if panes[id] == nil { panes[id] = Pane(identity: id, host: self) }
            panes[id]?.update(definition: object(definitions[id]), plan: object(raw), state: states, data: object(data[id]))
        }
        let layout = object(planned["layout"])
        let signature = jsonSignature(layout)
        if signature != layoutSignature {
            for pane in panes.values { pane.view.removeFromSuperview(); pane.removeFromParent() }
            rootController?.view.removeFromSuperview(); rootController = nil
            panes = panes.filter { entries[$0.key] != nil }
            if let controller = makeLayout(object(layout["root"])) {
                rootController = controller
                container.addSubview(controller.view); controller.view.translatesAutoresizingMaskIntoConstraints = false
                NSLayoutConstraint.activate([
                    controller.view.leadingAnchor.constraint(equalTo: container.leadingAnchor),
                    controller.view.trailingAnchor.constraint(equalTo: container.trailingAnchor),
                    controller.view.topAnchor.constraint(equalTo: container.topAnchor),
                    controller.view.bottomAnchor.constraint(equalTo: container.bottomAnchor)
                ])
            }
            layoutSignature = signature
        }
        status.textColor = .secondaryLabelColor
        let active = array(snapshot["activeRuns"])
        let groups = object(snapshot["outputGroups"])
        let issues = groups.values.map(object).filter { string($0["status"]) != "current" }
        let details = issues.map { "\(string($0["status"]))\(($0["resultIsStale"] as? Bool == true) ? " · saved result uses earlier inputs" : "")" }
        status.stringValue = active.isEmpty ? (details.isEmpty ? "Saved on this Mac" : details.joined(separator: "; ")) : "Calculation queued or running · previous results remain visible"
        if !array(snapshot["diagnostics"]).isEmpty { status.stringValue += " · recovery diagnostics are available" }
    }
    private func makeLayout(_ node: Object) -> NSViewController? {
        if string(node["kind"]) == "pane" { return panes[string(node["projection"])] }
        guard string(node["kind"]) == "split", let first = makeLayout(object(node["first"])), let second = makeLayout(object(node["second"])) else { return nil }
        let split = LayoutSplit(axis: string(node["axis"]), ratio: CGFloat(node["ratio"] as? Double ?? .5))
        split.addSplitViewItem(NSSplitViewItem(viewController: first)); split.addSplitViewItem(NSSplitViewItem(viewController: second))
        return split
    }
    func setParameter(_ id: String, value: Any) {
        let revision = object(object(snapshot["parameterStates"])[id])["revision"] as? Int ?? 0
        sendCommand(["method": "workspace.apply", "expected": ["state/\(id)": revision],
                     "operations": [["op": "parameter.set", "id": id, "value": value]]])
    }
    func activate(projection: String, control: String) {
        let revisions = object(snapshot["entityRevisions"])
        var expected = ["projections/\(projection)": revisions["projections/\(projection)"] as? Int ?? 0]
        for (id, raw) in object(document["bindings"]) {
            let event = object(object(raw)["event"])
            if string(event["kind"]) == "control.activate", string(event["projection"]) == projection, string(event["control"]) == control {
                expected["bindings/\(id)"] = revisions["bindings/\(id)"] as? Int ?? 0
            }
        }
        sendCommand(["method": "control.activate", "payload": ["projectionId": projection, "controlId": control], "expected": expected])
    }
    private func approve() {
        let actions = object(document["actions"])
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
    func showError(_ message: String) { status.stringValue = message; status.textColor = .systemRed }
    func windowWillClose(_ notification: Notification) {
        closing = true; timer?.invalidate()
        bridge.close { [weak self] in
            self?.serviceClosed = true
            self?.afterClose?()
            self?.afterClose = nil
        }
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
