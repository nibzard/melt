# RFC 001: Semantic objects, events, and composable projections

Status: Proposed; not part of `melt/v0.1`.

A request for comments (RFC) records a proposed extension and its acceptance tests. This RFC follows the execution foundation in [SPEC.md](../../SPEC.md). It makes the graph-and-inspector example in [note.md](../../note.md) a separate architectural milestone.

## 1. Decision summary

Preserve the v0.1 execution, permission, revision, and recovery contracts. Add a small semantic interaction model in a later language version.

The extension has four parts:

- Semantic collections identify records across immutable source versions.
- Typed events can assign shared state without running code.
- Projections can observe shared state through fixed, typed selectors.
- Actions publish named outputs independently of projections and layout.

Do not accept these fields in a v0.1 document. Use the provisional language identifier `melt/semantic-draft-1` in isolated experimental fixtures. It is not a stable interchange version. An accepted extension requires a new language identifier and an explicit migration path.

The first implementation runs on Linux. A test renderer and a command-line harness prove event routing and plan updates. Native macOS integration follows. Neither a browser application nor a universal object database is required.

## 2. Problem and constraints

The sales workflow proves that saved controls can run Python and publish durable results. It does not prove that different views share an object's identity.

Selecting a supplier should update an inspector and highlight its graph node without invoking Python or a model. Adding another view should not require changes to the graph's behavior.

The v0.1 output target rule also couples computation to panes. A calculation should be able to publish a result when no pane exists. New compatible views should be able to use that result without rerunning the calculation.

The extension MUST preserve these constraints:

- A valid document never grants execution permission.
- Pure interactions cannot execute project code or make network requests.
- Human state survives unrelated definition changes.
- Source content remains immutable and versioned.
- Invalid mutations leave the existing workspace intact.
- Old computations cannot replace newer requested results.
- Saved views remain readable without a model or project environment.

The prototype includes single selection, direct state assignment, graph highlighting, and record lookup. Multi-selection, arbitrary expressions, joins, automatic relationship traversal, editing object records, and custom executable renderers remain outside scope.

## 3. Semantic collections and references

### 3.1 Collection definition

A semantic collection is a typed identity index over a table source. It does not copy records into another database.

```json
{
  "collections": {
    "suppliers": {
      "source": {"sourceId": "supplier-records", "version": 3},
      "key": "supplier_id"
    }
  }
}
```

The key column MUST contain unique, non-null strings. The first extension restricts keys to strings to avoid cross-language numeric identity rules. Keys are case-sensitive. Do not trim or normalize them silently.

The service verifies the index when admitting the source version. Duplicate or invalid keys reject the update. Display labels and row positions are not identities.

A collection binding pins one source version. Updating it requires a revision-checked mutation. The compiler verifies dependent field selectors against the new schema before commitment.

### 3.2 Reference value

A serialized semantic reference contains `collection`, `incarnation`, and `key`:

```json
{
  "collection": "suppliers",
  "incarnation": "collection-epoch-1",
  "key": "S-42"
}
```

The service issues an opaque incarnation when creating a collection. Snapshots include it. Clients cannot choose it. Deleting and recreating the same collection ID creates another incarnation. An old event cannot select an object in the new collection by accident.

References are workspace-scoped. They do not include a source version. Resolution uses the collection's current pinned version. Run inputs and evidence records capture that version separately.

Within one incarnation, a key always refers to the same logical object. A producer MUST NOT recycle keys for different objects. The runtime cannot infer real-world identity from equal strings. Source adapters must document and preserve this constraint.

Renaming a key is a new identity in this extension. Explicit aliases and identity migration are deferred.

### 3.3 Missing and deleted records

A valid reference may become unresolved when a newer source version omits its key. Preserve the reference and report `missing`; do not select another row or clear it silently. If the same logical object returns under that key, resolution resumes.

A collection cannot be deleted while current definitions or state values reference it. The same transaction may remove dependent definitions and clear or delete their state records. Historical references remain interpretable through saved collection and source revisions.

The resolver returns `resolved`, `missing`, or `unavailable`. Missing means the key is absent in an available source. Unavailable means the source or its index cannot be read. Both differ from null selection.

## 4. Shared semantic state

