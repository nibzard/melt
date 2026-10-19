"""Bounded, session-local caches for immutable table artifacts."""

from collections import OrderedDict
import os
import threading

from melt.schema.canonical import canonical_hash, table_query_hash
from melt.schema.diagnostics import DiagnosticCode
from melt.schema.limits import DEFAULT_LIMITS
from runtime.workspace.reader import (
    MeltReaderError, _declared_order, _diagnostic, encode_page,
    encode_window_page, query_indices, validate_query,
)

BATCH_ROWS = 4096


class TablePageCache:
    """Retain at most 256 MiB of Arrow buffers and 128 cache entries.

    A short lock protects cache bookkeeping. A separate lock serializes
    cache-miss decoding and sorting to limit temporary allocations.
    Cache hits and workspace edits do not wait on that query lock.
    Every request safely opens and checks the immutable artifact file.
    """

    def __init__(self, max_bytes=256 * 1024 * 1024, max_entries=128):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.entries = OrderedDict()
        self.bytes = 0
        self._lock = threading.Lock()
        self._query_lock = threading.Lock()
        self._closed = False

    def clear(self):
        with self._lock:
            self.entries.clear()
            self.bytes = 0

    def close(self):
        # Already admitted reads use independent file handles. They can
        # finish after shutdown, but must not repopulate the cache.
        with self._lock:
            self._closed = True
            self.entries.clear()
            self.bytes = 0

    def _get(self, key):
        with self._lock:
            entry = self.entries.get(key)
            if entry is not None:
                self.entries.move_to_end(key)
                return entry[0]
        return None

    def _put(self, key, value):
        size = value.get_total_buffer_size() + 256
        if size > self.max_bytes or self.max_entries <= 0:
            return
        with self._lock:
            if self._closed:
                return
            previous = self.entries.pop(key, None)
            if previous is not None:
                self.bytes -= previous[1]
            while self.entries and (
                self.bytes + size > self.max_bytes or len(self.entries) >= self.max_entries
            ):
                _, (_, removed) = self.entries.popitem(last=False)
                self.bytes -= removed
            self.entries[key] = (value, size)
            self.bytes += size

    def _plain_page(self, handle, identity, schema, columns, query, limits):
        """Skip irrelevant row groups and decode bounded batches on misses."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        file = pq.ParquetFile(handle)
        empty = pa.Table.from_batches([], schema=file.schema_arrow)
        _declared_order(columns, empty)
        total = file.metadata.num_rows
        start = min(query.offset, total)
        end = min(start + query.limit, total)
        parts = []
        group_start = 0
        for group in range(file.num_row_groups):
            group_end = group_start + file.metadata.row_group(group).num_rows
            if group_start >= end:
                break
            if group_end > start:
                first = max(0, start - group_start) // BATCH_ROWS
                last = (min(end, group_end) - group_start - 1) // BATCH_ROWS
                keys = {index: (identity, 'batch', group, index)
                        for index in range(first, last + 1)}
                batches = {index: self._get(key) for index, key in keys.items()}
                if any(batch is None for batch in batches.values()):
                    with self._query_lock:
                        # Another reader may have filled these batches while
                        # this request waited. Keep any locally held batches.
                        for index, key in keys.items():
                            if batches[index] is None:
                                batches[index] = self._get(key)
                        missing = {index for index, batch in batches.items() if batch is None}
                        if missing:
                            for index, batch in enumerate(file.iter_batches(
                                batch_size=BATCH_ROWS, row_groups=[group],
                            )):
                                if index in missing:
                                    batches[index] = batch
                                    self._put(keys[index], batch)
                                if index >= max(missing):
                                    break
                for index, batch in batches.items():
                    batch_start = group_start + index * BATCH_ROWS
                    left = max(start, batch_start) - batch_start
                    right = min(end, batch_start + batch.num_rows) - batch_start
                    parts.append(batch.slice(left, right - left))
            group_start = group_end
        selected = pa.Table.from_batches(parts, schema=file.schema_arrow)
        return encode_window_page(selected, schema, query, total, limits=limits)

    def read(self, artifacts, artifact, schema, query, *, limits=DEFAULT_LIMITS):
        columns, errors = validate_query(schema, query, limits=limits)
        if errors:
            return None, errors
        import pyarrow as pa
        import pyarrow.parquet as pq

        with artifacts.open(artifact) as handle:
            stat = os.fstat(handle.fileno())
            identity = (artifact, stat.st_dev, stat.st_ino, stat.st_size,
                        stat.st_mtime_ns, stat.st_ctime_ns)
            try:
                table = self._get(identity)
                if not query.filters and not query.sort:
                    if table is not None:
                        return encode_page(table, schema, query, limits=limits)
                    return self._plain_page(handle, identity, schema, columns, query, limits)
                key = (identity, canonical_hash(schema),
                       table_query_hash(query.source, query.sort, query.filters))
                indices = self._get(key)
                if table is not None and indices is not None:
                    return encode_page(table, schema, query, indices, limits=limits)
                with self._query_lock:
                    # Hold references across eviction by other page readers.
                    table = table if table is not None else self._get(identity)
                    if table is None:
                        table = pq.read_table(handle).combine_chunks()
                        self._put(identity, table)
                    indices = indices if indices is not None else self._get(key)
                    if indices is None:
                        indices = query_indices(table, columns, query)
                        self._put(key, indices)
                    return encode_page(table, schema, query, indices, limits=limits)
            except MeltReaderError as error:
                return None, [error.diagnostic]
            except (pa.ArrowException, ValueError) as error:
                return None, [_diagnostic(
                    DiagnosticCode.ARTIFACT, '/payload/source',
                    f'the stored artifact is no longer readable Parquet: {error}',
                )]
