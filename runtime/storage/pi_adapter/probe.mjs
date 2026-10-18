#!/usr/bin/env node
/**
 * Melt Pi Durable storage probe (SPEC.md section 14, backlog task T002).
 *
 * Proves, against the pinned adapter on Linux:
 *   - atomic updates: one commit spans every record kind, and a commit that
 *     fails midway stores nothing at all;
 *   - committed observation: a resolved commit is observable on the same
 *     connection, on a second connection, after a clean close, and after a
 *     SIGKILL crash;
 *   - synchronous=FULL on the owning connection, applied and verified by the
 *     application-owned wrapper, with fsync evidence gathered under strace;
 *   - the file and directory synchronization pattern for artifact
 *     publication, including under SIGKILL.
 *
 * Usage:
 *   node probe.mjs --root <dir> [--json] [--suite all|config|atomic|observation|crash|conformance|fsync]
 *
 * `--json` prints one verdict document and nothing else on stdout. Exit code
 * 0 means every case passed.
 */

import { execFile } from "node:child_process";
import { readdir, readFile, rm, stat, mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import assert from "node:assert/strict";
import { createStorageConformance } from "@earendil-works/pi-durable/testing";
import { openNodeSqliteDatabase } from "@earendil-works/pi-durable/storage/sqlite/node";
import {
	CONTEXT,
	PI_DURABLE_PIN,
	assertPinnedAdapter,
	openDurableStorage,
	publishArtifactFile,
	readPragma,
	sha256Hex,
} from "./pi_storage.mjs";

const run = promisify(execFile);
const here = dirname(fileURLToPath(import.meta.url));
const WORKER = join(here, "probe_worker.mjs");

const args = process.argv.slice(2);
const arg = (name) => {
	const index = args.indexOf(`--${name}`);
	return index === -1 ? undefined : args[index + 1];
};
const jsonOutput = args.includes("--json");
const suitesWanted = arg("suite") ?? "all";
const root = arg("root");
if (!root) {
	process.stderr.write("probe.mjs: --root <dir> is required\n");
	process.exit(2);
}

const results = [];
const findings = [];

/** @param {string} suite @param {string} name @param {() => any} fn */
async function case_(suite, name, fn) {
	try {
		const detail = (await fn()) ?? null;
		results.push({ suite, name, status: "pass", detail });
	} catch (error) {
		results.push({
			suite,
			name,
			status: "fail",
			error: String(error?.message ?? error),
		});
	}
}

function skip(suite, name, reason) {
	results.push({ suite, name, status: "skip", detail: reason });
}

/** A fresh directory for one case, wiped first. */
async function caseDir(suite, name) {
	const dir = join(root, suite, name.replace(/[^a-z0-9]+/gi, "-").toLowerCase());
	await rm(dir, { recursive: true, force: true });
	await mkdir(dir, { recursive: true });
	return dir;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(predicate, timeoutMs, what) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (predicate()) return true;
		await sleep(5);
	}
	throw new Error(`timed out after ${timeoutMs} ms waiting for ${what}`);
}

/** @param {string} kind @param {string} summary @param {Record<string, unknown>} evidence */
function finding(kind, summary, evidence) {
	findings.push({ kind, summary, evidence });
}

async function allEntries(storage) {
	const items = [];
	let cursor;
	for (;;) {
		const page = await storage.scanEntries(
			{ conversationId: 1, order: "ascending" },
			500,
			cursor,
			CONTEXT,
		);
		items.push(...page.items);
		if (page.next === undefined) break;
		cursor = page.next;
	}
	return items;
}

async function commitSeqs(storage) {
	const entries = await allEntries(storage);
	const seqs = [];
	for (const entry of entries) {
		const read = await storage.entry(entry.id, CONTEXT);
		if (read === undefined) throw new Error(`entry ${entry.id} vanished between scan and read`);
		seqs.push(Number(read.commitSeq));
	}
	return seqs.sort((a, b) => a - b);
}

