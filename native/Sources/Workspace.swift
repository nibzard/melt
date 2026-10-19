import AppKit

final class ClickButton: NSButton {
    var invoke: (() -> Void)?
    convenience init(_ title: String, action: @escaping () -> Void) {
        self.init(title: title, target: nil, action: nil)
        invoke = action; target = self; self.action = #selector(clicked)
        bezelStyle = .rounded
    }
    @objc private func clicked() { invoke?() }
}

func label(_ text: String, size: CGFloat = 13, bold: Bool = false) -> NSTextField {
    let field = NSTextField(wrappingLabelWithString: text)
    field.font = bold ? .boldSystemFont(ofSize: size) : .systemFont(ofSize: size)
    field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    return field
}

final class DataTable: NSObject, NSTableViewDataSource, NSTableViewDelegate {
    let scroll = NSScrollView(), table = NSTableView()
    private var rows: [[Any]] = []
    override init() {
        super.init()
        table.dataSource = self; table.delegate = self
        table.usesAlternatingRowBackgroundColors = true
        table.rowHeight = 24
        scroll.documentView = table; scroll.hasVerticalScroller = true; scroll.hasHorizontalScroller = true
    }
    func update(_ data: Object) {
        let columns = array(data["columns"]).compactMap { $0 as? String }
        if table.tableColumns.map({ $0.identifier.rawValue }) != columns {
            for column in table.tableColumns { table.removeTableColumn(column) }
            for key in columns {
                let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(key))
                column.title = key; column.width = 130; table.addTableColumn(column)
            }
        }
        rows = array(data["rows"]).compactMap { $0 as? [Any] }
        table.reloadData()
    }
    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }
    func tableView(_ tableView: NSTableView, viewFor column: NSTableColumn?, row: Int) -> NSView? {
        guard let column = column, let index = table.tableColumns.firstIndex(of: column), rows[row].indices.contains(index) else { return nil }
        let value = rows[row][index]
        let text = value is NSNull ? "—" : String(describing: value)
        return label(text)
    }
}

final class Pane: NSViewController, NSTextFieldDelegate {
    let identity: String
    weak var host: Workspace?
    private let stack = NSStackView(), controls = NSStackView(), body = NSStackView()
    private let heading = label("", size: 16, bold: true)
    private var definition: Object = [:], state: Object = [:], data: Object = [:]
    private var signature = ""
    private var editors: [String: NSControl] = [:]
    private var table: DataTable?
    private let tableNotice = label("", size: 10)
    private let textView = NSTextView(), imageView = NSImageView()
    private var bodyKind = ""
    private var lastText = "", lastImage = ""
    private var lastGraph = ""

