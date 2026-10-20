import AppKit

final class CopyTable: NSTableView {
    var selectedText: (() -> String)?
    @objc func copy(_ sender: Any?) {
        guard let text = selectedText?(), !text.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command,
           event.charactersIgnoringModifiers == "c" { copy(nil); return true }
        return super.performKeyEquivalent(with: event)
    }
}

final class DataTable: NSObject, NSTableViewDataSource, NSTableViewDelegate {
    let view = NSStackView(), scroll = NSScrollView(), table = CopyTable()
    let column = NSPopUpButton(), direction = NSPopUpButton(), operation = NSPopUpButton()
    let value = NSTextField(string: "")
    let pageLabel = label("", size: 10)
    private(set) var previous: ClickButton!, next: ClickButton!
    var query: (([Object], [Object], Int) -> Void)?
    var select: ((String?) -> Void)?
    private var updating = false
    private var rows: [[Any]] = [], columns: [String] = []
    private var settings: Object = [:], types: Object = [:], page: Object = [:]
    private var history: [Int] = [], filterSignature = ""
    init(identity: String = "table") {
        super.init()
        view.orientation = .vertical; view.alignment = .leading; view.spacing = 5
        table.dataSource = self; table.delegate = self
        table.usesAlternatingRowBackgroundColors = true; table.rowHeight = 24
        table.allowsMultipleSelection = true
        table.setAccessibilityLabel("Table results")
        table.selectedText = { [weak self] in self?.copyText() ?? "" }
        scroll.documentView = table; scroll.hasVerticalScroller = true; scroll.hasHorizontalScroller = true
        column.setAccessibilityLabel("Sort or filter column")
        direction.addItems(withTitles: ["Ascending", "Descending"])
        direction.setAccessibilityLabel("Sort direction")
        operation.addItems(withTitles: ["equals", "less than", "at most", "greater than", "at least", "contains", "is empty"])
        operation.setAccessibilityLabel("Filter operator")
        value.placeholderString = "Value"; value.setAccessibilityLabel("Filter value")
        value.widthAnchor.constraint(greaterThanOrEqualToConstant: 60).isActive = true
        let sort = ClickButton("Sort") { [weak self] in self?.sort() }
        let filter = ClickButton("Filter") { [weak self] in self?.filter() }
        let clear = ClickButton("Clear") { [weak self] in self?.query?([], [], 0) }
        for (control, name) in [(column, "column"), (direction, "direction"), (operation, "operator"),
                                (value, "value"), (sort, "sort"), (filter, "filter"), (clear, "clear")] as [(NSControl, String)] {
            control.identifier = NSUserInterfaceItemIdentifier("\(identity)/table-\(name)")
        }
        let sorting = NSStackView(views: [column, direction, sort])
        let filtering = NSStackView(views: [operation, value, filter, clear])
        previous = ClickButton("Previous") { [weak self] in
            guard let self = self else { return }
            let offset = self.history.popLast() ?? max(0, (self.page["offset"] as? Int ?? 0) - 64)
            self.request(offset)
        }
        next = ClickButton("Next") { [weak self] in
            guard let self = self, let offset = self.page["nextOffset"] as? Int else { return }
            self.history.append(self.page["offset"] as? Int ?? 0); self.request(offset)
        }
        previous.identifier = NSUserInterfaceItemIdentifier("\(identity)/table-previous")
        next.identifier = NSUserInterfaceItemIdentifier("\(identity)/table-next")
        let paging = NSStackView(views: [previous, next, pageLabel])
        for row in [sorting, filtering, paging] { row.orientation = .horizontal; row.spacing = 5 }
        for child in [sorting, filtering, scroll, paging] { view.addArrangedSubview(child) }
        scroll.widthAnchor.constraint(equalTo: view.widthAnchor).isActive = true
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 80).isActive = true
    }
    private func request(_ offset: Int) {
        query?(array(settings["sort"]).map(object), array(settings["filters"]).map(object), offset)
    }
    private func sort() {
        guard let key = column.titleOfSelectedItem else { return }
        history = []
        query?([["column": key, "direction": direction.indexOfSelectedItem == 0 ? "asc" : "desc"]],
               array(settings["filters"]).map(object), 0)
    }
    private func filter() {
        guard let key = column.titleOfSelectedItem else { return }
        let operators = ["eq", "lt", "lte", "gt", "gte", "contains", "isNull"]
        let op = operators[max(0, operation.indexOfSelectedItem)]
        var item: Object = ["column": key, "operator": op]
        if op != "isNull" {
            switch string(types[key]) {
            case "int64", "float64", "decimal":
                guard let number = Double(value.stringValue), number.isFinite else { NSSound.beep(); return }
                item["value"] = number
            case "bool":
                guard ["true", "false"].contains(value.stringValue.lowercased()) else { NSSound.beep(); return }
                item["value"] = value.stringValue.lowercased() == "true"
            default: item["value"] = value.stringValue
            }
        }
        history = []; query?(array(settings["sort"]).map(object), [item], 0)
    }
    func update(_ data: Object, settings: Object = [:], types: Object = [:]) {
        updating = true; defer { updating = false }
        self.settings = settings; self.types = types
        if let sort = array(settings["sort"]).first {
            direction.selectItem(at: string(object(sort)["direction"]) == "desc" ? 1 : 0)
        }
        if jsonSignature(data["source"] ?? [:]) != jsonSignature(page["source"] ?? [:]) { history = [] }
        page = data
        let columns = array(data["columns"]).compactMap { $0 as? String }
        if self.columns != columns {
            self.columns = columns
            let selected = column.titleOfSelectedItem
            column.removeAllItems(); column.addItems(withTitles: columns)
            if let selected = selected, columns.contains(selected) { column.selectItem(withTitle: selected) }
            for column in table.tableColumns { table.removeTableColumn(column) }
            for key in columns {
                let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(key))
                column.title = key; column.width = 130; table.addTableColumn(column)
            }
        }
        let signature = jsonSignature(settings["filters"] ?? [])
        if signature != filterSignature, value.currentEditor() == nil {
            if let item = array(settings["filters"]).first {
                let filter = object(item)
                column.selectItem(withTitle: string(filter["column"]))
                let operators = ["eq", "lt", "lte", "gt", "gte", "contains", "isNull"]
                operation.selectItem(at: operators.firstIndex(of: string(filter["operator"])) ?? 0)
                value.stringValue = filter["value"].map { String(describing: $0) } ?? ""
            } else { value.stringValue = "" }
            filterSignature = signature
        }
        rows = array(data["rows"]).compactMap { $0 as? [Any] }; table.reloadData()
        table.allowsMultipleSelection = string(data["selectionMode"]) != "single"
        if string(data["selectionMode"]) == "single" {
            if let selected = data["selectedKey"] as? String,
               let index = array(data["rowKeys"]).firstIndex(where: { $0 as? String == selected }) {
                table.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
            } else { table.deselectAll(nil) }
        }
        let offset = data["offset"] as? Int ?? 0
        previous.isEnabled = offset > 0; next.isEnabled = data["hasMore"] as? Bool == true
        pageLabel.stringValue = rows.isEmpty ? "No rows" : "Rows \(offset + 1)–\(offset + rows.count)"
    }
    func copyText() -> String {
        table.selectedRowIndexes.compactMap { index -> String? in
            guard rows.indices.contains(index) else { return nil }
            return rows[index].map { $0 is NSNull ? "" : String(describing: $0) }.joined(separator: "\t")
        }.joined(separator: "\n")
    }
    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }
    func selectionShouldChange(in tableView: NSTableView) -> Bool { string(page["selectionMode"]) != "none" }
    func tableViewSelectionDidChange(_ notification: Notification) {
        guard !updating, string(page["selectionMode"]) == "single" else { return }
        let keys = array(page["rowKeys"]), index = table.selectedRow
        select?(keys.indices.contains(index) ? keys[index] as? String : nil)
    }
    func tableView(_ tableView: NSTableView, viewFor column: NSTableColumn?, row: Int) -> NSView? {
        guard rows.indices.contains(row), let column = column,
              let index = table.tableColumns.firstIndex(of: column), rows[row].indices.contains(index) else { return nil }
        let value = rows[row][index]
        let cell = label(value is NSNull ? "—" : String(describing: value))
        cell.isSelectable = true
        return cell
    }
}
