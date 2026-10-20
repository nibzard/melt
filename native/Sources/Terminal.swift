import AppKit
import GhosttyKit

// The terminal is a host surface. It is never a document projection and
// cannot grant permission to a workspace action through its output.
final class GhosttyEngine {
    static var resources = ""
    static var credentialNames: Set<String> = []
    static var credentialSuffixes: [String] = []
    static let shared = GhosttyEngine()
    private(set) var app: ghostty_app_t?
    private var config: ghostty_config_t?
    private init() {
        if !Self.resources.isEmpty { setenv("GHOSTTY_RESOURCES_DIR", Self.resources, 1) }
        // The policy is generated from the same Python boundary used for
        // project runners. Remove credentials before Ghostty captures its
        // child environment; the terminal cannot inherit model authority.
        for name in ProcessInfo.processInfo.environment.keys where
            Self.credentialNames.contains(name) || Self.credentialSuffixes.contains(where: { name.hasSuffix($0) }) {
            unsetenv(name)
        }
        guard ghostty_init(0, nil) == 0, let config = ghostty_config_new() else { return }
        self.config = config
        // Use application defaults, without loading a user's Ghostty command
        // or automatic window actions into a restored Melt workspace.
        ghostty_config_finalize(config)
        var runtime = ghostty_runtime_config_s()
        runtime.userdata = Unmanaged.passUnretained(self).toOpaque()
        runtime.supports_selection_clipboard = false
        runtime.wakeup_cb = { pointer in
            guard let pointer = pointer else { return }
            let engine = Unmanaged<GhosttyEngine>.fromOpaque(pointer).takeUnretainedValue()
            DispatchQueue.main.async { if let app = engine.app { ghostty_app_tick(app) } }
        }
        runtime.action_cb = { _, _, _ in false }
        runtime.read_clipboard_cb = { _, _, _, _, _, _ in GHOSTTY_CLIPBOARD_READ_UNAVAILABLE }
        runtime.confirm_read_clipboard_cb = { pointer, _, state, _ in
            guard let pointer = pointer,
                  let surface = Unmanaged<GhosttyTerminal>.fromOpaque(pointer).takeUnretainedValue().surface else { return }
            ghostty_surface_deny_clipboard_request(surface, state)
        }
        runtime.write_clipboard_cb = { _, _, content, count, confirm in
            guard !confirm, let content = content else { return }
            for index in 0..<count {
                let entry = content[index]
                guard let mime = entry.mime, String(cString: mime) == "text/plain", let bytes = entry.data,
                      let text = String(data: Data(bytes: bytes, count: entry.len), encoding: .utf8) else { continue }
                NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string)
                break
            }
        }
        runtime.close_surface_cb = { pointer, _ in
            guard let pointer = pointer else { return }
            let view = Unmanaged<GhosttyTerminal>.fromOpaque(pointer).takeUnretainedValue()
            DispatchQueue.main.async { view.stop() }
        }
        app = ghostty_app_new(&runtime, config)
    }
}

