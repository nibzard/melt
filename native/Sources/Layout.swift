import AppKit

final class DividerSplitView: NSSplitView {
    var finishedDragging: (() -> Void)?
    override func mouseDown(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        let position = isVertical ? point.x : point.y
        let divider = subviews.first.map { isVertical ? $0.frame.maxX : $0.frame.maxY } ?? -100
        let isDivider = abs(position - divider) <= dividerThickness + 5
        super.mouseDown(with: event)
        if isDivider { finishedDragging?() }
    }
}

final class LayoutContainer: NSView {
    var resized: (() -> Void)?
    override func layout() {
        super.layout()
        // Reparenting controllers during AppKit's layout pass is forbidden.
        DispatchQueue.main.async { [weak self] in self?.resized?() }
    }
}

enum PaneLayout {
    static func minimum(_ node: Object) -> NSSize {
        if string(node["kind"]) == "pane" { return NSSize(width: 240, height: 160) }
        guard string(node["kind"]) == "split" else { return .zero }
        let first = minimum(object(node["first"])), second = minimum(object(node["second"]))
        return string(node["axis"]) == "horizontal"
            ? NSSize(width: first.width + second.width + 1, height: max(first.height, second.height))
            : NSSize(width: max(first.width, second.width), height: first.height + second.height + 1)
    }
    static func leaves(_ node: Object) -> [String] {
        if string(node["kind"]) == "pane" { return [string(node["projection"])] }
        return string(node["kind"]) == "split" ? leaves(object(node["first"])) + leaves(object(node["second"])) : []
    }
    static func removing(_ id: String, from node: Object) -> Object? {
        if string(node["kind"]) == "pane" { return string(node["projection"]) == id ? nil : node }
        guard string(node["kind"]) == "split" else { return nil }
        let first = removing(id, from: object(node["first"])), second = removing(id, from: object(node["second"]))
        guard let first = first else { return second }
        guard let second = second else { return first }
        return node.merging(["first": first, "second": second]) { _, new in new }
    }
    static func replacingRatio(_ ratio: Double, at path: [String], in node: Object) -> Object {
        guard let key = path.first else { return node.merging(["ratio": ratio]) { _, new in new } }
        var copy = node
        copy[key] = replacingRatio(ratio, at: Array(path.dropFirst()), in: object(node[key]))
        return copy
    }
    static func detach(_ controller: NSViewController?) {
        guard let controller = controller else { return }
        if let split = controller as? NSSplitViewController {
            for item in split.splitViewItems {
                detach(item.viewController)
                split.removeSplitViewItem(item)
            }
        }
        controller.view.removeFromSuperview()
    }
}
