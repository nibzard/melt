"""Attach declared hardware and dependency versions to native timing evidence."""
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys

configuration_path, evidence_path = map(Path, sys.argv[1:])
configuration = json.loads(configuration_path.read_text())
record = json.loads(evidence_path.read_text())

def output(*command):
    return subprocess.check_output(command, text=True).strip()

node_dir = Path(configuration['nodeDirectory'])
if not node_dir.is_absolute():
    node_dir = configuration_path.parent / node_dir
record['measuredAt'] = datetime.now(timezone.utc).isoformat()
record['hardware'] = {
    'cpu': output('sysctl', '-n', 'machdep.cpu.brand_string'),
    'model': output('sysctl', '-n', 'hw.model'),
    'memoryBytes': int(output('sysctl', '-n', 'hw.memsize')),
    'architecture': platform.machine(),
    'macOS': platform.mac_ver()[0],
    'kernel': platform.release(),
}
record['versions'] = {
    'python': platform.python_version(),
    'node': output(str(node_dir / 'node'), '--version'),
    'swift': output('xcrun', 'swift', '--version'),
    'ghosttyRevision': 'c7cbc9bfd44cb11a9c7eae00f1166624671e363b',
    **{name: importlib.metadata.version(name) for name in ('pyarrow', 'pandas', 'matplotlib')},
}
record['runtimeKind'] = 'relocatable-package' if configuration.get('format') == 'melt-runtime/v1' else 'development'
record['timingMethod'] = {
    'heartbeat': '10 ms main-run-loop timer intervals during a real Python calculation; responsiveness proxy, not input feedback latency',
    'patch': '25 sequential title-only revision-checked mutations, request to acknowledged visible title; excludes model and Python execution',
    'restore': 'one fresh workspace-service launch to all five panes decoded/rendered; warm filesystem; not a cold-cache distribution',
}
evidence_path.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