/** Run the worker until the stop point, then SIGKILL it. */
async function crashRound(dir, workerArgs, stop) {
	const { spawn } = await import("node:child_process");
	const child = spawn(process.execPath, [WORKER, ...workerArgs], {
		stdio: ["ignore", "pipe", "pipe"],
	});
	// Attach before any await: the worker can exit before the stop point.
	const exit = new Promise((resolve) => {
		if (child.exitCode !== null || child.signalCode !== null) {
			resolve({ code: child.exitCode, signal: child.signalCode });
		} else {
			child.once("exit", (code, signal) => resolve({ code, signal }));
		}
	});
	const acks = [];
	let buffer = "";
	const stderr = [];
	child.stdout.setEncoding("utf8");
	child.stderr.setEncoding("utf8");
	child.stdout.on("data", (chunk) => {
		buffer += chunk;
		for (;;) {
			const index = buffer.indexOf("\n");
			if (index === -1) break;
			const line = buffer.slice(0, index);
			buffer = buffer.slice(index + 1);
			if (line.startsWith("ACK ")) acks.push(line.slice(4));
		}
	});
	child.stderr.on("data", (chunk) => stderr.push(chunk));
	try {
		if (stop.acks !== undefined) {
			await waitFor(() => acks.length >= stop.acks, 20000, `${stop.acks} acks`);
		} else {
			await sleep(stop.ms);
		}
	} finally {
		child.kill("SIGKILL");
	}
	const code = await exit;
	return { acks, exit: code, stderr: stderr.join("") };
}

function parseAcks(acks) {
	return acks.map((line) => {
		const [seq, id, kind, hash] = line.split(/\s+/);
		return { seq: Number(seq), id: Number(id), kind, hash };
	});
}

/**
 * Check the crash invariants on a reopened database.
 * - every acknowledged commit is present with its exact sequence;
 * - the committed sequences are contiguous (a commit is all or nothing);
 * - the database passes integrity_check.
 */
async function verifyCrashRound(dbPath, acks, expectEntries) {
	const { storage, db } = await openDurableStorage(dbPath);
	try {
		const parsed = parseAcks(acks);
		for (const ack of parsed) {
			if (ack.kind === "root") {
				const conversation = await storage.conversation(1, CONTEXT);
				assert.ok(conversation !== undefined, "acked root conversation is missing");
				continue;
			}
			const read = await storage.entry(ack.id, CONTEXT);
			assert.ok(read !== undefined, `acked entry ${ack.id} (seq ${ack.seq}) is missing`);
			assert.equal(Number(read.commitSeq), ack.seq, `entry ${ack.id} has the wrong commit sequence`);
			if (ack.kind === "artifact") {
				assert.equal(read.entry.data.hash, ack.hash, `entry ${ack.id} references another artifact`);
			}
		}
		// A timer round can kill the worker before the root conversation
		// commit resolves. The adapter throws instead of returning empty
		// when scanning a conversation that was never created, and such
		// a database holds no entries.
		const rootCommitted = (await storage.conversation(1, CONTEXT)) !== undefined;
		const seqs = rootCommitted ? await commitSeqs(storage) : [];
		if (seqs.length > 0) {
			assert.equal(seqs[0], 2, "entry sequences start after the root conversation commit");
			for (let index = 1; index < seqs.length; index++) {
				assert.equal(seqs[index], seqs[index - 1] + 1, "committed sequences have a hole");
			}
		}
		assert.equal(
			seqs.length >= expectEntries.minimum,
			true,
			`only ${seqs.length} entries survived, expected at least ${expectEntries.minimum}`,
		);
		assert.equal(await readPragma(db, "integrity_check"), "ok");
		assert.equal(await readPragma(db, "journal_mode"), "wal");
		return { acked: parsed.length, committed: seqs.length };
	} finally {
		await storage.close(CONTEXT);
	}
}

/** Every database artifact reference points at a complete, matching file. */
async function verifyArtifacts(dbPath, artifactsDir) {
	const { storage } = await openDurableStorage(dbPath);
	try {
		const entries = await allEntries(storage);
		let checked = 0;
		for (const entry of entries) {
			if (entry.kind !== "probe.artifact") continue;
			const bytes = await readFile(join(artifactsDir, entry.data.hash));
			assert.equal(
				bytes.byteLength,
				entry.data.byteLength,
				`artifact ${entry.data.hash} has the wrong length`,
			);
			assert.equal(sha256Hex(bytes), entry.data.hash, `artifact ${entry.data.hash} does not match its hash`);
			checked++;
		}
		return checked;
	} finally {
		await storage.close(CONTEXT);
	}
}

// ---------------------------------------------------------------------------
// Environment facts
// ---------------------------------------------------------------------------

async function filesystemFacts(path) {
	try {
		const type = (await run("stat", ["-f", "-c", "%T", path])).stdout.trim();
		const findmnt = (await run("findmnt", ["-no", "SOURCE,FSTYPE,OPTIONS", "-T", path])).stdout.trim();
		return { path, type, mount: findmnt };
	} catch (error) {
		return { path, type: "unknown", mount: `unavailable: ${String(error?.message ?? error)}` };
	}
}