```json
{
  "state": {
    "selected_supplier": {
      "type": {"kind": "ref", "collection": "suppliers"},
      "nullable": true,
      "default": null,
      "scope": "workspace"
    }
  }
}
```

The first extension adds nullable reference state with workspace scope. Existing numeric and string parameters keep their v0.1 behavior. Do not migrate them automatically into another state system.

State records contain a value and revision. Defaults apply only on creation. Changing a default does not reset a selection. A reference must match the declared collection and current incarnation at assignment time.

Changing a state's type requires an atomic migration that supplies a valid value. Deleting state requires removing its consumers and event bindings in the same transaction.

Reference state is distinct from an editable source record. Selecting a supplier does not change supplier data. A future record mutation contract must define its own authority, conflict handling, and source publication rules.

## 5. Events and pure transitions

### 5.1 Event envelope

A renderer event contains:

- An event ID and workspace ID.
- Projection ID and definition revision.
- Renderer ID and contract version.
- The displayed source references and collection revisions.
- Event name and typed payload.
- The expected revision of the target state.

The authenticated host supplies origin and authority. Renderer payloads cannot impersonate the host or grant permissions.

The initial event names are `object.select` and `selection.clear`. Selection carries one semantic reference. Clear carries no object data. The renderer contract declares these schemas.

### 5.2 Binding

```json
{
  "bindings": {
    "select_supplier": {
      "on": {
        "projection": "network",
        "event": "object.select"
      },
      "effect": {
        "kind": "state.set",
        "target": "selected_supplier",
        "value": {"eventField": "objectRef"}
      }
    },
    "clear_supplier": {
      "on": {
        "projection": "network",
        "event": "selection.clear"
      },
      "effect": {
        "kind": "state.set",
        "target": "selected_supplier",
        "value": {"literal": null}
      }
    }
  }
}
```

The compiler permits only declared event fields and typed literals. There are no expressions, function calls, string interpolation, or executable callbacks.

Each event address has at most one binding in this extension. Each binding writes one state record. The compiler rejects duplicate writers at the same event address. Multi-state transactions from one event require a later contract.

A state assignment does not emit another user event. Updating observers cannot trigger a binding recursively. Selection never implicitly invokes a capability. An explicit button may invoke an action with the current selection as an input under a separate execution contract.

### 5.3 Commit and latency

The host validates an event against its installed plan and MAY display an optimistic selection immediately. The service checks the event, references, revisions, and permissions before committing the state change and receipt together.

A pure transition uses only trusted metadata and typed values. It does not launch Python, call a model, or execute a renderer extension.

Duplicate event IDs return the original receipt. Reuse with different content is an error. Stale state or projection revisions return a conflict. A stale source or collection revision rejects the event, even if its key still exists. The host refreshes and requires a new gesture. Do not silently replay user intent against changed data.

The first prototype permits one pending selection event per state record per host. Queue later local gestures until acknowledgement and stamp them with the new revision. This avoids conflicts caused by the host racing itself.

After rejection, restore the committed selection and expose a brief conflict status. A saved indicator appears only after acknowledgement. Restart restores only committed state.

## 6. Projection observers

Observers are selectors in the compiled dependency plan. They read state and sources; they do not write either.

```json
{
  "projections": {
    "network": {
      "renderer": {"id": "diagram.flowchart", "version": 1},
      "graph": "supplier-network",
      "observe": {
        "highlight": {"state": "selected_supplier"}
      }
    },
    "inspector": {
      "renderer": {"id": "record", "version": 1},
      "collection": "suppliers",
      "observe": {
        "record": {"state": "selected_supplier"}
      }
    }
  }
}
```

These are extension fragments, not complete documents. Renderer-specific schema definitions must accompany the experimental fixtures.

The `record` selector resolves a reference in the projection's declared collection. Null displays an empty-selection state. Missing displays the missing key. Unavailable displays a recoverable source error. No state is changed by these display conditions.

The `highlight` selector maps a reference to zero or more visible elements. No matching element means no highlight. The selection remains valid for other projections.

A later table filter may use a fixed `reference.equals` selector with an explicit typed column mapping. General queries and relationship traversal are not required for the first graph milestone.

