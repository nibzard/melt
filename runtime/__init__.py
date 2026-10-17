"""Melt service runtime for the Linux round (SPEC.md section 2.2).

The runtime keeps compilation, execution, publication, and storage
separate. It never imports AppKit, SwiftUI, macOS paths, or Ghostty
view objects. Platform adapters own data paths, file synchronization,
process ownership, and credential access.
"""

from __future__ import annotations
