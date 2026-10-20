# Pi Durable storage adapter probe

Backlog task T002. External prerequisite for workspace storage (SPEC.md
section 14): probe the pinned Pi Durable storage adapter on Linux, prove
atomic updates and committed observation with integration tests, verify
`synchronous=FULL` on the owning connection, and record the tested
filesystem and crash model.

## Pin

| Item | Value |
|---|---|
| Adapter package | `@earendil-works/pi-durable` |
| Pinned version | `1.3.0` (exact, no range) |
| Source | npm registry, recorded in `package-lock.json` |
| Node engine | `>=22.19.0` (adapter requirement); probed on v24.18.0 |
| Used entry points | `/storage/sqlite/node`, `/storage/sqlite`, `/testing` |

Install the pin with `make probe-setup` (or `npm ci` here). `scripts/check.sh`
installs it before the test suite runs. The pin is asserted at run time by
`pi_storage.mjs`, so a different installed version fails the probe instead of
silently changing durability behavior.

Version 1.0.4 in the local source checkout at `~/compare/pi` was inspected as
well; it sets the same `synchronous = NORMAL` default described below.

## Files

| File | Role |
|---|---|
| `pi_storage.mjs` | Application-owned storage interface. Applies and verifies `synchronous=FULL` and WAL on the owning connection at every open, and publishes artifact bytes with file and directory synchronization. |
| `probe.mjs` | Probe driver. Emits a JSON verdict with per-case results, findings, and environment facts. |
| `probe_worker.mjs` | Child process for SIGKILL rounds. Acknowledges each commit on stdout only after it resolved. |
| `package.json`, `package-lock.json` | The pin. `node_modules/` is generated and ignored. |

## Proven properties

All cases below pass against the pin (26 cases, about 3 seconds). The pytest
wrapper `tests/integration/test_pi_durable_storage.py` re-runs the probe and
asserts the verdict, so `make check` gates on it.

**Configuration of the owning connection**

- The adapter opens every connection with `journal_mode = WAL`.
- The adapter opens every connection with `synchronous = NORMAL` (1). Finding
  F1 below.
- `pi_storage.mjs` raises the owning connection to `synchronous = FULL` (2)
  and verifies the read-back before returning. The setting survives the
  adapter's migrations, commits, and WAL checkpoints.
- A fresh connection starts at `NORMAL` again: `synchronous` is per
  connection, so every open must apply and verify it.
- On macOS the wrapper also enables and verifies `fullfsync=ON` and
  `checkpoint_fullfsync=ON` on every owning connection. The probe verifies
  both after commits and a WAL checkpoint. See the [macOS verification
  record](../../../docs/gates/macos-storage.md) for APFS results and limits.
- fsync evidence: a 21-commit run (root conversation plus 20 entries)
  produced 30 `fsync`/`fdatasync` calls under FULL and 8 under the shipped
  default, measured under `strace`. FULL adds at least one fsync per commit;
  the default syncs only at checkpoints.

**Atomic updates**

- One commit spans conversations, entries, tasks, and documents; all are
  observable after reopen.
- A commit whose encoding fails midway (a record that cannot be serialized
  after earlier writes in the same batch applied) stores nothing: no entry,
  no document, no revision.
- A rejected commit does not consume a sequence number; the next successful
  commit takes it.
- Unknown document changes are rejected without writes, committed record IDs
  are never reused, and one document address holds one live incarnation.

**Committed observation**

- A resolved commit is observable on the same connection, on a second
  concurrent connection, after a clean close (which checkpoints the WAL),
  and after every SIGKILL round below.
- Every acknowledged entry is present with its exact commit sequence after
  the crash, committed sequences are contiguous with no holes, and
  `PRAGMA integrity_check` returns `ok` after recovery.
- The timer round kills at a fixed time rather than at an ack count, so it
  can kill the worker before its first commit resolves. The reopened
  database then holds no conversation yet and verifies empty and intact.
  The adapter throws on scanning a conversation that was never created,
  so the verifier checks conversation existence first. Ack-driven rounds
  report their stop contract in the case detail, and the pytest wrapper
  asserts each round's own guarantee instead of a fixed commit count.

**Artifact publication synchronization**

- Publication order is: stage and fsync the file, rename into place, fsync
  the directory, then commit the database reference.
- After a SIGKILL during publication, every artifact entry in the database
  points at a complete file whose bytes hash to the referenced value.
  Unreferenced files may remain; recovery reconciles them (SPEC.md sections
  5.2 and 13.4).

**Upstream conformance**

- The adapter's own storage conformance suite (`createStorageConformance`,
  24 cases) passes through the Melt wrapper with FULL applied.

## Findings