Adding an observer updates the dependency index. It does not increment the observed state's revision or change existing bindings. Viewport, zoom, and pane state remain keyed by projection identity.

## 7. Typed graph and renderer boundary

### 7.1 Minimal graph contract

The first graph consists of nodes and edges. Each node has a stable node ID, a plain-text label, and a semantic reference. Each edge has a stable edge ID and two existing node IDs.

Node IDs identify visual graph elements. Semantic references identify domain objects. Several nodes may represent the same object. Keep these identity domains separate.

The graph declares the collections its nodes may reference. The prototype permits one collection per selectable graph so that selection has one static reference type. Nodes outside that collection must be non-selectable or rejected by validation.

A graph artifact is immutable. Its identity map is validated and stored with its source version. Graph positions are presentation state and cannot change supplier records.

### 7.2 Renderer registry

The host ships a versioned registry of trusted renderers. Each entry defines:

- Renderer ID, exact contract version, and implementation version.
- Configuration and compatible source schemas.
- Typed event schemas and supported observer slots.
- Stable element-to-object mapping requirements.
- Lifecycle, accessibility, resource limits, and failure behavior.

Plans pin the contract version. Cache keys also include the implementation version. An unavailable renderer displays a fallback with source details and diagnostics. It does not execute downloaded code or silently reinterpret an incompatible contract.

This registry creates an extension boundary. It does not authorize custom component loading. Signing, isolation, and distribution of third-party renderers remain separate future work.

### 7.3 Pele adapter

The proposed Pele adapter renders a validated graph. The graph and identity map remain authoritative. Never derive object identity from visible labels or generated SVG element order.

The adapter must prove that it can map rendered elements back to graph node IDs. Use a verified renderer mapping or host-owned hit regions. If the pinned renderer cannot support a reliable mapping, report the limitation and use the Linux test renderer for semantic tests. Do not claim interactive Pele integration based only on SVG generation.

Sanitize labels before lowering the graph. Disable external resources and executable content in rendered output. Keep hit testing, focus, keyboard selection, and accessibility in the trusted host adapter.

A Linux integration probe must establish the actual pinned Pele interface. This RFC does not assert an upstream hit-testing or event API.

## 8. Outputs independent of panes

### 8.1 Output declarations and subscriptions

In the extension, an action declares output names, kinds, and a publication group. It has no projection targets.

```json
{
  "actionOutput": {
    "action": "analyze-risk",
    "output": "risk-table",
    "incarnation": "output-epoch-1"
  },
  "follow": "current"
}
```

A projection source may subscribe using this shape. Alternatively, it may pin a source version with the existing source reference form. There is one writer per output identity: the owning action. Many compatible projections may subscribe.

The service assigns a stable output incarnation. Inspection returns this value for use in subscriptions; the compiler verifies it. Deleting and recreating an action does not attach old subscribers to a new calculation. Changing output names or kinds requires revision-checked subscriber migration or removal.

An action with no subscribers can still run and publish. Closing, hiding, or removing a projection does not cancel it or invalidate its outputs.

### 8.2 Publication consistency

Publication still commits every declared output in a group atomically. Generations, captured parameters, source versions, action revisions, and permission checks retain their v0.1 roles.

Eligibility no longer depends on pane existence or presentation configuration. It depends on the action and output contract. Deleting the action invalidates current publication; removing its last subscriber does not.

A successful publication advances output pointers in one transaction. Subscribers resolve those pointers from the same commit sequence. A native frame must not present mixed versions as one current publication group. Explicitly pinned views and stale error fallbacks may display older versions, but must show their version status.

Do not make a pane's preferred columns part of an action's output validity. A projection that cannot render a valid result shows a projection error and retains its prior display with a stale label. Other subscribers still advance. Invalid declared output types continue to reject the entire publication.

Subscribe-after-publication resolves the existing output. It never triggers a run. A pinned subscriber does not advance. A following subscriber shows the current output pointer and its provenance, including stale-result status.

An obsolete run remains historical and cannot advance output pointers. Historical inspection is separate from following the current result.

### 8.3 v0.1 compatibility

Retain v0.1 compilation and publication semantics unchanged. In particular, deleting a v0.1 target still invalidates its run. Do not retrofit the new behavior under the old language identifier.

