# Melt v0.1 language and runtime specification

Version: 0.1.0  
Status: Proposed implementation contract; no implementation or performance result is implied.  
First development round: Linux  
Later native application target: macOS on Apple Silicon  
Canonical format: JSON  
Language identifier: `melt/v0.1`

## 1. Purpose and authority

Melt describes durable interfaces that agents can change and people can operate directly. A saved document defines sources, views, parameters, actions, bindings, and layout. A compiler validates the document. A native host renders the compiled plan. A service controls changes, execution, and publication.

The first implementation proves one complete data analysis workflow. It must work without a model after its definitions and results exist.

This specification is the implementation contract for the language milestone. It supersedes conflicting language examples in `note.md`. The broader application specification remains product context. Features outside this milestone require a later specification.

The earlier name, Malleable, becomes Melt. Use `melt` for package names, imports, protocol identifiers, and application directories in new code.

### 1.1 Requirement terms

- **MUST** identifies a release requirement.
- **SHOULD** identifies a default. Record a reason for any exception.
- **MAY** identifies an optional feature.

All requirements apply to v0.1 unless a section explicitly defers them. Linux development gates exclude requirements explicitly assigned to the later native application round. Examples illustrate the contracts defined here. An example does not grant authority to execute code.

### 1.2 Core outcome

A developer must be able to implement this sequence:

1. Load a valid Melt document and its saved artifacts.
2. Display a table, a plot, and a parameter control.
3. Change the parameter without a model request.
4. Run a registered Python action with captured inputs and parameters.
5. Publish the matching table and plot together.
6. Patch the interface without losing unrelated state.
7. Restart and restore the last saved interface without executing project code.

## 2. Scope

### 2.1 Included

- A canonical JSON document and generated language models.
- Immutable source versions and explicit provenance.
- Typed parameter state and deterministic event bindings.
- Registered Python actions with durable definitions.
- Native `table`, `plot`, and `document` projections.
- Stable panes and a binary split layout.
- Atomic, revision-checked changes.
- Compilation, inspection, and dry-run commands.
- Durable result publication and restart recovery.
- A Python software development kit (SDK) that emits the same contracts.
- A small command-line compiler for fixtures and diagnostics.

The native agent prompt and Ghostty terminal are host features. They are not new Melt renderer kinds in this milestone. The host reserves a route to its prompt outside the document's authority.

### 2.2 Development rounds

Round 1 runs on Linux. Implement and test the schema, compiler, workspace service, Python SDK, runner, artifact reader, and persistence adapters there. A command-line harness drives the full action and publication loop. It inspects plans, state, table pages, and generated PNG files without a native renderer or model provider.

Keep the compiler and service independent of AppKit, SwiftUI, macOS paths, and Ghostty view objects. Use platform adapters for application data paths, file synchronization, process ownership, and credential access. The Linux round must not require Apple tools or a macOS machine.

The command-line harness represents a local user for parameter edits and action invocation. Agent and programmatic changes retain their separate event origin. Test this distinction through the same service handlers used by the later native host.

Round 2 integrates the native macOS application and Ghostty panes. Native input, accessibility, controller continuity, display latency, signing, and packaging are acceptance gates for that round. Linux development does not add a Linux desktop application to v0.1 scope.

### 2.3 Deferred

Graphs, diagrams, supplier simulations, editable tables, custom renderer modules, arbitrary expressions, reactive dependency graphs, animation, network synchronization, multiple users, a custom text syntax, persistent Python interpreters, and automatic schema migration are deferred.

An editor and a diff view belong to the broader product. The first language implementation needs only a host action that opens the saved script in an editor.

### 2.4 Implementation boundary

| Layer | Responsibility |
|---|---|
| Python schema package | Author portable schemas and export JSON Schema |
| Compiler | Validate, resolve references, check types, and create plans |
| Workspace service | Own revisions, stored state, permissions, jobs, and publication |
| Native host | Render plans, route input, preserve controllers, and enforce native limits |
| Python runner | Execute an approved script and stage artifacts |
| Agent adapter | Inspect state and propose the same commands available to the host |

The application uses Ghostty, a local Pi Durable service, and Python. Treat their exact adapter interfaces as integration work. Pin and test dependencies before relying on an upstream behavior. This specification does not assert that a particular upstream version already satisfies these requirements.

## 3. Terminology and identities

| Term | Definition |
|---|---|
| Workspace | The container for one document, state, revisions, artifacts, and history |
| Source | A logical collection of immutable content versions |
| Artifact | Immutable bytes addressed by their content hash |
| Projection | A native view over a specific source version |
| Parameter | A typed, persisted value that a control or event can change |
| Action | A durable declaration of registered executable behavior |
| Binding | A typed route from an event to a parameter or action |
| Output group | Projections that receive one action's results atomically |
| Pane | A host controller associated with one projection |
| Generation | A monotonically increasing request number for an output group |
| Definition revision | A version of one document entity |
| State revision | A version of one parameter's current value |
| Commit sequence | A workspace-wide order of durable transactions |

Entity identifiers MUST match `[a-z][a-z0-9_-]{0,63}`. Identifiers are unique within each entity collection. References always include a collection through their field or tagged reference type.

Projection IDs also identify panes. Display titles are not identifiers. Renaming an ID is a delete and create operation.

Artifact hashes use lowercase SHA-256 hex with the prefix `sha256:`. Hashes identify bytes, not trust. Two logical sources MAY refer to the same artifact.

A source reference is exactly `{"sourceId": "sales", "version": 1}`. Source versions are positive integers. A service allocates the next version; clients cannot overwrite an existing version.

## 4. Canonical document

### 4.1 Top-level shape

```json
{
  "language": "melt/v0.1",
  "workspaceId": "sales-demo",
  "title": "Sales analysis",
  "sources": {},
  "parameters": {},
  "actions": {},
  "projections": {},
  "bindings": {},
  "layout": {"root": null, "hidden": []}
}
```

