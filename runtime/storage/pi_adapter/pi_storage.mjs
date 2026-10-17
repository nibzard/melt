/**
 * Application-owned Pi Durable storage interface (SPEC.md section 14).
 *
 * Melt code never opens the pinned adapter directly. Every open goes through
 * this module, which applies and verifies the durability policy on the owning
 * connection before the first commit.
 *
 * Why the wrapper exists: the pinned adapter's Node SQLite backend hard-codes
 * `PRAGMA synchronous = NORMAL` and exposes no option for it (probe finding
 * F1 in README.md). `synchronous` is a per-connection setting, so the owning
 * connection raises it to FULL after open. A fresh connection starts at the
 * adapter default again, so every open must apply and verify the setting.
 *
 * Artifact bytes stay outside the database (SPEC.md sections 5 and 14).
 * `publishArtifactFile` is the file and directory synchronization pattern the
 * publication step will reuse: fsync the staged file, rename, fsync the
 * directory, and only then commit the database reference.
 */

import { createHash, randomUUID } from "node:crypto";
import { mkdir, open, rename } from "node:fs/promises";
import { join } from "node:path";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { SqliteStorage } from "@earendil-works/pi-durable/storage/sqlite";
import { openNodeSqliteDatabase } from "@earendil-works/pi-durable/storage/sqlite/node";
import piDurablePackage from "@earendil-works/pi-durable/package.json" with { type: "json" };

export const PI_DURABLE_PIN = "1.3.0";
export const SYNCHRONOUS_NORMAL = 1;
export const SYNCHRONOUS_FULL = 2;
export const CONTEXT = BACKGROUND_CONTEXT;

export class StorageDurabilityError extends Error {
	/** @param {string} message @param {Record<string, unknown> | undefined} facts */
	constructor(message, facts = undefined) {
		super(message);
		this.name = "StorageDurabilityError";
		this.facts = facts;
	}
}

/** Resolved version of the installed adapter package. */
export function resolvedAdapterVersion() {
	return piDurablePackage.version;
}

/** Fail fast when the installed adapter is not the pinned one. */
export function assertPinnedAdapter() {
	const resolved = resolvedAdapterVersion();
	if (resolved !== PI_DURABLE_PIN) {
		throw new StorageDurabilityError(
			`Pi Durable adapter pin mismatch: expected ${PI_DURABLE_PIN}, installed ${resolved}`,
		);
	}
	return resolved;
}

/** Open the adapter exactly as shipped, with no Melt policy applied. */
export async function openPlainStorage(path, options = {}) {
	assertPinnedAdapter();
	return SqliteStorage.open(await openNodeSqliteDatabase(path, options));
}

/**
 * Open the owning connection with Melt durability policy applied and
 * verified: WAL journal mode and `synchronous = FULL`.
 *
 * @returns {{ storage: SqliteStorage, db: import("@earendil-works/pi-durable/storage/sqlite/node").NodeSqliteDatabase, facts: Record<string, unknown> }}
 */
export async function openDurableStorage(path, options = {}) {
	const resolved = assertPinnedAdapter();
	const db = await openNodeSqliteDatabase(path, options);
	const synchronousDefault = await readPragma(db, "synchronous");
	if (synchronousDefault !== SYNCHRONOUS_FULL) {
		await db.exec("PRAGMA synchronous = FULL");
	}
	const storage = await SqliteStorage.open(db);
	// Verify after migrations and every other statement the adapter ran.
	const facts = {
		adapterVersion: resolved,
		journalMode: await readPragma(db, "journal_mode"),
		synchronousDefault,
		synchronous: await readPragma(db, "synchronous"),
	};
	if (facts.synchronous !== SYNCHRONOUS_FULL) {
		await storage.close(CONTEXT).catch(() => {});
		throw new StorageDurabilityError(
			`Owning connection reports synchronous=${facts.synchronous}, Melt requires FULL (${SYNCHRONOUS_FULL})`,
			facts,
		);
	}
	if (facts.journalMode !== "wal") {
		await storage.close(CONTEXT).catch(() => {});
		throw new StorageDurabilityError(
			`Owning connection reports journal_mode=${facts.journalMode}, Melt requires wal`,
			facts,
		);
	}
	return { storage, db, facts };
}

/** Read one pragma value on the given connection. Adapter statements are async. */
export async function readPragma(db, name) {
	const row = await db.get(`PRAGMA ${name}`);
	return row?.[name];
}

export const sha256Hex = (bytes) => createHash("sha256").update(bytes).digest("hex");

/**
 * Publish immutable artifact bytes under their content hash.
 *
 * Ordering: stage and fsync the file, rename it into place, fsync the
 * directory, then the caller commits the database reference. A crash can
 * leave an unreferenced final file, never a referenced missing file.
 *
 * @param {string} artifactsDir
 * @param {string} hash lowercase sha256 hex of `bytes`
 * @param {Uint8Array} bytes
 */
export async function publishArtifactFile(artifactsDir, hash, bytes) {
	if (hash !== sha256Hex(bytes)) {
		throw new StorageDurabilityError("Artifact hash does not match its bytes");
	}
	await mkdir(artifactsDir, { recursive: true });
	const stagingPath = join(artifactsDir, `.staging-${process.pid}-${randomUUID()}.tmp`);
	const staged = await open(stagingPath, "w");
	try {
		await staged.writeFile(bytes);
		await staged.sync();
	} finally {
		await staged.close();
	}
	const finalPath = join(artifactsDir, hash);
	await rename(stagingPath, finalPath);
	await syncDirectory(artifactsDir);
	return finalPath;
}

/** Linux directory synchronization: fsync the directory that received the rename. */
export async function syncDirectory(path) {
	const handle = await open(path, "r");
	try {
		await handle.sync();
	} finally {
		await handle.close();
	}
}
