# Pi Durable synchronous option proposal

Status: local draft, backlog task T033. Not filed upstream. This task
files no issue, contacts no maintainer, and pushes no change.
Publication is optional future work. It does not gate the Linux round,
the semantic-extension round, or the native macOS round.

Audience: maintainers of `@earendil-works/pi-durable`. The draft was
written and reviewed inside Melt first, against the pinned adapter and
the committed probe evidence.

## Summary

The pinned adapter opens every Node SQLite connection with
`PRAGMA synchronous = NORMAL`. The value is hard-coded. The options type
offers no field for it. SPEC.md section 14 requires `synchronous=FULL`
on the owning connection, so Melt applies and verifies FULL in an
application-owned wrapper after every open.

This draft proposes an optional `synchronous` field on
`NodeSqliteDatabaseOptions`. The default stays `NORMAL`, so existing
callers keep their behavior. The draft states the compatibility rules,
the test cases, and the conditions under which Melt would adopt the
option.

## Pinned version and evidence base

| Item | Value |
|---|---|
| Package | `@earendil-works/pi-durable` |
| Pinned version | `1.3.0`, exact, no range |
| Lock | `runtime/storage/pi_adapter/package-lock.json` |
| Run-time pin assertion | `PI_DURABLE_PIN` in `pi_storage.mjs`; `PINNED_ADAPTER` in the pytest wrapper |
| Entry point used | `@earendil-works/pi-durable/storage/sqlite/node` |
| Upstream source | `src/storage/sqlite/node.ts`, shipped as `dist/storage/sqlite/node.js` |
| Probed on | Node v24.18.0, Linux 6.8.0-147-generic, ext4 |
| Also inspected | Version `1.0.4` in a local checkout of the upstream monorepo |

Every claim below was re-verified against this pin on 2026-10-18: the
probe passed 26 of 26 cases in about 3 seconds, and
`tests/integration/test_pi_durable_storage.py` passed 9 of 9 tests.
`make check` gates on both, so the evidence cannot rot silently.

## The hard-coded NORMAL default

`openNodeSqliteDatabase` in the shipped 1.3.0 build sets the durability
pragmas on every connection it opens. The statement at
`dist/storage/sqlite/node.js:162` is:

```ts
export async function openNodeSqliteDatabase(path, options = {}) {
	const checkpointPages = options.walAutoCheckpointPages ?? DEFAULT_WAL_AUTO_CHECKPOINT_PAGES;
	const timeout = options.busyTimeoutMs ?? DEFAULT_BUSY_TIMEOUT_MS;
	if (path !== ":memory:") await mkdir(dirname(path), { recursive: true });
	const database = new DatabaseSync(path, { timeout });
	const adapter = new NodeSqliteDatabase(database);
	try {
		await adapter.exec("PRAGMA journal_mode = WAL");
		await adapter.exec("PRAGMA synchronous = NORMAL");
		await adapter.exec(`PRAGMA wal_autocheckpoint = ${checkpointPages}`);
		return adapter;
	} catch (error) {
		/* close the adapter and rethrow */
	}
}
```

The options type has no field for the pragma:

```ts
export type NodeSqliteDatabaseOptions = {
	readonly walAutoCheckpointPages?: number;
	readonly busyTimeoutMs?: number;
};
```

Three facts follow:

- The raw `node:sqlite` default is FULL (2). The adapter lowers it to
  NORMAL (1) on every connection. This was verified directly against
  `node:sqlite` v24.18.0.
- The default is long-standing, not new in 1.3.0. Version 1.0.4 sets the
  same statement at `packages/durable/src/storage/sqlite/node.ts:191` in
  the local source checkout.
- In write-ahead log (WAL) mode, NORMAL keeps the database consistent
  across application crashes, but a power loss or an operating-system
  crash can roll back the latest commits. FULL also syncs the WAL at
  each commit. That difference is why SPEC.md section 14 requires FULL
  for the owning connection.

## The Melt FULL override

`openDurableStorage` in `runtime/storage/pi_adapter/pi_storage.mjs` is
the only sanctioned open path. No Melt code opens the adapter directly.
The function does the following:

1. Assert the installed adapter matches `PI_DURABLE_PIN` (`1.3.0`).
2. Open the database with `openNodeSqliteDatabase`, exactly as shipped.
3. Read `PRAGMA synchronous`. Apply `PRAGMA synchronous = FULL` when the
   read value is not FULL already.
4. Run `SqliteStorage.open`, which performs the adapter migrations.
5. Read back `journal_mode` and `synchronous`. Throw
   `StorageDurabilityError` unless WAL and FULL hold.
6. Return the storage, the connection, and the recorded facts.

The setting is per connection, so every open applies and verifies it
again. The probe reports a finding,
`adapter-default-synchronous-normal`, on every run. The pytest wrapper
asserts `synchronousDefault == 1` with the message "adapter default
changed; update the probe and README". A future adapter release
therefore cannot change durability behavior silently.

## Probe evidence

The committed probe (`runtime/storage/pi_adapter/probe.mjs`) proves the
following on the machine listed above:

- The shipped default reads `1` on a fresh connection, and a fresh
  connection after a close reads `1` again.
- The wrapper moves the owning connection to `2` in WAL mode, and the
  setting survives migrations, commits, and a WAL checkpoint.
- A 21-commit run (root conversation plus 20 entries) produced 30
  `fsync` and `fdatasync` calls under FULL and 8 under the shipped
  default, measured under `strace`. FULL adds at least one sync per
  commit; the default syncs at checkpoints only.
- Every SIGKILL round preserved all acknowledged commits, whole and
  contiguous, with `PRAGMA integrity_check` reporting `ok`.
- The adapter's own conformance suite,
  `createStorageConformance`, passed 24 of 24 cases through the wrapper
  with FULL applied.

One honest limit: process death does not distinguish the two modes. The
contrast round with the shipped default also survives SIGKILL, because
WAL frames already handed to the operating system survive in both modes
(probe finding F2). Only the pragma read-back and the sync counts show
that the setting changes write behavior. No hardware power-loss test was
performed.

## The owning-connection requirement

SPEC.md section 14 states: "The service is the sole writer to each
workspace database. Enforce ownership through an operating-system lock."
Melt keeps exactly one owning connection per workspace `state.sqlite`,
guarded by `owner.lock` (backlog task T008). The probe findings behind
that design:

- F3: the adapter fences no second writer. Any connection that holds the
  database file can commit to it.
- F4: an overlapping writer busy-waits inside `BEGIN IMMEDIATE` and
  blocks the Node event loop until the busy timeout ends.
- F5: `mintId` is per-connection in-memory state, so two connections
  mint colliding identifiers.

The service therefore drives the owning connection in a Node helper
process behind the framed service boundary (SPEC.md sections 12.1 and
14). The consequence for this proposal: the option needs per-connection
scope only. It must be honored on the connection that commits. No
cross-connection coordination is required. Read-only connections may
keep the shipped default.

## Proposed option

Add one optional field to the Node SQLite backend options:

```ts
/** SQLite synchronous level for the Node SQLite backend. */
export type SqliteSynchronous = "OFF" | "NORMAL" | "FULL" | "EXTRA";

export type NodeSqliteDatabaseOptions = {
	readonly walAutoCheckpointPages?: number;
	readonly busyTimeoutMs?: number;
	/** Durability level for this connection. The default stays "NORMAL". */
	readonly synchronous?: SqliteSynchronous;
};
```

Implementation notes:

- Validate `options.synchronous` against the four names before any SQL
  text is built. Interpolate only the validated value.
- Replace the hard-coded statement with
  `PRAGMA synchronous = ${level}` in the same position: after
  `journal_mode = WAL` and before `wal_autocheckpoint`.
- `openNodeSqliteStorage` forwards the field with no extra change,
  because it passes the remaining options through.
- Validate before the database file is created, so a rejected option
  leaves no file behind.

Alternatives considered:

| Shape | Why not |
|---|---|
| `fullSync: boolean` | Cannot express `OFF` or `EXTRA`, and hides the pragma level. |
| Raw number from `0` to `3` | Matches the pragma read-back, but invites magic numbers and unchecked interpolation. |
| `onOpen(db)` hook | Most flexible, but widens the interface and returns policy to every caller. |

Scope: the Node SQLite backend only. The Cloudflare Durable Objects
backend, the JSONL backend, and the in-memory backend do not expose this
pragma.

## Compatibility

- The default stays `NORMAL`. Existing callers read the same pragma
  value (`1`) and keep the same write behavior.
- The field is optional and additive. Callers that never pass it are
  unaffected, at run time and at compile time.
- The only new failure mode is an invalid value. It rejects during open
  instead of failing later inside SQL execution.
- The package is experimental and changes without notice between
  releases. This proposal makes no release promise. Melt adopts any
  implementation only behind its exact pin and its probe.
- The test cases run on Linux and depend on no macOS-specific setting,
  as SPEC.md section 14 requires in both directions. macOS full-sync
  verification is backlog task T032.

## Test cases

Proposed for the upstream suite:

1. Default stays NORMAL. `openNodeSqliteDatabase(path)` reads
   `PRAGMA synchronous` as `1`.
2. Explicit FULL holds through work. With
   `{ synchronous: "FULL" }`, the read-back stays `2` after
   `SqliteStorage.open` migrations, one commit, and
   `PRAGMA wal_checkpoint(PASSIVE)`.
3. The setting is per connection. Connection A opens with FULL and
   connection B with the default on the same file. A reads `2` and B
   reads `1`.
4. Invalid values reject. `{ synchronous: "BOGUS" }` rejects, and no
   database file remains.
5. Forwarding works. `openNodeSqliteStorage(path,
   { synchronous: "FULL" })` reads `2`.
6. Conformance holds. `createStorageConformance` passes with
   `synchronous: "FULL"`.
7. Write behavior changes on Linux. Under `strace`, a 20-commit loop
   with FULL makes at least 20 more `fsync` and `fdatasync` calls than
   the default run.

Cases 1 to 3 and case 7 already exist as committed Melt probe cases, so
the upstream suite can reuse them directly.