**F1. The adapter pins `synchronous = NORMAL` and exposes no option for it.**
`openNodeSqliteDatabase` hard-codes `PRAGMA synchronous = NORMAL`, and
`NodeSqliteDatabaseOptions` only offers `walAutoCheckpointPages` and
`busyTimeoutMs`. SPEC.md section 14 requires `synchronous=FULL`, and the
raw `node:sqlite` default is FULL (2); the adapter lowers it. Resolution:
`synchronous` is a per-connection setting, so the application-owned wrapper
applies and verifies FULL on the owning connection after every open. This
closes the gap without a fork, and the probe fails loudly if a future
adapter version changes the default.

**F2. Process death does not distinguish NORMAL from FULL.** Under SIGKILL,
WAL frames already handed to the operating system survive in both modes, and
the contrast round passes with the shipped default. FULL matters for power
loss and operating-system crash; the fsync counts above are the evidence the
setting changes write behavior. See the crash model below for what is and is
not claimed.

**F3. The adapter does not fence a second writer.** A second connection can
commit to the same database file after the first commits. Single-writer
ownership is therefore a Melt obligation: the workspace `owner.lock` and one
owning connection per database (SPEC.md section 14, backlog task T008).

**F4. An overlapping writer blocks the Node event loop.** `BEGIN IMMEDIATE`
on a contended connection busy-waits synchronously, which starves the
transaction holding the write lock in the same process until the timeout
(the probe reproduces this deterministically: exactly one of two overlapping
commits fails with `database is locked`). Reinforces F3: keep exactly one
writing connection per workspace database, and keep the storage connection
out of any process that runs competing work.

**F5. `mintId` is per-connection in-memory state.** Two connections minting
IDs produce collisions. The owning service mints through one connection.

## Integration decision for T008

- The service opens each workspace `state.sqlite` through
  `openDurableStorage` in `pi_storage.mjs`, which applies and verifies the
  durability policy; no Melt code opens the adapter directly.
- The adapter is a Node library over `node:sqlite`, while the Melt service
  runtime is Python. The service therefore drives the owning connection in
  a Node helper process behind its own framed channel, the same boundary
  SPEC.md section 12.1 defines for host communication.
- One workspace database has exactly one owning connection, guarded by the
  operating-system `owner.lock`. No competing persistence path is being
  implemented before this boundary lands (SPEC.md section 14).
- The JSONL and in-memory storage backends in the adapter are not the Melt
  workspace path; this probe covers the SQLite backend only.

This decision is implemented by backlog task T008:

- `workspace_worker.mjs` is the Node helper. It opens at most one
  connection per process, refuses a second `open` while one is held, mints
  every identifier on that connection, and commits record batches
  atomically. It exits when stdin ends, so a service that dies leaves no
  orphan holding the database.
- `runtime/storage` in Python resolves the data root through the platform
  adapter, creates the section 14 directory tree, takes `owner.lock` with
  `flock`, and drives the helper over the framed channel. Findings F3
  through F5 are closed by construction: the fence is the lock, the event
  loop is never contended by a second writer, and IDs never collide
  because only one connection mints them.
- `tests/integration/test_workspace_storage.py` covers the layout, the
  platform adapter, ownership (including release after `SIGKILL`), the
  single owning connection, ID minting, restart and crash recovery, the
  frame limit on both sides, artifact bytes outside the database, and
  stored-format negotiation.

## Tested environment and crash model

| Item | Value on the probe machine |
|---|---|
| Kernel | Linux 6.8.0-147-generic x86_64 |
| Node | v24.18.0 |
| Filesystem | ext4 on `/dev/sda1`, mounted `rw,relatime,discard,errors=remount-ro,commit=30` |
| Database | SQLite through `node:sqlite` in WAL journal mode |
| Crash model | Process death by SIGKILL after an acknowledged commit |
| Not tested | Power loss and operating-system crash in hardware |

The probe reports its own environment facts in every verdict document, and
`stat -f` of the probe root is recorded there, so a run on another
filesystem records itself.

Durability claims are scoped to the model above: an acknowledged commit
survives process death (proven by the crash rounds), and its protection
against power loss comes from `synchronous=FULL` being applied and verified
on the owning connection (evidenced by per-commit fsync counts). No hardware
power-loss test was performed.

## Running

```text
make probe-setup                                  # npm ci against the pin
node probe.mjs --root <private-dir>               # human output
node probe.mjs --root <private-dir> --json        # machine verdict
node probe.mjs --root <private-dir> --suite crash # one suite
```

Use a private temporary root outside the repository. The pytest wrapper
creates its root under the system temporary directory, names it with the
Looper resource prefix when one is set, and registers idempotent cleanup.