All fields shown above are required. Each collection is a map keyed by an entity ID. An empty map is valid. An empty layout is valid. Titles are strings of 1–256 characters. Labels are strings of 1–256 characters.

Every object rejects unknown fields. Only explicitly designated metadata maps accept arbitrary keys. Version 0.1 defines no such metadata maps. No field accepts executable JavaScript, Swift, callbacks, Python objects, or templated expressions.

The document stores definitions and current published source references. Mutable parameter values, run records, permissions, and revision counters live in service records. A snapshot includes these records separately.

### 4.2 Encoding rules

- Use UTF-8 JSON without duplicate object keys.
- Use finite numbers. Reject `NaN`, infinity, and negative infinity at every boundary.
- Integers MUST stay within `-(2^53 - 1)` through `2^53 - 1` on the wire.
- Store larger integers and exact decimals as typed strings in table data.
- Strings MUST contain valid Unicode scalar values. Do not normalize user text silently.
- Reject invalid values rather than coercing strings into numbers or booleans.
- Sort map keys when calculating a canonical document hash. Preserve array order.
- Use RFC 8785 JSON canonicalization for hashes; include conformance fixtures with the implementation.

The document hash excludes runtime state and permission grants. A compiled plan key also includes renderer versions, source schemas, action registry revisions, and the compiler version.

## 5. Sources and artifacts

### 5.1 Source definition

```json
{
  "kind": "table",
  "currentVersion": 1,
  "versions": {
    "1": {
      "artifact": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
      "mediaType": "application/vnd.apache.parquet",
      "byteLength": 4200,
      "schema": {
        "columns": [
          {"id": "month", "type": "string", "nullable": false},
          {"id": "revenue", "type": "float64", "nullable": false}
        ],
        "rowKey": null
      },
      "origin": {"kind": "import", "label": "sales.csv"}
    }
  }
}
```

The all-zero hash above is a shape example, not a valid fixture artifact.

Required source fields are `kind`, `currentVersion`, and `versions`. Supported kinds are `table`, `image`, and `text`. Version keys are canonical decimal strings. `currentVersion` MUST refer to an included version.

Every version requires `artifact`, `mediaType`, `byteLength`, `schema`, and `origin`. `byteLength` is a nonnegative safe integer. The service verifies bytes, size, and hash before registration. Document commands cannot forge artifact records.

| Kind | Media type | Schema |
|---|---|---|
| `table` | `application/vnd.apache.parquet` | `columns` and `rowKey` |
| `image` | `image/png` | Positive integer `width` and `height` |
| `text` | `text/plain` or `text/markdown` | `{"encoding":"utf-8"}` |

Column IDs use the entity ID syntax and MUST be unique. The import adapter maps original column labels to IDs and retains labels through an optional `label` string. Column types are `string`, `bool`, `int64`, `float64`, `decimal`, `date`, and `timestamp`.

A decimal column additionally requires `precision` and `scale`: precision is 1–38, and scale is 0–precision. Dates use `YYYY-MM-DD`. Timestamps use UTC RFC 3339 strings with a `Z` suffix. Table page values for `int64` and `decimal` are strings. Null is allowed only for nullable columns. Non-finite table floats are rejected during publication in v0.1.

`rowKey` is null or a column ID whose values are unique and non-null. The reader verifies this before enabling durable row selection. A row index is never a durable row identity.

`origin` is one of:

- `{"kind":"import","label":"sales.csv"}`.
- `{"kind":"run","runId":"<service-issued-id>","output":"filtered"}`.

The service attaches run provenance. Project code cannot claim a different run ID.

### 5.2 Source lifecycle

Input imports create immutable snapshots before execution. A project path is not a source version. An explicit import or refresh operation captures new bytes and creates a new version.

A projection remains pinned to its source reference until an explicit patch or successful publication changes it. Updating `currentVersion` alone does not retarget projections or action inputs.

Source versions are append-only. Patches cannot change their bytes, schemas, or provenance. Removing a logical source is allowed only when no current definition references it. History still retains referenced artifacts.

Artifact deletion is deferred. Version 0.1 MUST NOT collect artifacts referenced by saved state, run history, or revision history. It MAY remove abandoned staging files after recovery.

## 6. Parameters and controls

### 6.1 Parameter definition

```json
{
  "type": "number",
  "default": 0,
  "nullable": false,
  "constraints": {"minimum": 0, "maximum": 1000000000}
}
```

Required fields are `type`, `default`, `nullable`, and `constraints`. Types are `string`, `number`, `integer`, and `boolean`.

Numeric constraints MAY contain inclusive `minimum` and `maximum`. String constraints MAY contain `maxLength` and an `enum` of unique strings. Boolean constraints MUST be empty. No regular expression or arbitrary validator is supported.

The default MUST satisfy the type and constraints. Null requires `nullable: true`. Defaults apply only when creating a parameter. Changing a default does not reset existing state.

All parameters have workspace scope and persist. Session-only state, such as hover and scroll offsets, belongs to the host and is not a parameter.

### 6.2 State record

The service stores each parameter as `{value, revision}`. The host submits edits through `parameter.set`; the authenticated native event endpoint marks them as user changes. Agent and SDK mutations are programmatic changes. Revisions begin at 1. A successful value change increments the revision. Writing an equal value is a no-op unless the command also contains another change.

The host MAY show an optimistic value. It MUST show an unsaved indicator until acknowledgement. On rejection, retain the attempted value as an editable error or restore the committed value. Do not label it saved.

### 6.3 Control definition

Every projection has a `controls` array. A control contains `id`, `kind`, and `label`. IDs are unique within the projection.

| Kind | Additional required fields | Compatibility |
|---|---|---|
| `number` | `parameter` | Number or integer |
| `text` | `parameter` | String |
| `toggle` | `parameter` | Non-null boolean |
| `select` | `parameter`, `options` | String with enum constraints |
| `button` | None | Emits activation only |

