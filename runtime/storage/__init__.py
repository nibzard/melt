"""Storage: the Pi Durable adapter and the artifact store (SPEC.md section 14).

The service is the sole writer to each workspace database. Artifact
bytes stay outside the database. Backlog tasks T002, T008, and T011
probe the adapter, implement workspace ownership, and register
verified artifacts.
"""

from __future__ import annotations
