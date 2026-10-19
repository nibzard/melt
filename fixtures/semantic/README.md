# Semantic extension fixtures (`melt/semantic-draft-1`)

These fixtures exercise the provisional RFC 001 extension under its own
language identifier. They are deliberately isolated from
`fixtures/valid` and `fixtures/invalid`, which pin the unchanged
`melt/v0.1` contract suite: a `melt/v0.1` document rejects the
`collections` and `state` fields, and the v0.1 suite must keep passing
without knowing this directory exists (RFC 001 section 1).

Contents:

- `supplier-workspace.json`: one complete semantic workspace. It pins
  the committed `suppliers.parquet` bytes — the two suppliers `S-42`
  and `S-73` of RFC 001 section 10.2 — the committed
  `supplier-network.graph.json` identity map, declares the `suppliers`
  collection keyed by `supplier_id`, one nullable reference state entry
  `selected_supplier` with workspace scope, the `network` graph and
  `inspector` record projections with their observers, and the two pure
  event bindings (RFC 001 sections 5 and 6).
- `supplier-network.graph.json`: the immutable graph artifact. Its
  measured schema is the identity map itself: two nodes bound to the
  `S-42` and `S-73` records of the `suppliers` collection, one edge
  between them (RFC 001 section 7.1).
- `definitions.json`: the mutation operations that build the same
  workspace through the service after the two imports: the collection,
  the state entry, both projections, both bindings, and the layout.
- `patches/second-inspector.json`: the stored agent-shaped patch of
  RFC 001 section 10.3: it adds a second record inspector and replaces
  the layout, touching no existing component.
- `catalog.json`: the trusted catalog entries measured from the
  committed bytes, so `melt compile --catalog
  fixtures/semantic/catalog.json` fully resolves the document.
- `invalid/`: documents that fail with the diagnostic each entry of
  `invalid/manifest.json` records. Every check is enforced by the
  Python schema layer today. The output-subscription entries of RFC 001
  section 8 cover the extension action (no targets), the subscription
  references and kind fit, the pinned and following shapes, and the
  `melt/v0.1` document that rejects a subscription source.

The service-issued incarnation of a collection never appears here: a
document cannot choose it, and snapshots carry it instead (RFC 001
section 3.2). The output incarnations of section 8.1 follow the same
rule: the invalid fixtures carry placeholder tokens because only the
service issues real ones, and a service-less compile cannot verify
them.

Regenerate `suppliers.parquet` and `supplier-network.graph.json` with
`make fixtures`; regeneration is byte-identical under the pinned
toolchain.

`document-hashes.json` records the RFC 8785 hash of every document
fixture here, computed by the Python schema package over the wire form
(every field present, absent optional fields explicit nulls or their
defaults, so the zero `debounceMs` of the pure-event bindings appears).
The index stays separate from `fixtures/valid/document-hashes.json` so
the v0.1 suite runs unchanged; the generated TypeScript and Swift
models of `protocol/generated` verify it beside Python and
`scripts/verify_canonical_hashes.mjs` (SPEC.md section 18.1, backlog
task T007). After editing a fixture, recompute its entry with:

```bash
uv run --locked python3 -c 'from pathlib import Path; import json; from melt.schema.semantic import parse_document_for_language; from melt.schema.canonical import document_hash; p = Path("fixtures/semantic/supplier-workspace.json"); print(document_hash(parse_document_for_language(p.read_text())))'
```