Each select option has `value` and `label`. Option values MUST exactly match the parameter enum. Control values always derive from parameter state. Controls cannot carry a second initial value.

Editing a parameter control produces one typed parameter update. Once committed, bindings observe the change. A button emits `control.activate` and never changes a parameter itself.

## 7. Actions and execution registry

### 7.1 Action definition

```json
{
  "implementation": {"registrationId": "sales-script-v1"},
  "parameters": {"minimum": {"parameter": "minimum"}},
  "inputs": {"sales": {"sourceId": "sales-input", "version": 1}},
  "outputs": {"filtered": "table", "monthly": "image"},
  "outputGroup": "sales-results",
  "targets": {"filtered": "sales", "monthly": "revenue"}
}
```

All fields are required. `parameters`, `inputs`, `outputs`, and `targets` are maps. Output names and parameter names use entity ID syntax. Output kinds use the source kinds. `outputGroup` also uses entity ID syntax.

Every declared output MUST have exactly one target projection. Every target MUST be compatible with that output kind. An action MUST NOT target the same projection twice.

In v0.1, one action owns each output group. A projection can belong to at most one output group. Compilation rejects competing writers.

### 7.2 Trusted registration

A registration is service-owned. It contains:

- Registration ID and immutable registration revision.
- A captured script artifact and its content hash.
- The project root and working-directory policy.
- The resolved Python environment fingerprint.
- Input source kinds and parameter types with constraints.
- Declared output kinds.
- Required execution permission.
- Replay classification, fixed to `nonReplaySafe` for arbitrary Python.

Registration is separate from granting execution permission. Registering a script does not run it. Editing a script produces a new registration. A permission grant may cover it only under an explicit project trust policy.

An action's mappings MUST exactly satisfy the registration contract. Parameter constraints MUST fit within the registration's accepted values. Reject ambiguous constraint compatibility; do not silently narrow runtime values.

Unknown registrations make executable publication invalid. During restoration, unavailable registrations instead disable the action. Saved projections remain readable.

### 7.3 Invocation snapshot

Admission captures the registration revision, script hash, environment fingerprint, input source references, parameter values and revisions, action definition revision, output group revision, generation, and permission decision reference.

The runner receives this immutable snapshot. Changes to the workspace cannot change an admitted run's inputs.

Run states are `queued`, `running`, `succeeded`, `failed`, `cancelled`, `interrupted`, and `unknown`. Store publication disposition separately: `none`, `current`, `superseded`, or `rejected`.

Only a successful process with valid, complete outputs can publish. Exit status zero alone is insufficient.

## 8. Projections

### 8.1 Shared fields

Each projection requires `kind`, `title`, `source`, `config`, and `controls`.

`source` is a source reference or null. Null displays an empty result state. Null is allowed only if an action targets the projection. A failed or pending action must not erase an existing source.

A projection contains no executable code. Its source and config must match its renderer contract.

### 8.2 Table

```json
{
  "kind": "table",
  "title": "Sales",
  "source": null,
  "config": {"columns": ["month", "revenue"], "selection": "none"},
  "controls": []
}
```

`columns` is null for all columns or an ordered list of distinct column IDs. `selection` is `none` or `single`. Single selection requires a source with a valid `rowKey`. When the source is null, validate this requirement again at publication.

The host supports bounded pages, local selection feedback, and service-backed sorting and filtering. View settings persist in host records keyed by projection ID. They do not invoke a model or rewrite action parameters.

A source replacement keeps compatible sort/filter settings. Removed columns clear affected settings and produce a visible notice. Selection survives only when the new version has the same key column and contains the selected key.

Page requests contain a source reference, ordered sort keys, filters, offset, and limit. Filters support `eq`, `lt`, `lte`, `gt`, `gte`, `contains`, and `isNull`. Validate operators against column types. Compare strings by Unicode code point. Sort nulls last and use original row position as the final tie-breaker. Apply filters before sorting and paging.

Responses identify the source reference and query hash. The host discards responses for an obsolete query. Bulk table data never travels in document updates.

### 8.3 Plot

A plot requires an image source and `config: {"fit":"contain"}`. It presents a PNG, supports zoom and export, and exposes the generating run. It does not embed a browser or execute image metadata.

### 8.4 Document

A document requires a text source and `config: {"format":"plain"}` or `{"format":"markdown"}`. Markdown rendering disables raw HTML, remote image loading, and executable links. Opening an external link requires a user action.

### 8.5 Controller continuity

Preserve native controllers when projection ID and kind remain unchanged. Updating a source must not rebuild the whole pane tree.

Changing a projection kind requires delete and create operations in one transaction. The host protects pinned panes and active edits through its command policy. A dry run reports controller replacement before commit.

## 9. Events and bindings

### 9.1 Supported events

| Event | Address | Payload |
|---|---|---|
| `parameter.changed` | Parameter ID | Previous value, committed value, state revision |
| `control.activate` | Projection ID and button ID | No application data |

Table row selection is native view state in v0.1. Routing it into actions is deferred.

Events carry an ID, workspace ID, origin, and relevant definition revisions. Event IDs are service-issued or deduplicated request IDs. Origin is `user` or `programmatic` and derives from the authenticated caller and command type.

### 9.2 Binding definition

```json
{
  "event": {"kind": "parameter.changed", "parameter": "minimum"},
  "effect": {"kind": "action.invoke", "action": "analyze-sales"},
  "debounceMs": 250
}
```

The alternate event shape is `{"kind":"control.activate","projection":"revenue","control":"run"}`. Only `action.invoke` is supported as an effect.

`debounceMs` is required and ranges from 0 through 5000. Button bindings require zero. A button can have at most one binding. At most one binding may connect a given parameter to a given action.

Bindings observe committed user changes only. Programmatic changes, restoration, defaults, and output publication do not emit user-change events. They cannot start recursive runs.

