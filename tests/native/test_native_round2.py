"""Functional acceptance backed by the real host, not a deferred placeholder.

Timing samples are retained as evidence; hardware-specific release targets
are assessed in docs/gates/native-application.md rather than timing CI.
"""


def test_native_input_layout_renderers_and_restoration(native_acceptance):
    assert set(native_acceptance["checks"]) >= {
        "pane-continuity", "move-hide-show", "focus", "overflow",
        "table-query-controls", "clipboard-text", "marked-text-input",
        "split-persistence", "plot-export-zoom",
        "accessibility", "markdown-policy", "ghostty-surface",
        "native-calculation", "five-pane-restoration", "linked-selection", "sales-hero",
    }
    assert len(native_acceptance["heartbeatSamplesMs"]) >= 10
    assert len(native_acceptance["patchSamplesMs"]) == 25
    assert native_acceptance["restoredPanes"] == 5
    for key in ("heartbeatP95Ms", "patchP95Ms", "restoreMs"):
        assert native_acceptance[key] > 0