// ---------------------------------------------------------------------------
// Suite: configuration of the owning connection
// ---------------------------------------------------------------------------

async function suiteConfig() {
	await case_("config", "pinned adapter resolves", async () => {
		assert.equal(assertPinnedAdapter(), PI_DURABLE_PIN);
		return { pin: PI_DURABLE_PIN };
	});

	await case_("config", "adapter default synchronous on a fresh connection", async () => {
		const dir = await caseDir("config", "adapter-default-synchronous");
		const db = await openNodeSqliteDatabase(join(dir, "state.sqlite"));
		try {
			const synchronous = await readPragma(db, "synchronous");
			if (synchronous !== 1) {
				finding("adapter-changed", "The adapter no longer opens with synchronous=NORMAL; update this probe and README.md", {
					synchronous,
				});
			}
			assert.equal(synchronous, 1, "expected the shipped adapter to open with synchronous=NORMAL");
			return { synchronous };
		} finally {
			await db.close();
		}
	});

	await case_("config", "wrapper applies and verifies synchronous FULL", async () => {
		const dir = await caseDir("config", "wrapper-synchronous-full");
		const { storage, facts } = await openDurableStorage(join(dir, "state.sqlite"));
		try {
			assert.equal(facts.synchronous, 2);
			assert.equal(facts.synchronousDefault, 1);
			return facts;
		} finally {
			await storage.close(CONTEXT);
		}
	});

	await case_("config", "journal mode is WAL on the owning connection", async () => {
		const dir = await caseDir("config", "journal-mode-wal");
		const { storage, facts } = await openDurableStorage(join(dir, "state.sqlite"));
		try {
			assert.equal(facts.journalMode, "wal");
			return { journalMode: facts.journalMode };
		} finally {
			await storage.close(CONTEXT);
		}
	});

	await case_("config", "FULL survives migrations, commits, and a checkpoint", async () => {
		const dir = await caseDir("config", "full-survives-work");
		const { storage, db } = await openDurableStorage(join(dir, "state.sqlite"));
		try {
			await storage.commit([{ type: "conversation", value: { id: 1 } }], CONTEXT);
			const id = await storage.mintId();
			await storage.commit(
				[{ type: "entry", value: { id, conversationId: 1, kind: "probe.entry", data: { n: 1 } } }],
				CONTEXT,
			);
			await db.exec("PRAGMA wal_checkpoint(PASSIVE)");
			const after = await readPragma(db, "synchronous");
			assert.equal(after, 2, "synchronous reverted during normal work");
			return { after };
		} finally {
			await storage.close(CONTEXT);
		}
	});

	await case_("config", "a fresh connection starts at the adapter default again", async () => {
		const dir = await caseDir("config", "fresh-connection-default");
		const path = join(dir, "state.sqlite");
		const first = await openDurableStorage(path);
		await first.storage.close(CONTEXT);
		const db = await openNodeSqliteDatabase(path);
		try {
			const synchronous = await readPragma(db, "synchronous");
			assert.equal(synchronous, 1, "synchronous is per connection, so every open must apply FULL");
			return { synchronous };
		} finally {
			await db.close();
		}
	});
}

// ---------------------------------------------------------------------------
// Suite: atomic updates
// ---------------------------------------------------------------------------