### 9.3 Debounce and ordering

The service owns debounce timers. A qualifying event immediately reserves a new generation for its output group. It also makes older runs ineligible for current publication. Do not wait until the timer fires to invalidate older work.

Repeated events replace the pending request and restart its timer. Persist the newest pending intent. Restart marks it interrupted; it does not automatically execute it.

A user action captures all mapped parameter values when its pending intent is updated. On dispatch, reject stale action definitions and recheck authority. Do not silently substitute a different script or action.

Invalid parameter edits do not commit, reserve a generation, or run an action. Unbound parameter changes do not reserve a generation, but still invalidate publication if they change an action's captured dependencies.

## 10. Layout and native state

A layout contains `root` and `hidden`. The root is null, a leaf, or a split:

```json
{
  "root": {
    "kind": "split",
    "axis": "horizontal",
    "ratio": 0.55,
    "first": {"kind": "pane", "projection": "sales"},
    "second": {"kind": "pane", "projection": "revenue"}
  },
  "hidden": []
}
```

A horizontal split places `first` to the left. A vertical split places `first` above. Ratios range from 0.1 through 0.9, inclusive. Maximum depth is eight.

Every projection appears exactly once, either in the tree or in `hidden`. Duplicate placement and unknown IDs are errors. Hidden projections keep their state and can receive results. The host may suspend their rendering without suspending their actions.

The host enforces a minimum pane size of 240 by 160 logical points. If the window cannot fit the saved tree, show an overflow selector. Do not discard or rewrite the saved layout to fit a smaller window.

Focus, hover, scroll offsets, and open menus are host-local. Table sort/filter settings, keyed selection, and pane pinning persist in separate host records. A selection record includes its source reference and row key. Validate it again on restoration. Layout geometry persists through revision-checked layout commands.

Publication MUST NOT change layout. Python's initial placement helper applies only when all referenced panes are first created. A mixed creation/update request must use a separate explicit layout command.

## 11. Compiler

### 11.1 Inputs and outputs

Inputs are the document, source catalog, action registry, renderer registry, compiler version, and current parameter state for patch validation. Permission grants are not compiler inputs.

The compiler returns either diagnostics or an immutable plan containing:

| Plan | Contents |
|---|---|
| Render | Renderer kind/version, source reference, configuration, controls |
| State | Parameter definitions, defaults, and validation rules |
| Interaction | Event addresses, bindings, and debounce policy |
| Action | Resolved registrations, mappings, targets, and required permissions |
| Dependency | References used for invalidation and incremental compilation |
| Identity | Projection-to-pane and control-to-parameter mappings |
| Layout | Validated tree and hidden projection IDs |

A plan is derived data. Persist the document and state, not native pointers or compiler objects.

### 11.2 Stages

1. Decode and reject malformed or oversized input.
2. Validate structural schemas and language version.
3. Resolve source, parameter, action, control, and layout references.
4. Check types, output ownership, and renderer compatibility.
5. Validate state continuity and native policy constraints.
6. Build dependency indexes and compiled plans.
7. Return the plan and deterministic diagnostics.

Compilation performs no project execution, network access, package installation, or permission grant. It may read trusted catalog metadata. Artifact validation occurs during import or publication.

Equivalent canonical inputs and registry versions MUST produce equivalent plans. Native pixel output may vary with display scale, fonts, and operating-system rendering.

### 11.3 Diagnostics

```json
{
  "code": "E_REFERENCE",
  "path": "/actions/analyze-sales/inputs/sales",
  "message": "Source version does not exist.",
  "relatedPaths": [],
  "hint": "Import the source or use an existing version."
}
```

Paths use JSON Pointer. Sort diagnostics by path, then code. Never include secrets or full source contents.

Required codes include `E_PARSE`, `E_VERSION`, `E_SCHEMA`, `E_LIMIT`, `E_REFERENCE`, `E_TYPE`, `E_LAYOUT`, `E_OUTPUT_OWNER`, `E_STATE_MIGRATION`, `E_REGISTRATION`, `E_ARTIFACT`, `E_CONFLICT`, `E_PERMISSION`, and `E_REQUEST_REUSE`.

The command-line interface provides `melt validate <file>` and `melt compile <file> --catalog <file>`. Validation exits with 0 on success, 1 for invalid input, and 2 for operational failure. `--json` emits structured results. Full compilation requires catalog and registry fixtures; structural validation alone must identify its limited coverage.

## 12. Commands, patches, and concurrency

### 12.1 Command envelope

```json
{
  "protocolVersion": 1,
  "workspaceId": "sales-demo",
  "requestId": "01-request-example",
  "method": "workspace.apply",
  "expected": {"parameters/minimum": 1, "layout": 1},
  "operations": []
}
```

Request IDs are opaque strings of 1–128 characters. They are unique within a workspace. The transport supplies caller identity and authority; document fields cannot impersonate a caller.

Use a private framed interprocess communication (IPC) channel between the native host and service. Frames use a four-byte unsigned big-endian byte length followed by UTF-8 JSON. Reject frames above the configured limit before allocation. Project stdout is never a control channel.

### 12.2 Mutation operations

Use a typed operation list, not unrestricted JSON Patch.

| Operation | Fields beyond `op` | Behavior |
|---|---|---|
| `entity.create` | `collection`, `id`, `value` | Create an absent entity |
| `entity.replace` | `collection`, `id`, `value` | Replace an existing definition |
| `entity.delete` | `collection`, `id` | Remove an entity if final references remain valid |
| `parameter.set` | `id`, `value` | Set a parameter value |
| `layout.replace` | `value` | Replace the full layout |

Entity collections are `parameters`, `actions`, `projections`, and `bindings`. Sources enter through `source.import` or publication. No generic patch can mutate artifact catalogs or run records.

