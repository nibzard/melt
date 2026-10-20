/**
 * Workspace storage worker (SPEC.md sections 12.1 and 14, backlog task T008).
 *
 * The Melt service runtime is Python and the pinned Pi Durable adapter is a
 * Node library over `node:sqlite`, so the owning connection for one workspace
 * `state.sqlite` lives here, in a Node helper process. The service drives it
 * over the framed channel of SPEC.md section 12.1: a four-byte unsigned
 * big-endian byte length followed by UTF-8 JSON, rejected above the control
 * frame limit before allocation. Project stdout is never a control channel;
 * this process speaks only with its parent through stdin and stdout.
 *
 * Why one connection per process matters (probe findings F3, F4, F5 in
 * README.md): the adapter fences no second writer, a contended `BEGIN
 * IMMEDIATE` blocks the Node event loop for the whole busy timeout, and
 * `mintId` is per-connection in-memory state whose counters collide across
 * connections. This worker therefore opens at most one connection, mints
 * every identifier on that connection, and refuses a second `open` while one
 * is held. Single-writer ownership across processes is the service's
 * operating-system `owner.lock`, not anything in this file.
 *
 * The worker exits when stdin ends, so a service that dies releases both the
 * lock and this connection without an orphan holding the database.
 */

import { openDurableStorage, readPragma, PI_DURABLE_PIN, assertPinnedAdapter, CONTEXT } from "./pi_storage.mjs";

const FRAME_HEADER_LENGTH = 4;
// SPEC.md section 15.2 fixes the control frame limit at 1 MiB. The same
// bound guards request and response frames on this private channel.
const FRAME_LIMIT_BYTES = 1024 * 1024;
// The adapter reserves this id for the root conversation of every database.
const ROOT_CONVERSATION_ID = 1;

/** An operation failure reported back to the service as one error frame. */
class WorkerError extends Error {
	/** @param {string} code @param {string} message */
	constructor(code, message) {
		super(message);
		this.name = "WorkerError";
		this.code = code;
	}
}

const state = {
	/** @type {import("@earendil-works/pi-durable/storage/sqlite").SqliteStorage | null} */
	storage: null,
	/** @type {import("@earendil-works/pi-durable/storage/sqlite/node").NodeSqliteDatabase | null} */
	db: null,
	path: null,
	rootCommitted: false,
};

// ---------------------------------------------------------------------------
// Framing
// ---------------------------------------------------------------------------

/**
 * Write one response frame. The length prefix is bounded before the frame is
 * built, so a response can never declare more than the control frame limit.
 * @param {Record<string, unknown>} payload
 */
function send(payload) {
	const body = Buffer.from(JSON.stringify(payload), "utf8");
	if (body.byteLength > FRAME_LIMIT_BYTES) {
		payload = {
			ok: false,
			code: "E_LIMIT",
			message: `response of ${body.byteLength} bytes exceeds the control frame limit of ${FRAME_LIMIT_BYTES}`,
		};
		return send(payload);
	}
	const frame = Buffer.alloc(FRAME_HEADER_LENGTH + body.byteLength);
	frame.writeUInt32BE(body.byteLength, 0);
	body.copy(frame, FRAME_HEADER_LENGTH);
	return new Promise((resolve) => {
		process.stdout.write(frame, () => resolve());
	});
}

/** Operations run one at a time; this chain keeps frames in arrival order. */
let queue = Promise.resolve();

/** @param {Record<string, unknown>} request */
function dispatch(request) {
	queue = queue
		.then(async () => {
			try {
				const result = await handle(request);
				await send({ ok: true, ...result });
				if (result.exitAfterReply === true) {
					// Already inside the queue chain; waiting on it here
					// would wait for this very task.
					await exitAfter(0, undefined, { waitForQueue: false });
				}
			} catch (error) {
				const code = error instanceof WorkerError ? error.code : "E_PARSE";
				await send({ ok: false, code, message: String(error?.message ?? error) });
			}
		})
		.catch(() => {});
}

let buffer = Buffer.alloc(0);

