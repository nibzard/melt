"""Measure workspace growth and concurrent framed service requests on Linux.

Run with uv run --locked python3 scripts/benchmark_scale.py.
Each table size runs in a fresh process. This drives process_frame directly;
it does not measure a network server or the serial stdio serve loop.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import benchmark_round1 as base


class MeasuredLock:
    """Measure outermost lock acquisition and ownership per caller thread."""
    def __init__(self, lock):
        self.lock = lock
        self.local = threading.local()

    def reset(self):
        self.local.wait = self.local.held = 0.0

    def __enter__(self):
        depth = getattr(self.local, 'depth', 0)
        start = time.perf_counter()
        self.lock.acquire()
        if depth == 0:
            self.local.wait = getattr(self.local, 'wait', 0.0) + time.perf_counter() - start
            self.local.start = time.perf_counter()
        self.local.depth = depth + 1
        return self

    def __exit__(self, *args):
        self.local.depth -= 1
        if self.local.depth == 0:
            self.local.held = getattr(self.local, 'held', 0.0) + time.perf_counter() - self.local.start
        self.lock.release()


def stats(values):
    return {"n": len(values), "p50Ms": round(base.percentile(values, .5), 3),
            "p95Ms": round(base.percentile(values, .95), 3), "maxMs": round(max(values), 3)}


def rss_bytes():
    for line in Path('/proc/self/status').read_text().splitlines():
        if line.startswith('VmRSS:'):
            return int(line.split()[1]) * 1024
    return None


def checked(answer):
    if not answer['ok']:
        raise RuntimeError(json.dumps(answer))
    return answer


def seed(root, payload, projections, parameters):
    adapter = base.PlatformAdapter.with_root(root)
    service = base.WorkspaceService.open(adapter, 'scale')
    try:
        result = service.create_workspace({
            'language': 'melt/v0.1', 'workspaceId': 'scale', 'title': 'Scale benchmark',
            'sources': {}, 'parameters': {
                f'p{i}': {'type': 'number', 'nullable': False, 'default': 0, 'constraints': {}}
                for i in range(parameters)
            }, 'actions': {}, 'bindings': {}, 'projections': {},
            'layout': {'root': None, 'hidden': []},
        }, 'create')
        assert result.ok, result.errors
        token = service.capture_artifact_token(payload, base.PARQUET).token
        checked(base.framed(service, 'scale', 'source.import', request_id='import',
            expected={'sources/big': 0}, payload={
                'sourceId': 'big', 'artifactToken': token, 'originLabel': 'big.parquet'}))
        names = [f'view{i}' for i in range(projections)]
        operations = [{'op': 'entity.create', 'collection': 'projections', 'id': name,
                       'value': base._table_projection(name, [], source={'sourceId': 'big', 'version': 1})}
                      for name in names]
        operations.append({'op': 'layout.replace', 'value': {
            'root': base._split_tree(names), 'hidden': []}})
        checked(base.framed(service, 'scale', 'workspace.apply', request_id='views',
            expected={'layout': 1, **{f'projections/{n}': 0 for n in names}}, operations=operations))
    finally:
        service.close()
    return adapter


def edit(service, revision, request_id):
    return checked(base.framed(service, 'scale', 'workspace.apply', request_id=request_id,
        expected={'state/p0': revision}, operations=[{
            'op': 'parameter.set', 'id': 'p0', 'value': revision}]))


def read(service, request_id, index=0, sort=None):
    answer = checked(base.framed(service, 'scale', 'table.page', request_id=request_id,
        payload={'source': {'sourceId': 'big', 'version': 1}, 'offset': index * 50 % 1000,
                 'limit': 50, 'sort': sort or [], 'filters': []}))
    assert len(answer['result']['rows']) == 50
    return answer


def inspect(service, request_id):
    return checked(base.framed(service, 'scale', 'workspace.inspect', request_id=request_id))


def concurrent(service, name, clients, requests, revision, mode):
    barrier = threading.Barrier(clients + 1)
    before = service.commit_sequence

    def caller(client):
        samples = []
        writer = mode == 'edits' or (mode.startswith('mixed') and client == 0)
        role = 'edit' if writer else 'page'
        barrier.wait()
        for index in range(requests):
            service._storage_lock.reset()
            start = time.perf_counter()
            request_id = f'{name}-{client}-{index}'
            if writer:
                edit(service, revision + index, request_id)
            else:
                sort = None
                if mode == 'mixed-sort':
                    # Each reader has a distinct query. A small filter value
                    # change forces row-order recomputation for every request.
                    answer = checked(base.framed(service, 'scale', 'table.page', request_id=request_id,
                        payload={'source': {'sourceId': 'big', 'version': 1}, 'offset': 0,
                                 'limit': 50, 'sort': [{'column': 'score', 'direction': 'desc'}],
                                 'filters': [{'column': 'revenue', 'operator': 'gte',
                                              'value': -100000 + client * requests + index}]}))
                    assert len(answer['result']['rows']) == 50
                else:
                    read(service, request_id, index, sort)
            elapsed = (time.perf_counter() - start) * 1000
            local = service._storage_lock.local
            samples.append({'role': role, 'latencyMs': elapsed,
                            'lockWaitMs': local.wait * 1000, 'lockHeldMs': local.held * 1000})
        return samples

    with ThreadPoolExecutor(max_workers=clients) as pool:
        futures = [pool.submit(caller, client) for client in range(clients)]
        start = time.perf_counter()
        barrier.wait()
        samples = [sample for future in futures for sample in future.result()]
        elapsed = time.perf_counter() - start
    writes = requests if mode.startswith('mixed') or mode == 'edits' else 0
    assert service.commit_sequence - before == writes
    roles = {}
    for role in {s['role'] for s in samples}:
        subset = [s for s in samples if s['role'] == role]
        roles[role] = {field: stats([s[field] for s in subset])
                       for field in ('latencyMs', 'lockWaitMs', 'lockHeldMs')}
    result = {'name': name, 'clients': clients, 'requestsPerClient': requests,
              'throughputPerSecond': round(len(samples) / elapsed, 2),
              'elapsedSeconds': elapsed, 'roles': roles, 'samples': samples,
              'retainedCacheBytes': service._table_cache.bytes, 'rssBytes': rss_bytes()}
    print(json.dumps({k: v for k, v in result.items() if k != 'samples'}), flush=True)
    return result, revision + writes


def tables(root, rows, requests):
    print(f'Generating {rows:,} rows', flush=True)
    payload = base.big_table_bytes(rows)
    artifact_bytes = len(payload)
    adapter = seed(root, payload, 32, 256)
    del payload
    results = []
    with base.WorkspaceService.open(adapter, 'scale') as service:
        service._storage_lock = MeasuredLock(service._storage_lock)
        revision = 1
        # Warm kernel caches and the service cache when the table fits.
        read(service, 'warmup')
        for mode, clients in [('edits', 1), ('pages', 1), ('pages', 4), ('pages', 8),
                              ('mixed', 4), ('mixed', 8), ('mixed-sort', 4)]:
            name = f'{rows}-{mode}-{clients}'
            result, revision = concurrent(service, name, clients, requests, revision, mode)
            results.append(result)
        durability = service.store.durability_facts()
    return {'rows': rows, 'columns': 20, 'projections': 32, 'parameters': 256,
            'artifactBytes': artifact_bytes, 'durability': durability, 'results': results}


def metadata(root, requests, checkpoints):
    from runtime.storage.errors import StorageWorkerError

    results = []
    payload = base.big_table_bytes(10_000)
    for projections, parameters in [(8, 16), (32, 256)]:
        for history in checkpoints:
            workspace_root = root / f'{projections}-{parameters}-{history}'
            adapter = seed(workspace_root, payload, projections, parameters)
            revision = 1
            with base.WorkspaceService.open(adapter, 'scale') as service:
                # The measured edits are the final edits of this history.
                while revision <= max(0, history - requests):
                    edit(service, revision, f'history-{revision}')
                    revision += 1
                prior = revision - 1
                service._storage_lock = MeasuredLock(service._storage_lock)
                result, revision = concurrent(service, f'metadata-{projections}-{history}', 1,
                                               requests, revision, 'edits')
                sequence = service.commit_sequence
                document_bytes = len(json.dumps(inspect(service, f'inspect-{history}')['result']['document']).encode())
            restore = []
            failures = []
            for index in range(5):
                start = time.perf_counter()
                try:
                    with base.WorkspaceService.open(adapter, 'scale') as service:
                        answer = inspect(service, f'restore-{history}-{index}')
                        assert answer['commitSequence'] == sequence
                except StorageWorkerError as error:
                    failures.append({'error': str(error), 'elapsedMs': (time.perf_counter() - start) * 1000})
                    # Record the failure; do not hide it by changing limits.
                    break
                restore.append((time.perf_counter() - start) * 1000)
            result.update({'projections': projections, 'parameters': parameters,
                           'priorParameterEdits': prior, 'restoredParameterEdits': revision - 1,
                           'commitSequence': sequence, 'documentBytes': document_bytes,
                           'restoreOpenInspectClose': stats(restore) if restore else None,
                           'restoreSamplesMs': restore, 'restoreFailures': failures,
                           'workspaceRoot': str(workspace_root),
                           'workspaceBytes': sum(p.stat().st_size for p in workspace_root.rglob('*') if p.is_file())})
            print(f"Restore {projections} projections, {revision - 1} edits: "
                  f"{failures or result['restoreOpenInspectClose']}", flush=True)
            results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--output', type=Path, default=Path('build/benchmarks/scale.json'))
    parser.add_argument('--rows', type=int, nargs='+', default=[1_000_000, 3_000_000])
    parser.add_argument('--requests', type=int, default=20)
    parser.add_argument('--history', type=int, nargs='+', default=[20, 100, 500])
    parser.add_argument('--table-child', type=int)
    args = parser.parse_args()
    if args.requests < 1 or min(args.rows) < 1050 or sorted(set(args.history)) != args.history or min(args.history) < 0:
        parser.error('use positive requests, rows >= 1050, and increasing nonnegative history checkpoints')
    root = args.root or Path(tempfile.mkdtemp(prefix='melt-scale-'))
    if args.root:
        root.mkdir(parents=True, exist_ok=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.table_child:
        result = tables(root, args.table_child, args.requests)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        return
    record = {'facts': base.machine_facts(base._REPO, root), 'root': str(root),
              'method': 'Closed-loop threads call process_frame on one service; lock timing includes only outermost acquisitions. No network or native rendering.',
              'metadata': metadata(root / 'metadata', args.requests, args.history), 'tables': []}
    args.output.write_text(json.dumps(record, indent=2) + '\n')
    for rows in args.rows:
        child_output = root / f'table-{rows}.json'
        subprocess.run([sys.executable, __file__, '--table-child', str(rows),
                        '--root', str(root / f'table-{rows}'), '--requests', str(args.requests),
                        '--output', str(child_output)], check=True)
        record['tables'].append(json.loads(child_output.read_text()))
        args.output.write_text(json.dumps(record, indent=2) + '\n')
    print(f'Results: {args.output}; workspaces: {root}', flush=True)


if __name__ == '__main__':
    main()