Evaluate operations in order against a private candidate. Validate the final candidate once. Intermediate references may be incomplete. Commit all changes or none. A transaction may contain a delete and create for a projection kind change.

`expected` maps touched record paths to revisions. Use zero for expected absence. State writes use `state/<parameter-id>`. Each replaced, deleted, or created record requires an expectation. References resolved during compilation form an additional read set. The service checks that read set again at commit.

Unrelated transcript or run-log changes do not cause a conflict. An intervening mutation to a compiler dependency does. Return current conflicting revisions without applying any operations.

### 12.3 State migrations

Changing a parameter type or constraints requires its current value to remain valid. Otherwise, the same transaction MUST supply `parameter.set` with a valid replacement and the expected state revision.

Deleting a parameter requires removal of all references in the same transaction. Creating it again uses its new default and a new identity incarnation internally. Old events cannot target the new incarnation.

A title change preserves state. A new source preserves compatible view state. An action change invalidates pending and running publication eligibility. Removing a target projection invalidates the entire output group publication.

### 12.4 Dry run and inspection

`workspace.inspect` returns the bounded document, entity revisions, parameter state, active runs, and diagnostics. Optional collection and ID filters limit the response.

`workspace.preview` accepts the same mutation payload as `workspace.apply`. It returns diagnostics, affected IDs, controller replacements, state resets, invalidated runs, and a candidate document hash. It performs no writes and grants no permissions.

Preview does not reserve revisions. Applying after preview still requires a successful concurrency check.

### 12.5 Receipts and notifications

Store the request payload hash and response atomically with each successful mutation. A retry with the same ID and payload returns the stored response. Reuse with different content returns `E_REQUEST_REUSE`.

Retain mutation receipts for the workspace lifetime in v0.1. A future bounded retention policy requires explicit reconciliation behavior.

Each transaction increments the commit sequence once. Changed records increment their own revision once. Rejected requests do neither.

Notifications contain a service epoch, commit sequence, and changed records. The host applies them in order. A gap or changed epoch requires a full snapshot. Subscribe before taking a snapshot, then discard queued changes at or below its sequence.

### 12.6 Service methods

All requests use the envelope from section 12.1. Methods other than `workspace.apply` use a `payload` field instead of `operations`. Only methods with revision preconditions use `expected`.

| Method | Required payload fields | Result |
|---|---|---|
| `session.hello` | `protocolVersions`, `languageVersions`, `clientVersion` | Selected versions, epoch, limits |
| `workspace.create` | `document` | Initial snapshot |
| `workspace.inspect` | None; optional `collection`, `ids` | Filtered snapshot |
| `workspace.preview` | Same candidate fields as `workspace.apply` | Diagnostics and effects |
| `source.import` | `sourceId`, `artifactToken`, `originLabel` | New source reference |
| `action.register` | `scriptToken`, `environmentId`, `contract` | Registration ID and revision |
| `action.invoke` | `actionId` | Admitted intent or approval-required result |
| `control.activate` | `projectionId`, `controlId` | Admitted intent or no-op |
| `run.cancel` | `runId` | Current run state and cancellation status |
| `run.inspect` | `runId` | Run record and output references |
| `table.page` | Source reference, sort, filters, offset, limit | Bounded page |
| `workspace.subscribe` | `afterSequence`, `epoch` | Ordered changes or snapshot-required result |

`workspace.preview` uses the same `expected` and `operations` fields as `workspace.apply`, rather than a nested payload. The table describes its conceptual input. `workspace.create`, import, registration, invocation admission, and cancellation also use durable request receipts.

Artifact and script tokens are opaque, service-issued handles from a host import operation. They are scoped to the workspace and expire after 15 minutes. Creating a token captures and validates bytes. A client cannot supply an arbitrary filesystem path in a document command.

`source.import` requires the expected source catalog revision, or zero for a new source. The artifact token provides the verified kind, schema, hash, and length. `action.register` requires a captured script token and an environment already known to the service. Its contract contains `parameters`, `inputs`, and `outputs`; it cannot declare a permission grant.

`workspace.create` is valid only for an absent workspace. In v0.1, its document must have empty `sources` and `actions` collections. This keeps creation separate from artifact import and execution registration. Full saved-document restoration uses the storage recovery path, not this command. The saved document contains resolved references, never temporary tokens.

For the simpler interactive path, create an empty workspace, import sources, register the script, and apply definitions. The full example in section 16 represents the resulting document, not an unrestricted source registration command.

`action.invoke` requires `expected["actions/<id>"]`. `control.activate` requires the projection and matching binding revisions. `run.cancel` is idempotent and does not require an expected revision. Admission deduplicates both manual and bound invocations using the request receipt. Permission decisions use a separate authenticated native endpoint and are never ordinary document mutations.

A successful response has `requestId`, `ok: true`, `commitSequence`, and `result`. A rejected response has `requestId`, `ok: false`, and `errors`. Each error uses the diagnostic shape. Reads return the sequence they observed. Approval-required is a successful admission result with `status: "approvalRequired"`, not a permission grant.

An approval applies to the captured intent. If its generation or definitions change before approval, expire that intent. Require a new admission and show its captured values. Never execute a newer request under approval for an older one.

### 12.7 Table page contract

A page request contains:

- `source`: an immutable source reference.
- `sort`: an array of `{column, direction}`, where direction is `asc` or `desc`.
- `filters`: an array of `{column, operator, value}`; `isNull` omits `value`.
- `offset`: a nonnegative safe integer.
- `limit`: an integer from 1 through 256.

Combine filters with logical AND. `contains` accepts only strings. Order comparisons accept numbers, dates, timestamps, and strings. `eq` accepts any non-null value of the column type. Null comparisons use `isNull`. A non-null comparison against a null cell is false.

A page response contains `source`, `queryHash`, `columns`, `rows`, `offset`, `nextOffset`, and `hasMore`. Rows are arrays in the returned column order. `nextOffset` is null at the end. The query hash covers the source, sort, and filters; it excludes offset and limit.