final class GhosttyTerminal: NSView, NSTextInputClient {
    private(set) var surface: ghostty_surface_t?
    private let marked = NSMutableAttributedString(string: "")
    private var keyEvent: NSEvent?, inserted = false
    private var markedSelection = NSRange(location: 0, length: 0)
    private var tracking: NSTrackingArea?
    var processID: UInt64 { surface.map { ghostty_surface_foreground_pid($0) } ?? 0 }
    init(directory: String) throws {
        super.init(frame: NSRect(x: 0, y: 0, width: 800, height: 200))
        wantsLayer = true
        setAccessibilityRole(.textArea); setAccessibilityLabel("Ghostty terminal")
        guard let app = GhosttyEngine.shared.app else { throw HostError.message("Ghostty could not initialize.") }
        var config = ghostty_surface_config_new()
        config.userdata = Unmanaged.passUnretained(self).toOpaque()
        config.platform_tag = GHOSTTY_PLATFORM_MACOS
        config.platform = ghostty_platform_u(macos: ghostty_platform_macos_s(nsview: Unmanaged.passUnretained(self).toOpaque()))
        config.scale_factor = Double(window?.backingScaleFactor ?? NSScreen.main?.backingScaleFactor ?? 2)
        surface = directory.withCString { path in
            "/bin/zsh -f".withCString { command in
                config.working_directory = path; config.command = command
                return ghostty_surface_new(app, &config)
            }
        }
        guard surface != nil else { throw HostError.message("Ghostty could not create a terminal surface.") }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }
    deinit { stop() }
    func stop() { if let surface = surface { self.surface = nil; ghostty_surface_free(surface) } }
    override var acceptsFirstResponder: Bool { true }
    override func becomeFirstResponder() -> Bool {
        if let surface = surface { ghostty_surface_set_focus(surface, true) }
        return true
    }
    override func resignFirstResponder() -> Bool {
        if let surface = surface { ghostty_surface_set_focus(surface, false) }
        return true
    }
    override func viewDidMoveToWindow() { super.viewDidMoveToWindow(); resize() }
    override func layout() { super.layout(); resize() }
    private func resize() {
        guard let surface = surface else { return }
        let scale = window?.backingScaleFactor ?? 2
        ghostty_surface_set_content_scale(surface, scale, scale)
        if let display = window?.screen?.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber {
            ghostty_surface_set_display_id(surface, display.uint32Value)
        }
        ghostty_surface_set_size(surface, UInt32(max(1, bounds.width * scale)), UInt32(max(1, bounds.height * scale)))
        ghostty_surface_refresh(surface)
    }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let tracking = tracking { removeTrackingArea(tracking) }
        let area = NSTrackingArea(rect: .zero, options: [.activeInKeyWindow, .mouseMoved, .inVisibleRect], owner: self)
        addTrackingArea(area); tracking = area
    }
    private func mods(_ flags: NSEvent.ModifierFlags) -> ghostty_input_mods_e {
        var result: UInt32 = 0
        if flags.contains(.shift) { result |= GHOSTTY_MODS_SHIFT.rawValue }
        if flags.contains(.control) { result |= GHOSTTY_MODS_CTRL.rawValue }
        if flags.contains(.option) { result |= GHOSTTY_MODS_ALT.rawValue }
        if flags.contains(.command) { result |= GHOSTTY_MODS_SUPER.rawValue }
        return ghostty_input_mods_e(rawValue: result)
    }
    private func key(_ event: NSEvent, action: ghostty_input_action_e, text: String? = nil) {
        guard let surface = surface else { return }
        var key = ghostty_input_key_s()
        key.action = action; key.keycode = UInt32(event.keyCode)
        key.mods = mods(event.modifierFlags)
        key.consumed_mods = mods(event.modifierFlags.subtracting([.control, .command]))
        key.unshifted_codepoint = event.characters(byApplyingModifiers: [])?.unicodeScalars.first?.value ?? 0
        key.composing = hasMarkedText()
        if let text = text {
            text.withCString { key.text = $0; _ = ghostty_surface_key(surface, key) }
        } else { _ = ghostty_surface_key(surface, key) }
    }
    override func keyDown(with event: NSEvent) {
        keyEvent = event; inserted = false
        inputContext?.handleEvent(event)
        if !inserted { key(event, action: event.isARepeat ? GHOSTTY_ACTION_REPEAT : GHOSTTY_ACTION_PRESS) }
        keyEvent = nil
    }
    override func keyUp(with event: NSEvent) { key(event, action: GHOSTTY_ACTION_RELEASE) }
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        guard event.modifierFlags.contains(.command) else { return false }
        if event.charactersIgnoringModifiers == "v" { paste(nil); return true }
        if event.charactersIgnoringModifiers == "c", let surface = surface {
            return "copy_to_clipboard".withCString { ghostty_surface_binding_action(surface, $0, UInt(strlen($0))) }
        }
        return false
    }
    @objc func paste(_ sender: Any?) {
        guard let text = NSPasteboard.general.string(forType: .string) else { return }
        paste(text)
    }
    @objc func copy(_ sender: Any?) {
        if let surface = surface { _ = "copy_to_clipboard".withCString { ghostty_surface_binding_action(surface, $0, UInt(strlen($0))) } }
    }
    func paste(_ text: String) {
        guard let surface = surface else { return }
        text.withCString { ghostty_surface_paste_text(surface, $0, UInt(text.utf8.count)) }
    }
    func insertText(_ value: Any, replacementRange: NSRange) {
        let text = (value as? NSAttributedString)?.string ?? value as? String ?? ""
        unmarkText(); inserted = true
        if let event = keyEvent { key(event, action: event.isARepeat ? GHOSTTY_ACTION_REPEAT : GHOSTTY_ACTION_PRESS, text: text) }
        else if let surface = surface { text.withCString { ghostty_surface_text(surface, $0, UInt(text.utf8.count)) } }
    }
    func setMarkedText(_ value: Any, selectedRange: NSRange, replacementRange: NSRange) {
        let text = (value as? NSAttributedString)?.string ?? value as? String ?? ""
        marked.setAttributedString(NSAttributedString(string: text)); inserted = true
        markedSelection = selectedRange
        if let surface = surface { text.withCString { ghostty_surface_preedit(surface, $0, UInt(text.utf8.count)) } }
    }
    func unmarkText() {
        marked.setAttributedString(NSAttributedString(string: ""))
        if let surface = surface { ghostty_surface_preedit(surface, nil, 0) }
    }
    func hasMarkedText() -> Bool { marked.length > 0 }
    func markedRange() -> NSRange { NSRange(location: hasMarkedText() ? 0 : NSNotFound, length: marked.length) }
    func selectedRange() -> NSRange { hasMarkedText() ? markedSelection : NSRange(location: 0, length: 0) }
    override func accessibilityValue() -> Any? { screenText() }
    func screenText() -> String {
        guard let surface = surface else { return "" }
        let region = ghostty_selection_s(
            top_left: ghostty_point_s(tag: GHOSTTY_POINT_VIEWPORT, coord: GHOSTTY_POINT_COORD_TOP_LEFT, x: 0, y: 0),
            bottom_right: ghostty_point_s(tag: GHOSTTY_POINT_VIEWPORT, coord: GHOSTTY_POINT_COORD_BOTTOM_RIGHT, x: 0, y: 0),
            rectangle: false)
        var text = ghostty_text_s()
        guard ghostty_surface_read_text(surface, region, &text) else { return "" }
        defer { ghostty_surface_free_text(surface, &text) }
        guard let pointer = text.text else { return "" }
        return String(decoding: UnsafeRawBufferPointer(start: pointer, count: min(Int(text.text_len), 1_048_576)), as: UTF8.self)
    }
    func validAttributesForMarkedText() -> [NSAttributedString.Key] { [] }
    func attributedSubstring(forProposedRange range: NSRange, actualRange: NSRangePointer?) -> NSAttributedString? {
        guard range.location != NSNotFound, NSMaxRange(range) <= marked.length else { return nil }
        actualRange?.pointee = range; return marked.attributedSubstring(from: range)
    }
    func characterIndex(for point: NSPoint) -> Int { 0 }
    func firstRect(forCharacterRange range: NSRange, actualRange: NSRangePointer?) -> NSRect {
        actualRange?.pointee = range
        var x = 0.0, y = 0.0, width = 0.0, height = 0.0
        if let surface = surface { ghostty_surface_ime_point(surface, &x, &y, &width, &height) }
        return window?.convertToScreen(convert(NSRect(x: x, y: bounds.height - y - height, width: width, height: height), to: nil)) ?? .zero
    }
    override func doCommand(by selector: Selector) {
        if let event = keyEvent { inserted = true; key(event, action: GHOSTTY_ACTION_PRESS) }
    }
    private func mouse(_ event: NSEvent, state: ghostty_input_mouse_state_e? = nil, button: ghostty_input_mouse_button_e = GHOSTTY_MOUSE_LEFT) {
        guard let surface = surface else { return }
        let point = convert(event.locationInWindow, from: nil)
        ghostty_surface_mouse_pos(surface, point.x, bounds.height - point.y, mods(event.modifierFlags))
        if let state = state { _ = ghostty_surface_mouse_button(surface, state, button, mods(event.modifierFlags)) }
    }
    override func mouseDown(with event: NSEvent) { window?.makeFirstResponder(self); mouse(event, state: GHOSTTY_MOUSE_PRESS) }
    override func mouseUp(with event: NSEvent) { mouse(event, state: GHOSTTY_MOUSE_RELEASE) }
    override func mouseDragged(with event: NSEvent) { mouse(event) }
    override func mouseMoved(with event: NSEvent) { mouse(event) }
    override func rightMouseDown(with event: NSEvent) { mouse(event, state: GHOSTTY_MOUSE_PRESS, button: GHOSTTY_MOUSE_RIGHT) }
    override func rightMouseUp(with event: NSEvent) { mouse(event, state: GHOSTTY_MOUSE_RELEASE, button: GHOSTTY_MOUSE_RIGHT) }
    override func scrollWheel(with event: NSEvent) {
        if let surface = surface { ghostty_surface_mouse_scroll(surface, event.scrollingDeltaX, event.scrollingDeltaY, 0) }
    }
}
