/**
 * Child process for the Pi Durable crash probe (SPEC.md sections 14 and 18.4).
 *
 * The parent kills this process with SIGKILL at a chosen point. Each
 * completed unit of work is acknowledged on stdout only after its storage
 * commit resolved, so an acknowledged line proves the commit returned before
 * the kill. Stdout itself is not durable; the probe's crash model is process
 * death, not power loss (README.md records the model).
 *
 * Modes:
 *   commit-loop   --db <path> --count <n> [--synchronous full|normal]
 *   artifact-loop --db <path> --artifacts <dir> --count <n>
 */

import { mkdir } from "node:fs/promises";
import { dirname } from "node:path";
import {
	CONTEXT,
	openDurableStorage,
	openPlainStorage,
	publishArtifactFile,
	sha256Hex,
} from "./pi_storage.mjs";

const args = process.argv.slice(2);
const arg = (name) => {
	const index = args.indexOf(`--${name}`);
	return index === -1 ? undefined : args[index + 1];
};
const mode = args[0] ?? "";

async function openStorage(dbPath) {
	if (arg("synchronous") === "normal") {
		// The adapter exactly as shipped, for the contrast round.
		return openPlainStorage(dbPath);
	}
	return (await openDurableStorage(dbPath)).storage;
}

function ack(line) {
	process.stdout.write(`${line}\n`);
}

async function commitLoop() {
	const dbPath = arg("db");
	const count = Number(arg("count") ?? 50);
	await mkdir(dirname(dbPath), { recursive: true });
	const storage = await openStorage(dbPath);
	const rootSeq = await storage.commit([{ type: "conversation", value: { id: 1 } }], CONTEXT);
	ack(`ACK ${rootSeq} 1 root`);
	for (let index = 0; index < count; index++) {
		const id = await storage.mintId();
		const seq = await storage.commit(
			[
				{
					type: "entry",
					value: {
						id,
						conversationId: 1,
						kind: "probe.entry",
						data: { index, payload: "x".repeat(64) },
					},
				},
			],
			CONTEXT,
		);
		ack(`ACK ${seq} ${id} entry`);
	}
	ack("DONE");
	await storage.close(CONTEXT);
}

async function artifactLoop() {
	const dbPath = arg("db");
	const artifactsDir = arg("artifacts");
	const count = Number(arg("count") ?? 40);
	await mkdir(dirname(dbPath), { recursive: true });
	const storage = await openStorage(dbPath);
	await storage.commit([{ type: "conversation", value: { id: 1 } }], CONTEXT);
	for (let index = 0; index < count; index++) {
		const bytes = Buffer.from(
			`probe-artifact-${index}\n` + "x".repeat(4096 + ((index * 37) % 512)),
			"utf8",
		);
		const hash = sha256Hex(bytes);
		// File durability first; the database reference follows only afterwards.
		await publishArtifactFile(artifactsDir, hash, bytes);
		const id = await storage.mintId();
		const seq = await storage.commit(
			[
				{
					type: "entry",
					value: {
						id,
						conversationId: 1,
						kind: "probe.artifact",
						data: { index, hash, byteLength: bytes.byteLength },
					},
				},
			],
			CONTEXT,
		);
		ack(`ACK ${seq} ${id} artifact ${hash}`);
	}
	ack("DONE");
	await storage.close(CONTEXT);
}

try {
	if (mode === "commit-loop") await commitLoop();
	else if (mode === "artifact-loop") await artifactLoop();
	else {
		process.stderr.write(`probe_worker.mjs: unknown mode ${JSON.stringify(mode)}\n`);
		process.exit(2);
	}
} catch (error) {
	process.stderr.write(`probe_worker.mjs: ${error?.stack ?? error}\n`);
	process.exit(1);
}