If a table exceeds 128 columns, return an explicit column-selection requirement. Do not choose columns silently. Byte limits may reduce row count. The reader must return at least one display row or a structured limit error; it must never return an endless empty page with `hasMore: true`.

## 13. Running and publishing results

### 13.1 Scheduling

`action.invoke` contains an action ID and its expected revision. A manual invocation reserves a generation just like a bound event. `run.cancel` contains a run ID.

Reserve generations in the same transaction that admits an intent or commits a triggering user change. When several bindings in one transaction target the same action, coalesce them into one intent using the final parameter values. A generation is never reused.

Only one project execution runs at a time per workspace. Keep at most one pending request per output group. The trusted table reader can run separately.

At admission and immediately before launch, check permission and registration availability. A missing grant produces an approval-required state in the host. It must not execute or falsely report failure as successful completion.

Execution uses a fresh process in the resolved project environment. Do not install packages as part of invocation. Environment repair is a separate operation.

### 13.2 Python runner contract

The runner receives a versioned manifest through a dedicated file or descriptor. The manifest includes the run ID, captured parameters, input artifact paths, declared outputs, and a private staging directory.

The SDK exposes:

- `ui.params`: read-only captured parameter values.
- `ui.input_path(name)`: path to the captured input artifact.
- `ui.output(name, value)`: stage a declared output.

`ui.output` accepts a pandas DataFrame for a table, a matplotlib figure for an image, and a string for text. It materializes Parquet, PNG, or UTF-8 bytes. Reject duplicate or undeclared output names. The service validates actual files independently.

```python
import pandas as pd
import matplotlib.pyplot as plt
from melt import ui

sales = pd.read_parquet(ui.input_path("sales"))
selected = sales[sales["revenue"] >= ui.params["minimum"]]
monthly = selected.groupby("month", sort=True)["revenue"].sum()
fig, ax = plt.subplots()
monthly.plot(ax=ax)
ax.set_ylabel("Revenue")
ui.output("filtered", selected)
ui.output("monthly", fig)
plt.close(fig)
```

The first SDK separates definition authoring from run output. A later `ui.show` helper may build definitions, but cannot bypass this publication contract. A run cannot change its own action, bindings, permissions, or layout.

### 13.3 Publication eligibility

A completed run is eligible to become current only when all conditions hold:

- The process succeeded and all declared outputs validate.
- The run is not cancelled or interrupted.
- Its generation equals the output group's newest generation.
- Its action, registration, and output group revisions remain current.
- Its captured parameter revisions remain current.
- Its input references remain the action's current input references.
- Every target projection still exists with the expected kind and ownership.
- Execution permission has not been revoked.

The service maintains an output group revision starting at 1. Increment it when targets, target kinds, output contracts, or ownership change. An action definition replacement increments its action revision and invalidates its admitted runs, even when the replacement appears equivalent.

Capture definition dependencies precisely. A projection title or layout change does not invalidate a result. A target kind, action mapping, or input change does.

A successful but obsolete run remains in history with `superseded` disposition. It cannot overwrite current sources. The host can show its captured parameters and artifacts through run inspection.

### 13.4 Atomic publication

1. Verify declared files, types, hashes, sizes, and output completeness.
2. Synchronize staged files and finalize immutable artifact paths.
3. Recheck eligibility within the owning service transaction.
4. Allocate source versions and update all target references together.
5. Store run provenance, disposition, and the publication receipt.
6. Commit and acknowledge before showing a saved result.

Output source IDs are service-owned mappings from `(action ID, output name)` to a stable logical source. They are allocated on first publication. Clients must not derive them from titles.

If eligibility fails, retain the result only as historical output. If finalization succeeds but the transaction fails, files may remain unreferenced. Recovery removes or reconciles them later.

A missing or invalid output rejects the entire publication. The previous table and plot remain together. Publication does not claim to undo external effects from Python.

### 13.5 Result status

Each output group exposes `empty`, `pending`, `current`, `stale`, or `error` status. Choose `pending` when an active or approval-required intent exists, then `error` when the latest intent failed, then `empty` when no result exists, then `stale` when captured dependencies differ, otherwise `current`. Cancellation of the newest intent is an error state with a cancellation reason. A separate `resultIsStale` boolean remains available in every status. Pending and error states may retain an earlier result. Always show the parameter values associated with that result when they differ from current controls.

Cancellation requests stop scheduling and signal the owned process group. Escalate termination when necessary. Revocation blocks publication immediately and requests cancellation. Neither operation claims that external effects were reversed.

## 14. Persistence and recovery

The service is the sole writer to each workspace database. Enforce ownership through an operating-system lock. Python never writes authoritative workspace state.

Store documents, entity revisions, parameter state, host records, runs, generations, receipts, and publication references in the durable store. Keep artifact bytes outside the database.

```text
<application-data-root>/melt/workspaces/<workspace-id>/
  state.sqlite
  owner.lock
  artifacts/<sha256-hex>
  staging/<run-id>/
  logs/<run-id>/
  backups/
```

On Linux, the application data root is `$XDG_DATA_HOME`, or `$HOME/.local/share` when unset. On macOS, use `~/Library/Application Support`. Tests use an isolated temporary root. Resolve paths through a platform adapter.

Use the Pi Durable storage adapter behind an application-owned interface. Prove atomic updates and committed observation with integration tests. If the pinned adapter cannot provide them, resolve that integration before implementing competing persistence paths.

Configure and verify SQLite durability settings on the owning connection. Require `synchronous=FULL` on Linux and macOS. On macOS, also configure and verify supported full-sync behavior. Linux validation must not depend on macOS-specific settings. Test file and directory synchronization for artifact publication. Record the tested filesystem and crash model.

On startup:

1. Acquire ownership and negotiate supported stored versions.
2. Load the last committed definitions, state, and source references.
3. Validate saved data without importing or executing project code.
4. Restore readable projections and report missing artifacts individually.
5. Mark previous active jobs interrupted or unknown, as appropriate.
6. Reconcile staging files and publication receipts.
7. Offer explicit rerun or recovery actions.

An unavailable environment or model does not block saved views. An unknown language version opens a recovery screen and preserves files. Do not silently migrate, reset, or overwrite the database.

Quit stops scheduling, flushes acknowledged state, and terminates owned workers. Work does not continue through a hidden background service.

## 15. Permissions and limits

### 15.1 Authority

Document validity is not execution authority. Only host-generated permission controls can grant permission. Source text, model output, and terminal output cannot grant it.

Support a specific-run grant and an explicit project execution grant. Store grants outside exported documents. Recheck grants before execution and publication.

Unrestricted Python runs with the user's operating-system authority. A virtual environment does not contain filesystem or network access. This milestone does not promise safe execution of hostile code.

Do not pass model credentials to project processes. Restrict built-in artifact readers to approved immutable files. Validate paths without trusting project-provided symlinks.

### 15.2 Default limits

| Resource | Limit |
|---|---|
| Control frame | 1 MiB |
| Operations per mutation | 128 |
| Projections | 32 |
| Parameters | 256 |
| Actions | 64 |
| Bindings | 256 |
| Controls per projection | 32 |
| Layout depth | 8 |
| Table page | 256 rows, 128 columns, and 1 MiB encoded bytes |
| Native table cache | 64 MiB |
| PNG dimensions | 16,384 per axis and 64 million total pixels |
| Displayed text artifact | 10 MiB; larger documents require explicit partial reading |
| Run log | 10 MiB per stream with explicit truncation |
| Staged output bytes per run | 1 GiB by default |

Limits are configurable service policy, not fields that project code can raise. Return `E_LIMIT` with the resource and allowed bound. Never silently drop output columns or publish partial results.

Large table cells may be truncated for display only. Mark truncation and provide bounded full-value retrieval. Reader work, image decoding, compilation, and database access must stay off the native main thread.

## 16. Complete workspace example

This document starts with an imported table and empty result panes. The fixture generator substitutes an actual artifact hash. The trusted catalog contains `sales-script-v1` with the contract from section 7.

```json
{
  "language": "melt/v0.1",
  "workspaceId": "sales-demo",
  "title": "Sales analysis",
  "sources": {
    "sales-input": {
      "kind": "table",
      "currentVersion": 1,
      "versions": {
        "1": {
          "artifact": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
          "mediaType": "application/vnd.apache.parquet",
          "byteLength": 4200,
          "schema": {
            "columns": [
              {"id": "month", "type": "string", "nullable": false},
              {"id": "revenue", "type": "float64", "nullable": false}
            ],
            "rowKey": null
          },
          "origin": {"kind": "import", "label": "sales.csv"}
        }
      }
    }
  },
  "parameters": {
    "minimum": {
      "type": "number",
      "default": 0,
      "nullable": false,
      "constraints": {"minimum": 0, "maximum": 1000000000}
    }
  },
  "actions": {
    "analyze-sales": {
      "implementation": {"registrationId": "sales-script-v1"},
      "parameters": {"minimum": {"parameter": "minimum"}},
      "inputs": {"sales": {"sourceId": "sales-input", "version": 1}},
      "outputs": {"filtered": "table", "monthly": "image"},
      "outputGroup": "sales-results",
      "targets": {"filtered": "sales", "monthly": "revenue"}
    }
  },
  "projections": {
    "sales": {
      "kind": "table",
      "title": "Sales",
      "source": null,
      "config": {"columns": ["month", "revenue"], "selection": "none"},
      "controls": []
    },
    "revenue": {
      "kind": "plot",
      "title": "Revenue by month",
      "source": null,
      "config": {"fit": "contain"},
      "controls": [
        {"id": "minimum", "kind": "number", "label": "Minimum revenue", "parameter": "minimum"},
        {"id": "run", "kind": "button", "label": "Run analysis"}
      ]
    }
  },
  "bindings": {
    "minimum-rerun": {
      "event": {"kind": "parameter.changed", "parameter": "minimum"},
      "effect": {"kind": "action.invoke", "action": "analyze-sales"},
      "debounceMs": 250
    },
    "manual-run": {
      "event": {"kind": "control.activate", "projection": "revenue", "control": "run"},
      "effect": {"kind": "action.invoke", "action": "analyze-sales"},
      "debounceMs": 0
    }
  },
  "layout": {
    "root": {
      "kind": "split",
      "axis": "horizontal",
      "ratio": 0.55,
      "first": {"kind": "pane", "projection": "sales"},
      "second": {"kind": "pane", "projection": "revenue"}
    },
    "hidden": []
  }
}
```

Creating this document does not run the action. The user starts the first run. A later committed parameter change triggers the bound action under the execution policy.

## 17. Required implementation artifacts

```text
python/melt/
  schema/                 # Pydantic document and command definitions
  sdk/                    # Input access and output materialization
  runner/                 # Captured execution manifest handling
runtime/
  compiler/               # Validation, resolution, plans, diagnostics
  workspace/              # Commands, revisions, receipts, snapshots
  execution/              # Registry, generations, jobs, publication
  storage/                # Pi adapter and artifact store
native/
  projections/            # Table, plot, document, controls
  workspace/              # Stable controllers and layout adapter
protocol/
  schema/                 # Generated JSON Schema
  generated/              # TypeScript and Swift models
fixtures/
  valid/
  invalid/
  sales/
tests/
  contract/
  recovery/
  native/
  integration/
```

Generate TypeScript and Swift models from the restricted schema vocabulary. Do not author three independent schemas. Swift decoding alone is not semantic validation.

The compiler's authoritative semantic implementation lives in the service. Python validates authored shapes before submission. Native code checks transport, renderer limits, and local presentation constraints. Shared fixtures detect disagreement.

