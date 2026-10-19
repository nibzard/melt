"""Cache reuse must preserve table results and artifact checks."""

import dataclasses
import os

import pytest
import pyarrow.parquet as pq

from runtime.storage.artifacts import ArtifactStore
from runtime.workspace.table_cache import TablePageCache
from runtime.workspace.reader import read_table_page
from test_table_reader import rich_bytes, rich_schema_wire, page


@pytest.mark.parametrize('budget', [0, 2048, 1024 * 1024])
def test_cache_matches_uncached_pages(tmp_path, budget):
    store = ArtifactStore(tmp_path)
    payload = rich_bytes()
    artifact = store.write(payload)
    schema = rich_schema_wire()
    cache = TablePageCache(max_bytes=budget, max_entries=3)
    for filters in [[], [{'column': 'revenue', 'operator': 'gte', 'value': 100}]]:
        for sort in [[], [{'column': 'revenue', 'direction': 'desc'}]]:
            for offset in [0, 2, 7, 99, 0]:
                query = page(sort=sort, filters=filters, offset=offset, limit=2)
                assert cache.read(store, artifact, schema, query) == read_table_page(
                    payload, schema, query
                )
                assert cache.bytes <= budget
                assert len(cache.entries) <= 3
    cache.clear()
    assert cache.bytes == 0 and not cache.entries


def test_scrolling_reuses_decode_and_sort(tmp_path, monkeypatch):
    import runtime.workspace.table_cache as module
    store = ArtifactStore(tmp_path)
    artifact = store.write(rich_bytes())
    cache = TablePageCache()
    schema = rich_schema_wire()
    query = page(sort=[{'column': 'revenue', 'direction': 'desc'}], limit=2)
    cache.read(store, artifact, schema, query)

    def unexpected(*args, **kwargs):
        raise AssertionError('a cached page must not decode or sort again')

    monkeypatch.setattr(pq, 'read_table', unexpected)
    monkeypatch.setattr(module, 'query_indices', unexpected)
    result, errors = cache.read(store, artifact, schema, dataclasses.replace(query, offset=2))
    assert errors is None and len(result['rows']) == 2


@pytest.mark.parametrize('replacement', ['missing', 'symlink', 'fifo', 'corrupt'])
def test_cached_artifact_is_checked_on_every_read(tmp_path, replacement):
    store = ArtifactStore(tmp_path)
    artifact = store.write(rich_bytes())
    cache = TablePageCache()
    schema = rich_schema_wire()
    cache.read(store, artifact, schema, page())
    path = store.path_for(artifact)
    path.unlink()
    if replacement == 'corrupt':
        path.write_bytes(b'not parquet')
        result, errors = cache.read(store, artifact, schema, page())
        assert result is None and errors[0].code == 'E_ARTIFACT'
    else:
        if replacement == 'fifo':
            os.mkfifo(path)
        if replacement == 'symlink':
            target = tmp_path / 'target'
            target.write_bytes(rich_bytes())
            path.symlink_to(target)
        with pytest.raises(OSError):
            cache.read(store, artifact, schema, page())


def test_cache_does_not_hide_in_place_corruption(tmp_path):
    store = ArtifactStore(tmp_path)
    artifact = store.write(rich_bytes())
    cache = TablePageCache()
    schema = rich_schema_wire()
    cache.read(store, artifact, schema, page())
    path = store.path_for(artifact)
    path.chmod(0o644)
    with path.open('r+b') as handle:
        handle.seek(-4, os.SEEK_END)
        handle.write(b'BAD!')
    result, errors = cache.read(store, artifact, schema, page())
    assert result is None and errors[0].code == 'E_ARTIFACT'


def test_invalid_query_does_not_open_artifact(tmp_path):
    store = ArtifactStore(tmp_path)
    query = page(sort=[{'column': 'absent', 'direction': 'asc'}])
    result, errors = TablePageCache().read(
        store, 'sha256:' + '0' * 64, rich_schema_wire(), query
    )
    assert result is None and errors[0].code == 'E_REFERENCE'


@pytest.mark.parametrize('budget', [0, 1024 * 1024])
def test_partial_pages_cross_batches_and_row_groups(tmp_path, budget):
    import pyarrow as pa
    from runtime.workspace.importer import measure_artifact
    table = pa.table({'number': range(13000), 'text': ['é'] * 13000})
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, row_group_size=6000)
    payload = sink.getvalue().to_pybytes()
    schema = measure_artifact(payload, kind='table', media_type='application/vnd.apache.parquet')
    store = ArtifactStore(tmp_path)
    artifact = store.write(payload)
    cache = TablePageCache(max_bytes=budget)
    for offset in [0, 4090, 5990, 6000, 10090, 11990, 12990, 20000, 0]:
        query = page(offset=offset, limit=50)
        assert cache.read(store, artifact, schema, query) == read_table_page(payload, schema, query)
        assert cache.bytes <= budget


def test_plain_page_decodes_only_needed_batches(tmp_path, monkeypatch):
    import pyarrow as pa
    from runtime.workspace.importer import measure_artifact
    table = pa.table({'number': range(13000)})
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, row_group_size=6000)
    payload = sink.getvalue().to_pybytes()
    schema = measure_artifact(payload, kind='table', media_type='application/vnd.apache.parquet')
    store = ArtifactStore(tmp_path)
    artifact = store.write(payload)
    cache = TablePageCache()
    original = pq.ParquetFile
    decoded = []

    class ObservedFile:
        def __init__(self, *args, **kwargs):
            self.file = original(*args, **kwargs)
        def __getattr__(self, name):
            return getattr(self.file, name)
        def iter_batches(self, **kwargs):
            for batch in self.file.iter_batches(**kwargs):
                decoded.append((kwargs['row_groups'], batch.num_rows))
                yield batch

    def no_full_decode(*args, **kwargs):
        raise AssertionError('plain pages must not decode the complete table')

    monkeypatch.setattr(pq, 'ParquetFile', ObservedFile)
    monkeypatch.setattr(pq, 'read_table', no_full_decode)
    for offset in [6000, 6050, 7000]:
        result, errors = cache.read(store, artifact, schema, page(offset=offset))
        assert errors is None and result['rows'][0] == [str(offset)]
    assert decoded == [([1], 4096)]


def test_cached_batch_does_not_wait_for_an_uncached_sort(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import pyarrow as pa
    import runtime.workspace.table_cache as module
    from runtime.workspace.importer import measure_artifact

    sink = pa.BufferOutputStream()
    pq.write_table(pa.table({'number': range(13000)}), sink)
    payload = sink.getvalue().to_pybytes()
    schema = measure_artifact(payload, kind='table', media_type='application/vnd.apache.parquet')
    store = ArtifactStore(tmp_path)
    artifact = store.write(payload)
    # One batch fits; the complete decoded table does not.
    cache = TablePageCache(max_bytes=64 * 1024)
    assert cache.read(store, artifact, schema, page())[1] is None
    entered, release = threading.Event(), threading.Event()
    original = module.query_indices

    def gated(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(module, 'query_indices', gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(cache.read, store, artifact, schema,
                              page(sort=[{'column': 'number', 'direction': 'desc'}]))
        try:
            assert entered.wait(5)
            result, errors = pool.submit(cache.read, store, artifact, schema, page(offset=50)).result(timeout=3)
            assert errors is None and result['rows'][0] == ['50']
        finally:
            release.set()
        assert pending.result(timeout=5)[1] is None
    assert cache.bytes <= cache.max_bytes