async function suiteAtomic() {
	await case_("atomic", "one commit spans every record kind", async () => {
		const dir = await caseDir("atomic", "multi-record-commit");
		const path = join(dir, "state.sqlite");
		const { storage } = await openDurableStorage(path);
		try {
			const entryId = await storage.mintId();
			const taskId = await storage.mintId();
			const documentId = await storage.mintId();
			const seq = await storage.commit(
				[
					{ type: "conversation", value: { id: 1 } },
					{
						type: "entry",
						value: { id: entryId, conversationId: 1, kind: "probe.entry", data: { n: 1 } },
					},
					{
						type: "task",
						value: {
							id: taskId,
							conversationId: 1,
							kind: "probe.task",
							version: 1,
							input: { value: 1 },
							state: { status: "pending", checkpoint: { phase: "ready" } },
							background: false,
							abortRequested: false,
						},
					},
					{
						type: "document.create",
						record: { id: documentId, kind: "probe.document", scope: { kind: "session" } },
						content: { kind: "base", version: 1, value: { hello: "melt" } },
					},
				],
				CONTEXT,
			);
			assert.ok((await storage.conversation(1, CONTEXT)) !== undefined);
			assert.ok((await storage.entry(entryId, CONTEXT)) !== undefined);
			assert.ok((await storage.task(taskId, CONTEXT)) !== undefined);
			const document = await storage.document(documentId, "current", CONTEXT);
			assert.deepEqual(document?.value, { hello: "melt" });
			await storage.close(CONTEXT);
			const reopened = await openDurableStorage(path);
			try {
				assert.ok((await reopened.storage.entry(entryId, CONTEXT)) !== undefined);
				assert.deepEqual(
					(await reopened.storage.document(documentId, "current", CONTEXT))?.value,
					{ hello: "melt" },
				);
				return { seq, entryId, taskId, documentId };
			} finally {
				await reopened.storage.close(CONTEXT);
			}
		} finally {
			await storage.close(CONTEXT).catch(() => {});
		}
	});

	await case_("atomic", "a commit that fails midway stores nothing", async () => {
		const dir = await caseDir("atomic", "mid-batch-failure");
		const path = join(dir, "state.sqlite");
		const { storage } = await openDurableStorage(path);
		try {
			const before = await storage.commit([{ type: "conversation", value: { id: 1 } }], CONTEXT);
			const goodEntry = await storage.mintId();
			const badEntry = await storage.mintId();
			const documentId = await storage.mintId();
			const circular = {};
			circular.self = circular;
			await assert.rejects(
				storage.commit(
					[
						{
							type: "entry",
							value: { id: goodEntry, conversationId: 1, kind: "probe.entry", data: { n: 1 } },
						},
						{
							type: "document.create",
							record: { id: documentId, kind: "probe.document", scope: { kind: "session" } },
							content: { kind: "base", version: 1, value: { doomed: true } },
						},
						{
							type: "entry",
							// Encoding this record throws after the writes above applied.
							value: { id: badEntry, conversationId: 1, kind: "probe.entry", data: circular },
						},
					],
					CONTEXT,
				),
			);
			assert.equal(await storage.entry(goodEntry, CONTEXT), undefined, "first write leaked");
			assert.equal(await storage.entry(badEntry, CONTEXT), undefined, "failing write leaked");
			assert.equal(
				await storage.findDocument(
					{ kind: "probe.document", scope: { kind: "session" } },
					"current",
					CONTEXT,
				),
				undefined,
				"document leaked",
			);
			// The storage stays usable and the next sequence is the rejected one.
			const after = await storage.commit(
				[
					{
						type: "entry",
						value: { id: await storage.mintId(), conversationId: 1, kind: "probe.entry", data: { n: 2 } },
					},
				],
				CONTEXT,
			);
			assert.equal(after, before + 1, "a rejected commit must not consume a sequence number");
			await storage.close(CONTEXT);
			const reopened = await openDurableStorage(path);
			try {
				assert.equal(await reopened.storage.entry(goodEntry, CONTEXT), undefined);
				return { before, after };
			} finally {
				await reopened.storage.close(CONTEXT);
			}
		} finally {
			await storage.close(CONTEXT).catch(() => {});
		}
	});

	await case_("atomic", "unknown document change is rejected without writes", async () => {
		const dir = await caseDir("atomic", "unknown-document-change");
		const { storage } = await openDurableStorage(join(dir, "state.sqlite"));
		try {
			const before = await storage.commit([{ type: "conversation", value: { id: 1 } }], CONTEXT);
			await assert.rejects(
				storage.commit(
					[
						{
							type: "document.change",
							id: 9999,
							content: { kind: "base", version: 1, value: { nope: true } },
						},
					],
					CONTEXT,
				),
				/Unknown document/,
			);
			const after = await storage.commit(
				[
					{
						type: "entry",
						value: { id: await storage.mintId(), conversationId: 1, kind: "probe.entry", data: {} },
					},
				],
				CONTEXT,
			);
			assert.equal(after, before + 1);
			return { before, after };
		} finally {
			await storage.close(CONTEXT);
		}
	});

	await case_("atomic", "committed record IDs are never reused", async () => {
		const dir = await caseDir("atomic", "ids-never-reused");
		const { storage } = await openDurableStorage(join(dir, "state.sqlite"));
		try {
			await storage.commit([{ type: "conversation", value: { id: 1 } }], CONTEXT);
			await assert.rejects(
				storage.commit([{ type: "conversation", value: { id: 1 } }], CONTEXT),
				/already belongs/,
			);
			return {};
		} finally {
			await storage.close(CONTEXT);
		}
	});

	await case_("atomic", "one live incarnation per document address", async () => {
		const dir = await caseDir("atomic", "document-incarnation");
		const { storage } = await openDurableStorage(join(dir, "state.sqlite"));
		try {
			const first = await storage.mintId();
			await storage.commit(
				[
					{
						type: "document.create",
						record: { id: first, kind: "probe.document", scope: { kind: "session" } },
						content: { kind: "base", version: 1, value: { n: 1 } },
					},
				],
				CONTEXT,
			);
			await assert.rejects(
				storage.commit(
					[
						{
							type: "document.create",
							record: {
								id: await storage.mintId(),
								kind: "probe.document",
								scope: { kind: "session" },
							},
							content: { kind: "base", version: 1, value: { n: 2 } },
						},
					],
					CONTEXT,
				),
			);
			const current = await storage.findDocument(
				{ kind: "probe.document", scope: { kind: "session" } },
				"current",
				CONTEXT,
			);
			assert.equal(current?.id, first);
			return {};
		} finally {
			await storage.close(CONTEXT);
		}
	});
}