/** Consume complete frames from the accumulated stdin bytes. */
function drain() {
	for (;;) {
		if (buffer.byteLength < FRAME_HEADER_LENGTH) return;
		const declared = buffer.readUInt32BE(0);
		if (declared > FRAME_LIMIT_BYTES) {
			// The stream can no longer be resynchronized: the payload of this
			// frame was never read. Report and exit (SPEC.md section 12.1).
			const message =
				`declared frame length ${declared} exceeds the control frame` +
				` limit of ${FRAME_LIMIT_BYTES} bytes`;
			queue = queue
				.then(() => send({ ok: false, code: "E_LIMIT", message }))
				.then(() => {
					process.exit(1);
				});
			return;
		}
		if (buffer.byteLength < FRAME_HEADER_LENGTH + declared) return;
		const body = buffer.subarray(FRAME_HEADER_LENGTH, FRAME_HEADER_LENGTH + declared);
		buffer = buffer.subarray(FRAME_HEADER_LENGTH + declared);
		let request;
		try {
			request = JSON.parse(body.toString("utf8"));
		} catch (error) {
			// The frame itself was well formed, so the stream stays aligned.
			queue = queue.then(() =>
				send({ ok: false, code: "E_PARSE", message: `frame is not valid JSON: ${error?.message ?? error}` }),
			);
			continue;
		}
		if (request === null || typeof request !== "object" || Array.isArray(request)) {
			queue = queue.then(() => send({ ok: false, code: "E_SCHEMA", message: "frame must carry one request object" }));
			continue;
		}
		dispatch(request);
	}
}

process.stdin.on("data", (chunk) => {
	buffer = Buffer.concat([buffer, chunk]);
	drain();
});

let exiting = false;

/** Close the owning connection when one is open. */
async function closeConnection() {
	if (state.storage === null) {
		return { wasOpen: false };
	}
	const storage = state.storage;
	state.storage = null;
	state.db = null;
	state.path = null;
	state.rootCommitted = false;
	await storage.close(CONTEXT);
	return { wasOpen: true };
}

/**
 * Close the connection, optionally wait for queued work, and exit.
 * @param {number} closeCode @param {string | undefined} reason
 * @param {{waitForQueue?: boolean}} [options]
 */
async function exitAfter(closeCode, reason, options = {}) {
	if (exiting) return;
	exiting = true;
	try {
		if (options.waitForQueue !== false) {
			await queue;
		}
		await closeConnection();
	} catch {
		// A closing failure must not keep the process alive.
	}
	process.exitCode = closeCode;
	if (reason !== undefined) {
		process.stderr.write(`workspace_worker.mjs: ${reason}\n`);
	}
	process.exit(closeCode);
}

process.stdin.on("end", () => {
	void exitAfter(0);
});
process.stdin.on("error", () => {
	void exitAfter(0);
});
process.stdout.on("error", () => {
	process.exit(1);
});

// ---------------------------------------------------------------------------
// Operation handlers
// ---------------------------------------------------------------------------

/** The one owning connection, or an error when none is open. */
function requireOpen() {
	if (state.storage === null) {
		throw new WorkerError("E_CONFLICT", "no connection is open on this worker");
	}
	return state.storage;
}

/** @param {Record<string, unknown>} request @param {string} name */
function requiredString(request, name) {
	const value = request[name];
	if (typeof value !== "string" || value.length === 0) {
		throw new WorkerError("E_SCHEMA", `field ${name} must be a non-empty string`);
	}
	return value;
}

/** @param {Record<string, unknown>} request @param {string} name */
function requiredId(request, name) {
	const value = request[name];
	if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) {
		throw new WorkerError("E_SCHEMA", `field ${name} must be a positive integer`);
	}
	return value;
}

/**
 * Open the single owning connection with the Melt durability policy applied
 * and verified by `pi_storage.mjs`.
 */
async function opOpen(request) {
	if (state.storage !== null) {
		throw new WorkerError(
			"E_CONFLICT",
			`this worker already owns the connection for ${state.path};` +
				" one workspace database has exactly one owning connection",
		);
	}
	const path = requiredString(request, "path");
	const { storage, db, facts } = await openDurableStorage(path);
	state.storage = storage;
	state.db = db;
	state.path = path;
	// The root conversation commit carries no entry, so a fresh database and
	// a reopened one differ here. The first record commit includes it.
	state.rootCommitted = (await storage.conversation(ROOT_CONVERSATION_ID, CONTEXT)) !== undefined;
	return { facts, rootConversationCommitted: state.rootCommitted, adapterVersion: PI_DURABLE_PIN };
}

/** Re-read the durability settings after migrations, commits, and checkpoints. */
async function opFacts() {
	const db = state.db;
	if (db === null) {
		throw new WorkerError("E_CONFLICT", "no connection is open on this worker");
	}
	return {
		facts: {
			adapterVersion: assertPinnedAdapter(),
			journalMode: await readPragma(db, "journal_mode"),
			synchronous: await readPragma(db, "synchronous"),
			...(process.platform === "darwin" ? {
				fullfsync: await readPragma(db, "fullfsync"),
				checkpointFullfsync: await readPragma(db, "checkpoint_fullfsync"),
			} : {}),
			integrityCheck: await readPragma(db, "integrity_check"),
		},
	};
}

/** Mint one identifier on the owning connection (probe finding F5). */
async function opMintId() {
	const storage = requireOpen();
	const id = await storage.mintId();
	if (!Number.isSafeInteger(id) || id <= 0) {
		throw new WorkerError("E_PARSE", `mintId returned ${id}, which is not a positive safe integer`);
	}
	return { id };
}