    init(identity: String, host: Workspace?) {
        self.identity = identity; self.host = host
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }
    override func loadView() {
        view = NSView(); view.wantsLayer = true
        view.layer?.backgroundColor = NSColor.textBackgroundColor.cgColor
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 10
        controls.orientation = .vertical; controls.alignment = .leading; controls.spacing = 6
        body.orientation = .vertical; body.alignment = .leading
        let sections: [NSView] = [heading, controls, body]
        for item in sections { stack.addArrangedSubview(item) }
        view.addSubview(stack); stack.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 12),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -12),
            stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 12),
            stack.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -12),
            body.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
    }
    func update(definition: Object, plan: Object, state: Object, data: Object) {
        if !isViewLoaded { _ = view }
        self.definition = definition; self.state = state; self.data = data
        heading.stringValue = string(definition["title"])
        let definitions = array(plan["controls"]).map(object)
        let newSignature = jsonSignature(definitions)
        if newSignature != signature {
            for view in controls.arrangedSubviews { controls.removeArrangedSubview(view); view.removeFromSuperview() }
            editors.removeAll()
            for control in definitions { addControl(control) }
            signature = newSignature
        }
        for (key, editor) in editors {
            let value = object(state[key])["value"]
            if let toggle = editor as? NSButton { toggle.state = (value as? Bool ?? false) ? .on : .off }
            else if let field = editor as? NSTextField, field.currentEditor() == nil {
                field.stringValue = value.map { String(describing: $0) } ?? ""
            } else if let popup = editor as? NSPopUpButton { popup.selectItem(withTitle: string(value)) }
        }
        let renderer = object(plan["renderer"])
        let rendererID = string(renderer["id"])
        guard renderer["contractVersion"] as? Int == 1,
              ["melt.table", "melt.document", "melt.plot", "melt.graph", "melt.record"].contains(rendererID) else {
            showNotice("This renderer is unavailable. Saved source: \(String(describing: plan["source"] ?? "none"))")
            return
        }
        if string(data["status"]) != "ready" {
            showNotice(string(data["message"])); return
        }
        let kind = string(definition["kind"])
        if kind != bodyKind { clearBody(); bodyKind = kind }
        switch kind {
        case "table":
            if table == nil {
                table = DataTable(); body.addArrangedSubview(table!.scroll)
                table!.scroll.widthAnchor.constraint(equalTo: body.widthAnchor).isActive = true
                table!.scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 100).isActive = true
                body.addArrangedSubview(tableNotice)
            }
            table?.update(object(data["table"]))
            tableNotice.stringValue = object(data["table"])["hasMore"] as? Bool == true ? "Partial table: showing the first 64 rows. Use the harness to read later pages." : ""
        case "document":
            if textView.enclosingScrollView == nil {
                let scroll = NSScrollView(); scroll.hasVerticalScroller = true
                textView.isEditable = false; textView.isSelectable = true
                textView.font = .systemFont(ofSize: 14); textView.textContainerInset = NSSize(width: 6, height: 8)
                textView.isVerticallyResizable = true; textView.autoresizingMask = [.width]
                textView.isHorizontallyResizable = false
                textView.textContainer?.widthTracksTextView = true
                textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
                scroll.documentView = textView; body.addArrangedSubview(scroll)
                scroll.widthAnchor.constraint(equalTo: body.widthAnchor).isActive = true
                scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 100).isActive = true
            }
            let text = string(data["text"]) + ((data["truncated"] as? Bool == true) ? "\n\n[Partial document: showing the first 48,000 characters.]" : "")
            if lastText != text { textView.string = text; lastText = text }
        case "plot":
            if imageView.superview == nil {
                imageView.imageScaling = .scaleProportionallyUpOrDown
                body.addArrangedSubview(imageView)
                imageView.widthAnchor.constraint(equalTo: body.widthAnchor).isActive = true
                imageView.heightAnchor.constraint(greaterThanOrEqualToConstant: 100).isActive = true
                imageView.setAccessibilityLabel(heading.stringValue)
            }
            let path = string(data["imagePath"]), schema = object(data["imageSchema"])
            let width = schema["width"] as? Int ?? 0, height = schema["height"] as? Int ?? 0
            if path != lastImage, width > 0, height > 0, width <= 16_384, height <= 16_384, width * height <= 64_000_000 {
                imageView.image = NSImage(contentsOfFile: path); lastImage = path
            }
        case "record":
            let observer = object(data["observer"])
            let cells = object(object(observer["record"])["cells"])
            showNotice(cells.isEmpty ? "Selection: \(string(observer["display"]))" : cells.keys.sorted().map { "\($0): \(cells[$0]!)" }.joined(separator: "\n"))
        case "graph":
            let graphSignature = jsonSignature(data)
            if graphSignature == lastGraph { return }
            clearBody(); bodyKind = kind
            lastGraph = graphSignature
            let highlighted = array(object(data["observer"])["highlightedNodes"]).compactMap { $0 as? String }
            let nodes = array(object(data["graph"])["nodes"]).map(object)
            for node in nodes.prefix(64) {
                let id = string(node["nodeId"])
                let button = ClickButton((highlighted.contains(id) ? "● " : "○ ") + string(node["label"])) { [weak self] in
                    guard let self = self else { return }
                    let event = object(object(self.data["events"])[id])
                    self.host?.sendCommand(event)
                }
                button.setAccessibilityLabel("Select \(string(node["label"]))")
                body.addArrangedSubview(button)
            }
            if nodes.count > 64 { body.addArrangedSubview(label("Showing the first 64 of \(nodes.count) nodes.")) }
            body.addArrangedSubview(label("Node selector · graph layout is not implemented in this host." , size: 10))
        default: showNotice("Unsupported projection")
        }
    }
    private func clearBody() {
        textView.enclosingScrollView?.documentView = nil
        for item in body.arrangedSubviews { body.removeArrangedSubview(item); item.removeFromSuperview() }
        table = nil; lastText = ""; lastImage = ""; bodyKind = ""
        lastGraph = ""
    }
    private func showNotice(_ message: String) {
        clearBody(); body.addArrangedSubview(label(message.isEmpty ? "No result yet." : message))
    }
    private func addControl(_ value: Object) {
        let kind = string(value["kind"]), title = string(value["label"]), parameter = string(value["parameter"])
        if kind == "button" {
            let id = string(value["id"])
            controls.addArrangedSubview(ClickButton(title) { [weak self] in self?.host?.activate(projection: self?.identity ?? "", control: id) })
            return
        }
        let row = NSStackView(); row.orientation = .horizontal; row.spacing = 8
        row.addArrangedSubview(label(title))
        if kind == "toggle" {
            let button = ClickButton("") { [weak self] in
                guard let self = self, let button = self.editors[parameter] as? NSButton else { return }
                self.host?.setParameter(parameter, value: button.state == .on)
            }
            button.setButtonType(.switch); button.setAccessibilityLabel(title)
            editors[parameter] = button; row.addArrangedSubview(button)
        } else if kind == "select" {
            let popup = NSPopUpButton()
            popup.addItems(withTitles: array(value["options"]).map { string(object($0)["value"]) })
            popup.identifier = NSUserInterfaceItemIdentifier(parameter)
            popup.target = self; popup.action = #selector(selectChanged(_:)); popup.setAccessibilityLabel(title)
            editors[parameter] = popup; row.addArrangedSubview(popup)
        } else {
            let field = NSTextField(string: "")
            field.identifier = NSUserInterfaceItemIdentifier(parameter); field.delegate = self
            field.setAccessibilityLabel(title); field.widthAnchor.constraint(equalToConstant: 100).isActive = true
            editors[parameter] = field; row.addArrangedSubview(field)
        }
        controls.addArrangedSubview(row)
    }
    @objc private func selectChanged(_ sender: NSPopUpButton) {
        host?.setParameter(sender.identifier?.rawValue ?? "", value: sender.titleOfSelectedItem ?? "")
    }
    func controlTextDidEndEditing(_ notification: Notification) {
        guard let field = notification.object as? NSTextField else { return }
        let key = field.identifier?.rawValue ?? ""
        let type = string(object(object(host?.document["parameters"])[key])["type"])
        if type == "number" || type == "integer" {
            guard let number = Double(field.stringValue), number.isFinite else { host?.showError("Enter a finite number."); return }
            if type == "integer" {
                guard number.rounded(.towardZero) == number, abs(number) <= 9_007_199_254_740_991 else { host?.showError("Enter a safe integer."); return }
                host?.setParameter(key, value: Int(number))
            } else { host?.setParameter(key, value: number) }
        } else { host?.setParameter(key, value: field.stringValue) }
    }
}