// ---------------------------------------------------------------------------
// Suite: committed observation
// ---------------------------------------------------------------------------

async function suiteObservation() {
	await case_("observation", "committed state is visible on the same connection", async () => {
		const dir = await caseDir("observation", "same-connection");
		const { storage } = await openDurableStorage(join(dir, "state.sqlite"));
		try {
			await storage.commit([{ type: "conversation", value: { id: 1 } }], CONTEXT);
			const id = await storage.mintId();
			await storage.commit(
				[{ type: "entry", value: { id, conversationId: 1, kind: "probe.entry", data: { n: 1 } } }],
				CONTEXT,
			);
			assert.ok((await storage.entry(id, CONTEXT)) !== undefined);
			return { id };
		} finally {
			await storage.close(CONTEXT);
		}
	});

	await case_("observation", "committed state is visible on a second connection", async () => {
		const dir = await caseDir("observation", "second-connection");
		const path = join(dir, "state.sqlite");
		const first = await openDurableStorage(path);
		try {
			await first.storage.commit([{ type: "conversation", value: { id: 1 } }], CONTEXT);
			const second = await openDurableStorage(path);
			try {
				const id = await first.storage.mintId();
				await first.storage.commit(
					[{ type: "entry", value: { id, conversationId: 1, kind: "probe.entry", data: { n: 1 } } }],
					CONTEXT,
				);
				assert.ok(
					(await second.storage.entry(id, CONTEXT)) !== undefined,
					"the second connection did not observe the commit",
				);
				return { id };
			} finally {
				await second.storage.close(CONTEXT);
			}
		} finally {
			await first.storage.close(CONTEXT);
		}
	});

	await case_("observation", "a second connection can write after the first commits", async () => {
		const dir = await caseDir("observation", "second-writer");
		const path = join(dir, "state.sqlite");
		const a = await openDurableStorage(path);
		try {
			const b = await openDurableStorage(path);
			try {
				await a.storage.commit([{ type: "conversation", value: { id: 1 } }], CONTEXT);
				for (let index = 0; index < 4; index++) {
					for (const [name, writer] of [["a", a.storage], ["b", b.storage]]) {
						// mintId is per-connection state, so one connection mints for both.
						await writer.commit(
							[
								{
									type: "entry",
									value: {
										id: await a.storage.mintId(),
										conversationId: 1,
										kind: "probe.entry",
										data: { index, writer: name },
									},
								},
							],
							CONTEXT,
						);
					}
				}
				assert.equal((await allEntries(a.storage)).length, 8);
				finding(
					"no-single-writer-fence",
					"The adapter accepts writes from any connection that holds the database; it does not fence a "
						+ "second writer. Melt must enforce single-writer ownership with the workspace owner lock "
						+ "(SPEC.md section 14, backlog task T008).",
					{ secondConnectionWritesAccepted: 8 },
				);
				return { entries: 8 };
			} finally {
				await b.storage.close(CONTEXT);
			}
		} finally {
			await a.storage.close(CONTEXT);
		}
	});

	await case_("observation", "overlapping writers in one process hit the busy timeout", async () => {
		const dir = await caseDir("observation", "overlapping-writers");
		const path = join(dir, "state.sqlite");
		// A short busy timeout keeps this case fast; the default is 5,000 ms.
		const a = await openDurableStorage(path, { busyTimeoutMs: 200 });
		try {
			const b = await openDurableStorage(path, { busyTimeoutMs: 200 });
			try {
				await a.storage.commit([{ type: "conversation", value: { id: 1 } }], CONTEXT);
				const idA = await a.storage.mintId();
				const idB = await b.storage.mintId();
				const entry = (id, writer) => ({
					type: "entry",
					value: { id, conversationId: 1, kind: "probe.entry", data: { writer } },
				});
				// Both transactions start before either commits. Whichever
				// connection reaches BEGIN IMMEDIATE second blocks the event
				// loop for the whole busy timeout, so that commit times out
				// and the lock holder finishes afterwards.
				const started = Date.now();
				const second = b.storage.commit([entry(idB, "b")], CONTEXT);
				const first = a.storage.commit([entry(idA, "a")], CONTEXT);
				const outcome = { a: "ok", b: "ok" };
				await first.catch((error) => {
					outcome.a = String(error?.message ?? error);
				});
				await second.catch((error) => {
					outcome.b = String(error?.message ?? error);
				});
				const rejected = Object.entries(outcome).filter(([, value]) => value !== "ok");
				assert.equal(rejected.length, 1, `expected exactly one rejected writer: ${JSON.stringify(outcome)}`);
				assert.match(rejected[0][1], /database is locked/);
				finding(
					"event-loop-blocking-busy-wait",
					"An overlapping writer on a second connection blocks the Node event loop inside "
						+ "BEGIN IMMEDIATE for the whole busy timeout, which starves the transaction that holds "
						+ "the write lock in the same process. The service must keep exactly one writing "
						+ "connection per workspace database (SPEC.md section 14).",
					{ busyTimeoutMs: 200, waitedMs: Date.now() - started, outcome },
				);
				return outcome;
			} finally {
				await b.storage.close(CONTEXT);
			}
		} finally {
			await a.storage.close(CONTEXT);
		}
	});

	await case_("observation", "clean close checkpoints the WAL and reopen sees everything", async () => {
		const dir = await caseDir("observation", "clean-close");
		const path = join(dir, "state.sqlite");
		const { storage } = await openDurableStorage(path);
		await storage.commit([{ type: "conversation", value: { id: 1 } }], CONTEXT);
		const ids = [];
		for (let index = 0; index < 10; index++) {
			const id = await storage.mintId();
			ids.push(id);
			await storage.commit(
				[{ type: "entry", value: { id, conversationId: 1, kind: "probe.entry", data: { index } } }],
				CONTEXT,
			);
		}
		await storage.close(CONTEXT);
		const wal = await stat(join(dir, "state.sqlite-wal")).catch(() => undefined);
		const reopened = await openDurableStorage(path);
		try {
			const entries = await allEntries(reopened.storage);
			assert.equal(entries.length, ids.length);
			return { walBytesAfterClose: wal?.size ?? null, entries: entries.length };
		} finally {
			await reopened.storage.close(CONTEXT);
		}
	});

	await case_("observation", "artifact publication orders file sync before the reference", async () => {
		const dir = await caseDir("observation", "artifact-ordering");
		const artifactsDir = join(dir, "artifacts");
		const bytes = Buffer.from("melt probe artifact\n" + "y".repeat(2048), "utf8");
		const hash = sha256Hex(bytes);
		const finalPath = await publishArtifactFile(artifactsDir, hash, bytes);
		const stored = await readFile(finalPath);
		assert.equal(sha256Hex(stored), hash);
		const names = await readdir(artifactsDir);
		assert.deepEqual(names, [hash], "staging file must not remain after publication");
		return { hash, finalPath };
	});
}