Persisted records and commands require their own versioned schemas. Include RunRecord, RegistrationRecord, ArtifactRecord, ParameterState, CommandReceipt, Snapshot, ChangeNotification, PageRequest, and PageResponse.

Document every generated file and provide one repeatable generation command. Continuous integration MUST fail when regeneration changes committed output.

## 18. Acceptance tests

### 18.1 Contract fixtures

Every invalid fixture includes an expected diagnostic code and JSON Pointer. Cover:

- Unknown fields, duplicate JSON keys, invalid IDs, and unsupported versions.
- Non-finite numbers, unsafe integers, invalid defaults, and wrong parameter types.
- Missing source versions, missing controls, and incompatible renderer kinds.
- Duplicate pane placement, missing panes, excessive depth, and invalid ratios.
- Competing output writers and incomplete action mappings.
- Invalid state migration and missing expected revisions.
- Registry mismatches and an artifact schema that differs from its declaration.

Valid fixtures include the empty workspace, every projection kind, hidden panes, nullable parameters, a state migration, and the full sales example. Verify canonical hashes across supported languages.

### 18.2 Hero workflow

In round 1, drive these steps through the Linux command-line harness. Inspect compiled plans, table pages, PNG artifacts, and saved state. Moving a pane means submitting a layout command and verifying the saved tree. Round 2 repeats the workflow through native controls.

Use an input with rows `(2026-01, 100)`, `(2026-01, 300)`, and `(2026-02, 200)`. At minimum zero, the output table has three rows. At minimum 200, it has two rows. Monthly totals become 300 and 200.

Verify these steps:

1. Import the input and create the document without executing it.
2. Approve and run the action; publish both outputs together.
3. Set minimum to 200; verify zero model calls.
4. Move a pane while execution runs; preserve that layout after publication.
5. Restart offline; restore values, bindings, sources, and layout without execution.
6. Invoke again explicitly and verify the captured input and parameter values.

### 18.3 Race tests

| Scenario | Expected result |
|---|---|
| Run A finishes after parameter change but before debounce fires | A cannot become current |
| Three parameter changes occur during a run | One newest pending request remains |
| A newer run fails | Keep the last good result and show its older parameters |
| Action definition changes during execution | Archive old output; require a new invocation |
| Target pane is hidden | Publication remains valid |
| Target pane is deleted | Reject the entire current publication |
| Layout or title changes | Publication remains valid |
| Parameter changes and changes back | Old state revisions still prevent old publication |
| Programmatic parameter update | Save value, emit no user event, mark affected result stale |
| Duplicate activation request | Admit at most one run |
| Permission is revoked during execution | Block publication and request cancellation |

### 18.4 Fault injection

Kill the service before and after artifact finalization, database commit, and acknowledgement. Verify that acknowledged references always point to complete files. Unacknowledged work may exist, but retries must reconcile it.

Also test disk-full errors, missing artifacts, missing environments, lost notifications, changed service epochs, reused request IDs, corrupt descriptors, cancelled runs, and incompatible saved versions.

Never automatically rerun uncertain Python effects after restart. Preserve the previous readable interface when recovery fails.

### 18.5 Native integration — round 2

Verify terminal controller identity while moving and updating Melt panes. Test focus, keyboard navigation, clipboard, input methods, accessibility labels, pane hiding, and small-window overflow.

Test that table paging and Python computation do not block native input. Verify that model disconnection does not affect local controls or saved result inspection.

## 19. Performance targets

For round 1, measure compilation, service commits, artifact publication, and table paging on a declared Linux machine with 16 GiB memory. Record hardware, dependency versions, build mode, fixture sizes, and warm/cold conditions.

Round 2 measures native feedback, visible updates, and pane restoration on a declared Apple Silicon machine with 16 GiB memory. Native targets do not block completion of the Linux round.

| Measurement | Target |
|---|---|
| Compile a 32-projection document with cached catalogs | p95 below 50 ms |
| Local control feedback | p95 below 50 ms |
| Small committed patch to visible update | p95 below 100 ms |
| Restore five saved panes | Below 2 seconds |
| Warm first table page for 1 million rows and 20 columns | p95 below 300 ms |

These are targets, not measured results. Exclude model and Python execution time from patch latency. Report full-table sorting separately. A missed target requires investigation; it does not justify weaker persistence or incorrect results.

## 20. Delivery sequence

1. **Linux setup and schema:** Pin the Linux toolchain. Implement structural models, canonicalization, diagnostics, and the complete sales fixture.
2. **Compiler:** Resolve references and produce plans with deterministic valid/invalid tests.
3. **Service:** Implement revisions, atomic mutations, preview, receipts, and snapshots through the Pi adapter.
4. **Execution:** Register the sales script, capture invocations, and publish validated output groups through the Linux harness.
5. **Linux recovery gate:** Pass the hero workflow, contract suite, race suite, and publication fault tests. Record compiler and reader measurements.
6. **Native integration:** Render table, plot, document, controls, and stable layout beside a Ghostty terminal on macOS.
7. **Application release gate:** Repeat the workflow through native controls. Pass native regressions, recovery tests, performance targets, and clean-machine packaging checks.

Probe Pi atomic storage and committed observation early on Linux. Defer the Ghostty mixed-pane probe to round 2. The Linux gate requires no native application build.

Round 1 is complete when the Linux harness proves the full language and execution loop, including patches and restart recovery. A valid JSON document alone does not pass. The native application milestone remains incomplete until steps 6 and 7 pass.

## 21. Decisions reserved for later versions

The following require new language versions or explicit extensions: custom renderers, general expressions, object graphs, multi-source joins in projections, event-driven state reducers, additional parameter scopes, automatic migrations, portable permission policies, and distributed editing.

Do not reserve speculative fields in v0.1. Add features when a concrete workflow demonstrates the need and includes validation, persistence, and recovery semantics.
