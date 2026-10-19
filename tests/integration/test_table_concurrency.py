"""Blocked table queries must not hold the workspace lock."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from runtime.storage import PlatformAdapter
from test_scheduling import seed_workspace, envelope, requires_storage_worker
from test_table_reader import _import_rich_source

pytestmark = requires_storage_worker


@pytest.mark.parametrize('close_during_read', [False, True])
def test_sort_allows_edits_and_cached_pages(tmp_path, monkeypatch, close_during_read):
    import runtime.workspace.table_cache as module
    seeded = seed_workspace(PlatformAdapter.with_root(tmp_path), grant=False)
    service = seeded.service
    _import_rich_source(service)
    entered = threading.Event()
    release = threading.Event()
    original = module.query_indices

    def gated(*args, **kwargs):
        entered.set()
        assert release.wait(10), 'test did not release the sorted read'
        return original(*args, **kwargs)

    def page(request_id, sort=()):
        return service.dispatch(envelope(request_id, method='table.page', payload={
            'source': {'sourceId': 'rich', 'version': 1}, 'offset': 0, 'limit': 2, 'sort': list(sort),
        }))

    assert page('warm').ok
    captured = service.commit_sequence
    monkeypatch.setattr(module, 'query_indices', gated)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(page, 'slow', [{'column': 'revenue', 'direction': 'desc'}])
            try:
                assert entered.wait(5)
                edited = pool.submit(service.dispatch, envelope('edit', method='workspace.apply',
                    expected={'state/minimum': 1}, operations=[{
                        'op': 'parameter.set', 'id': 'minimum', 'value': 42,
                    }])).result(timeout=3)
                assert edited.ok, edited.errors
                cached = pool.submit(page, 'cached').result(timeout=3)
                assert cached.ok and cached.commitSequence == edited.commitSequence
                if close_during_read:
                    pool.submit(service.close).result(timeout=3)
                    assert service._table_cache.bytes == 0
            finally:
                release.set()
            result = pending.result(timeout=5)
            assert result.ok and result.commitSequence == captured
            assert [row[1] for row in result.result['rows']] == [300.0, 300.0]
            if close_during_read:
                assert service._table_cache.bytes == 0
    finally:
        release.set()
        service.close()