An explicit migration can turn each v0.1 target into a following subscription. It preserves source versions, projection IDs, parameter values, and layout. The preview must explain that pane removal no longer invalidates output publication.

Quiesce active runs before migration. Validate the new document, save a backup, and commit the language change atomically. A failed migration leaves v0.1 current. Old histories retain their original language semantics.

## 9. Compiler and command changes

Extend the existing plans rather than create another compiler:

| Plan | Addition |
|---|---|
| State | Reference types, collection incarnation, and resolution rules |
| Interaction | Event schema, source guards, and typed assignment |
| Dependency | State-to-observer and output-to-subscriber indexes |
| Identity | Graph nodes, semantic references, and visual elements |
| Render | Renderer versions and observer slots |
| Action | Stable output identities without pane ownership |

Inspection must expose collection revisions, state values, unresolved references, subscriptions, and renderer versions. Preview must identify affected observers and any state migration. An unrelated observer addition must report no state reset and no run invalidation.

Add structured errors for reference-type mismatch, stale events, invalid collection keys, missing observer slots, unsupported renderer versions, and invalid output subscriptions. Errors include JSON Pointer paths and relevant record revisions.

Document patches still validate their final candidate and commit atomically. Pure event commits use the same receipt and notification machinery. This extension must not introduce a second persistence engine or scheduler.

## 10. Linux acceptance tests

### 10.1 Baseline sales workflow

Run every v0.1 contract and execution test unchanged. Then migrate a completed sales workspace explicitly and verify equal saved sources, parameters, and layout. Add a second table subscriber without modifying or rerunning the action.

### 10.2 Supplier fixture

Use two suppliers, `S-42` and `S-73`, with distinct names. Create graph nodes that reference them and a record inspector. The graph and table source remain immutable fixtures.

1. Select `S-42` through the test renderer's event contract.
2. Verify one state commit and an inspector record for `S-42`.
3. Verify graph highlighting through the compiled observer plan.
4. Verify zero model requests and zero Python process launches.
5. Restart and restore the committed reference and inspector resolution.
6. Remove `S-42` in a new collection source version and verify a missing state.
7. Reject an event from the previous graph or collection revision.

### 10.3 Agent-style extension

Apply a revision-checked patch that adds a second inspector observing the current selection. It may add a pane and change layout, but must not replace the existing graph, action, state, or bindings.

Assert that the selected reference and state revision remain unchanged. Assert that existing projection IDs and their view state remain unchanged. Assert that admitted runs retain eligibility. The new inspector must resolve the current selection immediately.

The deterministic test uses a stored agent-shaped patch. A later live-agent trial may generate it. Model quality is not part of the runtime correctness assertion.

### 10.4 Output independence

Publish a result with no projections. Add two following projections afterward and verify that both resolve the same saved output without execution. Remove one during another run. Verify successful publication to the remaining subscriber.

Also test pinned subscriptions, obsolete generations, missing output files, deleted actions, incompatible projection settings, and atomic multi-output visibility.

### 10.5 Conflicts and recovery

Test duplicate events, reused IDs with changed payloads, stale state revisions, a recreated collection ID, failed commits, lost acknowledgements, and unavailable renderer versions.

An optimistic selection must never be reported as durable before commit. An unavailable graph renderer must not prevent inspection of saved sources or semantic state.

## 11. Delivery and acceptance decision

1. Preserve the v0.1 Linux baseline and its fixtures.
2. Implement collection indexes, reference state, and assignment validation in the experimental language.
3. Add the test renderer, observers, and graph-to-inspector tests.
4. Implement independent outputs and explicit migration tests.
5. Probe Pele rendering and identity mapping on Linux.
6. Review semantics and accept or revise the extension before assigning a stable language version.
7. Integrate native renderers and accessibility in the later macOS round.

The architecture test passes when a new view can observe existing semantic state without rewriting unrelated components. SVG generation alone does not pass. A graph that launches Python to update its inspector does not pass.

Remaining integration decisions are the pinned Pele version, its supported identity mapping, and the stable language version after review. These do not change the pure interaction contract. No upstream integration claim is considered verified by this document.
