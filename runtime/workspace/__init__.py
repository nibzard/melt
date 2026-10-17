"""Workspace service: commands, revisions, receipts, and snapshots (SPEC.md section 12).

The service owns revisions, stored state, permissions, jobs, and
publication. Backlog tasks T009 and T010 implement mutations, preview,
inspection, and the framed IPC envelope.
"""

from __future__ import annotations