// ---------------------------------------------------------------------------
// Suite: SIGKILL crash rounds
// ---------------------------------------------------------------------------

const CRASH_ROUNDS = [
	{ name: "kill-after-first-commit", stop: { acks: 1 }, count: 60 },
	{ name: "kill-early", stop: { acks: 5 }, count: 60 },
	{ name: "kill-midway", stop: { acks: 27 }, count: 60 },
	{ name: "kill-late", stop: { acks: 55 }, count: 60 },
	{ name: "kill-on-a-timer", stop: { ms: 120 }, count: 200 },
];

async function suiteCrash() {
	for (const round of CRASH_ROUNDS) {
		await case_("crash", `${round.name} keeps acknowledged commits and no partial ones`, async () => {
			const dir = await caseDir("crash", round.name);
			const dbPath = join(dir, "state.sqlite");
			const { acks, exit, stderr } = await crashRound(dir, [
				"commit-loop",
				"--db",
				dbPath,
				"--count",
				String(round.count),
			], round.stop);
			if (exit.code === 0 || exit.signal !== "SIGKILL") {
				throw new Error(`worker was not killed: exit=${JSON.stringify(exit)} stderr=${stderr}`);
			}
			// Acks include the root conversation, whose commit carries no
			// entry. A timer round can kill the worker before any commit.
			const minimum = round.stop.acks === undefined ? 0 : Math.max(0, round.stop.acks - 1);
			const verified = await verifyCrashRound(dbPath, acks, { minimum });
			// The stop contract travels with the detail so the pytest
			// wrapper can assert what this round guarantees: ack-driven
			// rounds promise their ack count, timer rounds promise none.
			return { killed: exit, ...verified, stop: round.stop, minimum };
		});
	}

	await case_("crash", "contrast round with the shipped default also survives process death", async () => {
		const dir = await caseDir("crash", "contrast-normal");
		const dbPath = join(dir, "state.sqlite");
		const { acks, exit } = await crashRound(
			dir,
			["commit-loop", "--db", dbPath, "--count", "40", "--synchronous", "normal"],
			{ acks: 20 },
		);
		assert.equal(exit.signal, "SIGKILL");
		const verified = await verifyCrashRound(dbPath, acks, { minimum: 19 });
		finding(
			"process-kill-does-not-separate-modes",
			"Under process death, WAL frames already written to the operating system survive regardless of "
				+ "synchronous mode. synchronous=FULL matters for power loss and operating-system crash, which "
				+ "the fsync evidence suite documents instead (README.md, crash model).",
			{ mode: "normal", ...verified },
		);
		return { ...verified, stop: { acks: 20 } };
	});

	await case_("crash", "acknowledged artifact references always point at complete files", async () => {
		const dir = await caseDir("crash", "artifact-references");
		const dbPath = join(dir, "state.sqlite");
		const artifactsDir = join(dir, "artifacts");
		const { acks, exit } = await crashRound(
			dir,
			["artifact-loop", "--db", dbPath, "--artifacts", artifactsDir, "--count", "40"],
			{ acks: 17 },
		);
		assert.equal(exit.signal, "SIGKILL");
		const verified = await verifyCrashRound(dbPath, acks, { minimum: 16 });
		const checked = await verifyArtifacts(dbPath, artifactsDir);
		return { ...verified, artifactsVerified: checked, stop: { acks: 17 } };
	});
}