/**
 * Commit records atomically. Every identifier is minted on the owning
 * connection, and one storage commit covers the whole batch, so the caller
 * sees either every record or none of them.
 */
async function opCommit(request) {
	const storage = requireOpen();
	const records = request.records;
	if (!Array.isArray(records) || records.length === 0) {
		throw new WorkerError("E_SCHEMA", "commit requires a non-empty records array");
	}
	/** @type {Record<string, unknown>[]} */
	const writes = [];
	/** @type {{id: number, kind: string}[]} */
	const entries = [];
	if (!state.rootCommitted) {
		writes.push({ type: "conversation", value: { id: ROOT_CONVERSATION_ID } });
	}
	for (const record of records) {
		if (record === null || typeof record !== "object" || Array.isArray(record)) {
			throw new WorkerError("E_SCHEMA", "each record must be an object");
		}
		const kind = record.kind;
		if (typeof kind !== "string" || kind.length === 0) {
			throw new WorkerError("E_SCHEMA", "each record requires a non-empty kind");
		}
		const data = record.data;
		if (data === undefined || data === null || typeof data !== "object" || Array.isArray(data)) {
			throw new WorkerError("E_SCHEMA", `record ${kind} requires a data object`);
		}
		const id = await storage.mintId();
		entries.push({ id, kind });
		writes.push({
			type: "entry",
			value: { id, conversationId: ROOT_CONVERSATION_ID, kind, data },
		});
	}
	const commitSequence = await storage.commit(writes, CONTEXT);
	state.rootCommitted = true;
	return { commitSequence, entries };
}

/** Read one entry with the commit sequence that made it durable. */
async function opEntry(request) {
	const storage = requireOpen();
	const id = requiredId(request, "id");
	const read = await storage.entry(id, CONTEXT);
	if (read === undefined) {
		return { entry: null };
	}
	return {
		entry: {
			id,
			kind: read.entry.kind,
			data: read.entry.data ?? null,
			commitSequence: Number(read.commitSeq),
		},
	};
}

/** Scan the workspace record stream oldest first, with continuation. */
async function opScan(request) {
	const storage = requireOpen();
	const limit = request.limit === undefined ? 200 : request.limit;
	if (typeof limit !== "number" || !Number.isSafeInteger(limit) || limit < 1 || limit > 500) {
		throw new WorkerError("E_SCHEMA", "limit must be an integer from 1 through 500");
	}
	const order = request.order === "descending" ? "descending" : "ascending";
	const cursor = request.cursor === undefined || request.cursor === null ? undefined : request.cursor;
	let pageSize = limit;
	while (true) {
		const page = await storage.scanEntries(
			{ conversationId: ROOT_CONVERSATION_ID, order },
			pageSize,
			cursor,
			CONTEXT,
		);
		const entries = [];
		for (const item of page.items) {
			const read = await storage.entry(item.id, CONTEXT);
			if (read === undefined) {
				throw new WorkerError("E_PARSE", `entry ${item.id} vanished between scan and read`);
			}
			entries.push({
				id: item.id,
				kind: item.kind,
				data: item.data ?? null,
				commitSequence: Number(read.commitSeq),
			});
		}
		const result = { entries, next: page.next ?? null };
		if (Buffer.byteLength(JSON.stringify({ ok: true, ...result }), "utf8") <= FRAME_LIMIT_BYTES) {
			return result;
		}
		if (pageSize === 1) {
			throw new WorkerError("E_LIMIT", "one stored record exceeds the scan response frame limit");
		}
		// Retry from the same opaque cursor. Only the adapter creates cursors,
		// so shrinking a page cannot skip entries or change scan order.
		pageSize = Math.max(1, Math.floor(Math.min(pageSize, entries.length) / 2));
	}
}

/** Close the owning connection. A later `open` on this worker is allowed. */
async function opClose() {
	const { wasOpen } = await closeConnection();
	return { closed: true, wasOpen };
}

/**
 * @param {Record<string, unknown>} request
 * @returns {Promise<Record<string, unknown>>}
 */
async function handle(request) {
	switch (request.op) {
		case "open":
			return opOpen(request);
		case "ping":
			requireOpen();
			return { pong: true };
		case "facts":
			return opFacts();
		case "mintId":
			return opMintId();
		case "commit":
			return opCommit(request);
		case "entry":
			return opEntry(request);
		case "scan":
			return opScan(request);
		case "close":
			return opClose();
		case "shutdown": {
			const closed = await opClose();
			return { shuttingDown: true, exitAfterReply: true, ...closed };
		}
		default:
			throw new WorkerError("E_SCHEMA", `unknown op ${JSON.stringify(request.op)}`);
	}
}