Melt keeps its own re-verification regardless of upstream:
`test_pinned_adapter_is_the_one_probed`,
`test_owning_connection_runs_synchronous_full_in_wal`,
`test_full_is_evidenced_by_fsyncs`, and
`test_upstream_conformance_passes_through_the_wrapper` in
`tests/integration/test_pi_durable_storage.py`.

## Reproducible local example

The example writes only to a private temporary directory outside the
repository. The adapter install under
`runtime/storage/pi_adapter/node_modules` is generated output; the
committed `.gitignore` excludes it.

1. From the repository root, install the pin: `make probe-setup`.
2. From the repository root, run the block below with Node 22.19 or
   newer. It changes into the probe directory, runs against the pin, and
   removes the temporary directory afterwards.

```bash
cd runtime/storage/pi_adapter
export MELT_PI_DB="$(mktemp -d)/state.sqlite"
node --input-type=module <<'EOF'
import { openNodeSqliteDatabase } from "@earendil-works/pi-durable/storage/sqlite/node";

const path = process.env.MELT_PI_DB;
const read = async (db) => (await db.get("PRAGMA synchronous")).synchronous;

// 1. The shipped default on a fresh connection.
const plain = await openNodeSqliteDatabase(path);
console.log("shipped default:", await read(plain), "(1 = NORMAL)");
await plain.close();

// 2. There is no option to change it: NodeSqliteDatabaseOptions only
//    offers walAutoCheckpointPages and busyTimeoutMs, so an extra
//    property is ignored at run time.
const ignored = await openNodeSqliteDatabase(path, { synchronous: "FULL" });
console.log("option passed:  ", await read(ignored), "(still NORMAL)");
await ignored.close();

// 3. The Melt override from pi_storage.mjs, in one line.
const owned = await openNodeSqliteDatabase(path);
await owned.exec("PRAGMA synchronous = FULL");
console.log("after override: ", await read(owned), "(2 = FULL)");
await owned.close();

// 4. The setting is per connection: a fresh open starts at NORMAL again.
const fresh = await openNodeSqliteDatabase(path);
console.log("fresh open:     ", await read(fresh), "(1 = NORMAL)");
await fresh.close();
EOF
rm -rf "$(dirname "$MELT_PI_DB")"
```

Observed output on 2026-10-18, Node v24.18.0, Linux 6.8.0-147-generic,
ext4:

```text
shipped default: 1 (1 = NORMAL)
option passed:   1 (still NORMAL)
after override:  2 (2 = FULL)
fresh open:      1 (1 = NORMAL)
```

The four lines are the whole case for the option: the default is NORMAL,
no option exists today, the override raises one connection, and the next
open starts at the default again. For the verified path, run the
committed evidence instead of the third line. Both commands run from the
repository root:

```bash
node runtime/storage/pi_adapter/probe.mjs --root "$(mktemp -d)" --suite config
uv run --locked python3 -m pytest tests/integration/test_pi_durable_storage.py
```

## Why Melt keeps its tested FULL override

- The wrapper is the tested path. `make check` gates on the probe, 26
  cases, and on the pytest wrapper, 9 tests. The override is not an
  assumption; it is verified behavior.
- The adapter is experimental and changes without notice. Melt holds it
  at an exact pin asserted at run time. Any adoption starts with a
  deliberate re-pin.
- Process-death tests cannot catch a silent durability loss. Finding F2
  shows that SIGKILL rounds pass under the shipped default too. If an
  upstream option were ignored, renamed, or changed in meaning, every
  crash test would still pass while the connection ran at NORMAL. Only
  the pragma read-back and the sync counts notice the difference.
- SPEC.md section 14 asks Melt to configure and verify the setting.
  Verification stays mandatory, whichever path configures it.

Adoption plan, in order:

1. A released adapter version ships the option.
2. Melt bumps the exact pin in `package.json`, `package-lock.json`,
   `PI_DURABLE_PIN`, and `PINNED_ADAPTER`.
3. The probe gains a case that opens through the native option and
   verifies the read-back, next to the fsync contrast case. The full
   probe passes on the tested Linux filesystem.
4. The wrapper passes the option to the adapter and keeps its read-back
   verification.
5. The probe README and this draft record the verified version.

Until step 5 completes, the applied-and-verified pragma in
`pi_storage.mjs` stays in place, and the pinned version remains
`1.3.0`.

## Non-goals

- This draft files no issue and contacts no maintainer. An operator
  decides whether and where to publish it.
- Publication does not gate the Linux release, the RFC 001 extension
  round, or the native macOS round.
- Melt requests no change to defaults. Callers that want NORMAL keep it.
- The Durable Objects, JSONL, and in-memory backends are out of scope.

## References

- `SPEC.md` section 14, persistence and recovery.
- `runtime/storage/pi_adapter/README.md`, findings F1 to F5, the tested
  environment, and the crash model.
- `runtime/storage/pi_adapter/pi_storage.mjs`, the wrapper.
- `tests/integration/test_pi_durable_storage.py`, the gated verdict.
- Upstream source, `src/storage/sqlite/node.ts` in
  `@earendil-works/pi-durable` 1.3.0.