// ---------------------------------------------------------------------------
// Suite: upstream storage conformance on the pinned adapter
// ---------------------------------------------------------------------------

function partialDeepEqual(actual, expected, path = "$") {
	if (expected === null || typeof expected !== "object") {
		assert.deepEqual(actual, expected, `${path}: deep mismatch`);
		return;
	}
	if (Array.isArray(expected)) {
		assert.ok(Array.isArray(actual), `${path}: expected an array`);
		assert.equal(actual.length, expected.length, `${path}: array length`);
		expected.forEach((item, index) => partialDeepEqual(actual[index], item, `${path}[${index}]`));
		return;
	}
	assert.ok(actual !== null && typeof actual === "object", `${path}: expected an object`);
	for (const [key, value] of Object.entries(expected)) {
		assert.ok(key in actual, `${path}.${key}: missing key`);
		partialDeepEqual(actual[key], value, `${path}.${key}`);
	}
}

async function suiteConformance() {
	await case_("conformance", "upstream storage conformance over the durable wrapper", async () => {
		let passed = 0;
		const failures = [];
		const cases = createStorageConformance({
			assertions: {
				ok: (value, message) => assert.ok(value, message),
				strictEqual: (actual, expected) => assert.strictEqual(actual, expected),
				deepEqual: (actual, expected) => assert.deepEqual(actual, expected),
				partialDeepEqual: (actual, expected) => partialDeepEqual(actual, expected),
				greaterThan: (actual, expected) => assert.ok(actual > expected, `${actual} > ${expected}`),
				rejects: async (operation, messageIncludes) => {
					try {
						await operation;
					} catch (error) {
						assert.ok(
							String(error?.message ?? error).includes(messageIncludes),
							`expected rejection containing ${JSON.stringify(messageIncludes)}, got ${error?.message ?? error}`,
						);
						return;
					}
					assert.fail(`expected a rejection containing ${JSON.stringify(messageIncludes)}`);
				},
			},
			withStorage: async (use) => {
				const dir = await caseDir("conformance", `case-${passed + failures.length}`);
				const { storage } = await openDurableStorage(join(dir, "state.sqlite"));
				try {
					await use(storage);
				} finally {
					await storage.close(CONTEXT).catch(() => {});
				}
			},
		});
		for (const storageCase of cases) {
			try {
				await storageCase.run();
				passed++;
			} catch (error) {
				failures.push({ case: storageCase.name, error: String(error?.message ?? error) });
			}
		}
		assert.deepEqual(failures, []);
		return { cases: cases.length, passed };
	});
}

// ---------------------------------------------------------------------------
// Suite: fsync evidence for synchronous=FULL
// ---------------------------------------------------------------------------

