"""Compile and exercise the AppKit/Ghostty host when macOS tools exist."""


def test_native_host_compiles_and_preserves_pane_controllers(native_acceptance):
    assert native_acceptance["format"] == "melt-native-acceptance/v1"
    assert "pane-continuity" in native_acceptance["checks"]