async function fsyncCount(dir, mode, count) {
	const { spawn } = await import("node:child_process");
	const traceFile = join(dir, `trace-${mode}.txt`);
	const dbPath = join(dir, `state-${mode}.sqlite`);
	const child = spawn(
		"strace",
		[
			"-f",
			"-e",
			"trace=fsync,fdatasync",
			"-o",
			traceFile,
			process.execPath,
			WORKER,
			"commit-loop",
			"--db",
			dbPath,
			"--count",
			String(count),
			"--synchronous",
			mode,
		],
		{ stdio: ["ignore", "ignore", "pipe"] },
	);
	const stderr = [];
	child.stderr.setEncoding("utf8");
	child.stderr.on("data", (chunk) => stderr.push(chunk));
	const exit = await new Promise((resolve) => child.on("exit", (code, signal) => resolve({ code, signal })));
	if (exit.code !== 0) {
		throw new Error(`strace round failed: ${JSON.stringify(exit)} ${stderr.join("")}`);
	}
	const text = await readFile(traceFile, "utf8");
	return (text.match(/fsync\(|fdatasync\(/g) ?? []).length;
}

async function suiteFsync() {
	const available = await run("strace", ["--version"])
		.then(() => true)
		.catch(() => false);
	if (!available) {
		skip("fsync", "strace is unavailable", "fsync evidence needs strace; synchronous=FULL is still verified by pragma read-back");
		return;
	}
	await case_("fsync", "FULL fsyncs per commit, the shipped default does not", async () => {
		const dir = await caseDir("fsync", "fsync-per-commit");
		const count = 20;
		const full = await fsyncCount(dir, "full", count);
		const normal = await fsyncCount(dir, "normal", count);
		assert.ok(
			full - normal >= count,
			`FULL produced ${full} fsyncs and NORMAL ${normal}; FULL must add at least one per commit`,
		);
		return { commits: count, fsyncsFull: full, fsyncsNormal: normal };
	});
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
	await mkdir(root, { recursive: true });
	const environment = {
		node: process.version,
		platform: process.platform,
		arch: process.arch,
		kernel: (await run("uname", ["-r"]).catch(() => ({ stdout: "unknown" }))).stdout.trim(),
		filesystem: await filesystemFacts(root),
		crashModel:
			"SIGKILL process death after an acknowledged commit. Power loss and operating-system crash are "
			+ "not simulated in hardware; their protection is synchronous=FULL on the owning connection, "
			+ "evidenced by the per-commit fsync counts from the fsync suite.",
	};
	finding(
		"adapter-default-synchronous-normal",
		"The pinned adapter opens every connection with PRAGMA synchronous = NORMAL and exposes no option to "
			+ "change it. SPEC.md section 14 requires synchronous=FULL, so Melt applies and verifies FULL on "
			+ "the owning connection in runtime/storage/pi_adapter/pi_storage.mjs after every open.",
		{ pinnedAdapter: PI_DURABLE_PIN, required: "FULL", adapterDefault: "NORMAL" },
	);

	const suites = {
		config: suiteConfig,
		atomic: suiteAtomic,
		observation: suiteObservation,
		crash: suiteCrash,
		conformance: suiteConformance,
		fsync: suiteFsync,
	};
	for (const [name, run_] of Object.entries(suites)) {
		if (suitesWanted === "all" || suitesWanted === name) await run_();
	}

	const failures = results.filter((result) => result.status === "fail");
	const verdict = {
		probe: "melt-pi-durable-storage-probe",
		pinnedAdapter: PI_DURABLE_PIN,
		resolvedAdapter: assertPinnedAdapter(),
		environment,
		findings,
		results,
		summary: {
			passed: results.filter((r) => r.status === "pass").length,
			failed: failures.length,
			skipped: results.filter((r) => r.status === "skip").length,
		},
		verdict: failures.length === 0 ? "pass" : "fail",
	};
	if (jsonOutput) {
		process.stdout.write(`${JSON.stringify(verdict, null, 2)}\n`);
	} else {
		for (const result of results) {
			process.stdout.write(`${result.status.toUpperCase().padEnd(4)} ${result.suite}/${result.name}\n`);
			if (result.status === "fail") process.stdout.write(`       ${result.error}\n`);
		}
		process.stdout.write(
			`\n${verdict.verdict}: ${verdict.summary.passed} passed, ${verdict.summary.failed} failed, ${verdict.summary.skipped} skipped\n`,
		);
	}
	process.exitCode = failures.length === 0 ? 0 : 1;
}

await main();
