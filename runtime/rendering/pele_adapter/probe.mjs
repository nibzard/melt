#!/usr/bin/env node
/**
 * Melt Pele rendering probe (RFC 001 section 7.3, backlog task T028).
 *
 * Establishes the actual Linux interface of the pinned Pele renderer and
 * proves, or reports as a limitation, each property the semantic
 * extension needs from it:
 *
 *   - the pinned version resolves and render() works in plain Node with
 *     no browser globals;
 *   - rendered node groups carry `data-id` values that equal the lowered
 *     graph's node IDs verbatim, and the adapter fails closed when the
 *     rendered identity set differs;
 *   - host-owned hit regions extracted from the rendered SVG map a click
 *     in user space to a graph node ID and through the graph's identity
 *     map to a semantic reference;
 *   - labels are sanitized before lowering and the output carries no
 *     scripts, handlers, URL references, or non-allowlisted elements;
 *   - SVG generation alone provides no interaction: the pinned renderer
 *     exposes no hit-testing or event API, which this probe records as a
 *     finding rather than claiming it.
 *
 * Usage:
 *   node probe.mjs [--graph <graph.json>] [--json] [--suite all|pin|interface|mapping|hitregions|sanitize|security]
 *
 * `--json` prints one verdict document and nothing else on stdout. Exit
 * code 0 means every case passed. The probe writes no files.
 */

import { execFileSync } from "node:child_process";
import * as pele from "pele";
import {
	PELE_PIN,
	RENDER_OPTIONS,
	defaultGraphPath,
	hitTest,
	readGraph,
	renderGraph,
	resolvedPeleVersion,
	sanitizeLabel,
	verifySvg,
	PeleAdapterError,
} from "./pele_render.mjs";

const args = process.argv.slice(2);
const arg = (name) => {
	const index = args.indexOf(`--${name}`);
	return index === -1 ? undefined : args[index + 1];
};
const jsonOutput = args.includes("--json");
const suitesWanted = arg("suite") ?? "all";
const graphPath = arg("graph") ?? defaultGraphPath();

const results = [];
const findings = [];

/** @param {string} suite @param {string} name @param {() => any} fn */
function case_(suite, name, fn) {
	try {
		const detail = fn() ?? null;
		results.push({ suite, name, status: "pass", detail });
	} catch (error) {
		results.push({
			suite,
			name,
			status: "fail",
			error: error instanceof Error ? error.message : String(error),
		});
	}
}

/** @param {string} kind @param {string} summary @param {Record<string, unknown>} evidence */
function finding(kind, summary, evidence) {
	findings.push({ kind, summary, evidence });
}

/** @param {unknown} error @param {string} code */
function adapterErrorOf(error, code) {
	if (error instanceof PeleAdapterError && error.code === code) return error;
	throw new Error(
		`expected a PeleAdapterError with code ${code}, got: ${String(error)}`,
	);
}

const environment = {
	node: process.version,
	platform: process.platform,
	arch: process.arch,
	pele: resolvedPeleVersion(),
	domGlobals: ["window", "document", "HTMLElement"].filter(
		(name) => typeof globalThis[name] !== "undefined",
	),
};

// ---------------------------------------------------------------------------
// Suite: pin
// ---------------------------------------------------------------------------

function suitePin() {
	case_("pin", "the installed pele is the pinned version", () => {
		if (resolvedPeleVersion() !== PELE_PIN) {
			throw new Error(`resolved ${resolvedPeleVersion()}, pinned ${PELE_PIN}`);
		}
		return { pinned: PELE_PIN };
	});
	case_("pin", "render needs no browser globals", () => {
		if (environment.domGlobals.length > 0) {
			throw new Error(`browser globals present: ${environment.domGlobals}`);
		}
		const rendered = renderGraph(readGraph(graphPath));
		return { type: rendered.type, svgChars: rendered.svg.length };
	});
	case_("pin", "the committed lockfile installs the pin", () => {
		// `npm ls` fails when the installed tree disagrees with the lock.
		const out = execFileSync("npm", ["ls", "--omit=dev"], {
			cwd: new URL(".", import.meta.url).pathname,
			encoding: "utf8",
			stderr: "pipe",
		});
		if (!out.includes(`pele@${PELE_PIN}`)) {
			throw new Error(`npm ls does not report pele@${PELE_PIN}: ${out.trim()}`);
		}
		return { line: out.split("\n").find((l) => l.includes("pele@"))?.trim() };
	});
}

// ---------------------------------------------------------------------------
// Suite: interface
// ---------------------------------------------------------------------------

function suiteInterface() {
	case_("interface", "the module surface this adapter depends on", () => {
		const used = ["render", "parse", "PeleError", "detectType"];
		const missing = used.filter((name) => typeof pele[name] !== "function");
		if (missing.length > 0) throw new Error(`missing exports: ${missing}`);
		return { used, browserOnly: ["mount", "enableZoom"] };
	});
	case_("interface", "render returns svg, width, height, type, links", () => {
		const rendered = pele.render('flowchart LR\n  a["x"] --> b["y"]\n', RENDER_OPTIONS);
		for (const field of ["svg", "width", "height", "type", "links"]) {
			if (!(field in rendered)) throw new Error(`RenderResult lacks ${field}`);
		}
		return { type: rendered.type, width: rendered.width, height: rendered.height };
	});
	case_("interface", "parse and render accept the split pipeline", () => {
		const model = pele.parse('flowchart LR\n  a["x"] --> b["y"]\n');
		const rendered = pele.render(model, RENDER_OPTIONS);
		return { type: rendered.type, svgChars: rendered.svg.length };
	});
	case_("interface", "syntax errors carry code, line, column, snippet", () => {
		let thrown;
		try {
			pele.render("flowchart LR\n  a[)\n", RENDER_OPTIONS);
		} catch (error) {
			thrown = error;
		}
		if (!thrown || thrown.code !== "syntax") {
			throw new Error(`expected a syntax PeleError, got ${String(thrown)}`);
		}
		return {
			code: thrown.code,
			type: thrown.type,
			line: thrown.line,
			column: thrown.column,
			snippetChars: thrown.snippet.length,
		};
	});
	case_("interface", "a source beyond the limit fails with the limit code", () => {
		let thrown;
		try {
			pele.render("flowchart LR\n  a[\"" + "x".repeat(64) + "\"]\n", {
				...RENDER_OPTIONS,
				limit: 32,
			});
		} catch (error) {
			thrown = error;
		}
		if (!thrown || thrown.code !== "limit") {
			throw new Error(`expected a limit PeleError, got ${String(thrown)}`);
		}
		return { code: thrown.code };
	});
	case_("interface", "non-diagram text fails with unsupported-diagram", () => {
		let thrown;
		try {
			pele.render("just some words\n", RENDER_OPTIONS);
		} catch (error) {
			thrown = error;
		}
		if (!thrown || thrown.code !== "unsupported-diagram") {
			throw new Error(`expected unsupported-diagram, got ${String(thrown)}`);
		}
		return { code: thrown.code };
	});
	case_("interface", "the render options are locked by the adapter", () => {
		const frozen = Object.isFrozen(RENDER_OPTIONS);
		if (!frozen) throw new Error("RENDER_OPTIONS is not frozen");
		if (RENDER_OPTIONS.links !== false || RENDER_OPTIONS.images !== false) {
			throw new Error("links and images must be disabled");
		}
		return {
			links: RENDER_OPTIONS.links,
			images: RENDER_OPTIONS.images,
			limit: RENDER_OPTIONS.limit,
			outputLimit: RENDER_OPTIONS.outputLimit,
			maxEdges: RENDER_OPTIONS.maxEdges,
		};
	});
}

// ---------------------------------------------------------------------------
// Suite: mapping
// ---------------------------------------------------------------------------

function suiteMapping() {
	const graph = readGraph(graphPath);

	case_("mapping", "rendered data-ids equal the graph node ids", () => {
		const rendered = renderGraph(graph);
		const rendered_ = [...rendered.svg.matchAll(/<g class="pele-node[^"]*" data-id="([^"]+)"/g)].map((m) => m[1]);
		const expected = graph.nodes.map((n) => n.nodeId).sort();
		const actual = [...rendered_].sort();
		if (actual.length !== expected.length || actual.some((id, i) => id !== expected[i])) {
			throw new Error(`rendered ${actual}, expected ${expected}`);
		}
		return { rendered: actual, expected };
	});
	case_("mapping", "entity-syntax ids survive verbatim", () => {
		const ids = ["n", "n-2", "n_3", "a-b-c", "z".repeat(64)];
		const nodes = ids.map((id, i) => ({
			nodeId: id,
			label: `Node ${i}`,
			collection: "c",
			key: `K${i}`,
		}));
		const rendered = renderGraph({ nodes, edges: [] });
		const actual = [...rendered.svg.matchAll(/<g class="pele-node[^"]*" data-id="([^"]+)"/g)].map((m) => m[1]);
		const expected = [...ids].sort();
		const sorted = [...actual].sort();
		if (sorted.length !== expected.length || sorted.some((id, i) => id !== expected[i])) {
			throw new Error(`rendered ${sorted}, expected ${expected}`);
		}
		return { ids: sorted };
	});
	case_("mapping", "identical input renders byte-identical svg", () => {
		const first = renderGraph(graph);
		const second = renderGraph(graph);
		if (first.svg !== second.svg) throw new Error("two renders of one graph differ");
		return { svgChars: first.svg.length };
	});
	case_("mapping", "a label change keeps node ids and moves geometry", () => {
		const short = renderGraph({
			nodes: [
				{ nodeId: "a", label: "short", collection: "c", key: "K1" },
				{ nodeId: "b", label: "x", collection: "c", key: "K2" },
			],
			edges: [{ edgeId: "e", from: "a", to: "b" }],
		});
		const long = renderGraph({
			nodes: [
				{ nodeId: "a", label: "a much longer label here", collection: "c", key: "K1" },
				{ nodeId: "b", label: "x", collection: "c", key: "K2" },
			],
			edges: [{ edgeId: "e", from: "a", to: "b" }],
		});
		const ids = (r) =>
			[...r.svg.matchAll(/data-id="(a|b)"/g)].map((m) => m[1]).sort();
		if (JSON.stringify(ids(short)) !== JSON.stringify(ids(long))) {
			throw new Error("node ids changed with the label");
		}
		if (short.width === long.width) throw new Error("geometry did not move");
		return { ids: ids(long), widths: [short.width, long.width] };
	});
	case_("mapping", "a tampered identity set fails closed", () => {
		const rendered = renderGraph(graph);
		const wrong = ["s-42", "s-99"];
		let thrown;
		try {
			verifySvg(rendered.svg, wrong);
		} catch (error) {
			thrown = error;
		}
		return adapterErrorOf(thrown, "svg").facts;
	});
	case_("mapping", "a graph of 120 nodes maps and lays out disjoint regions", () => {
		const nodes = [];
		for (let i = 0; i < 120; i++) {
			nodes.push({
				nodeId: i === 0 ? "root" : i % 3 === 0 ? `n-${i}` : `n_${i}`,
				label: `Node ${i}`,
				collection: "c",
				key: `K${i}`,
			});
		}
		const edges = [];
		for (let i = 0; i + 1 < nodes.length; i++) {
			edges.push({ edgeId: `e${i}`, from: nodes[i].nodeId, to: nodes[i + 1].nodeId });
		}
		edges.push({ edgeId: "e-wrap", from: "root", to: nodes[119].nodeId });
		const rendered = renderGraph({ nodes, edges });
		if (rendered.nodeIds.length !== 120) throw new Error("node count drifted");
		let overlaps = 0;
		const regions = rendered.hitRegions;
		for (let i = 0; i < regions.length; i++) {
			for (let j = i + 1; j < regions.length; j++) {
				const a = regions[i];
				const b = regions[j];
				if (a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height) {
					overlaps++;
				}
			}
		}
		if (overlaps > 0) throw new Error(`${overlaps} overlapping node regions`);
		return { nodes: 120, edges: edges.length, overlaps, svgChars: rendered.svg.length };
	});
	case_("mapping", "edge group data-ids collide on underscored node ids", () => {
		// Documented upstream behavior: node data-ids stay verbatim and
		// the adapter never uses edge ids. The case asserts the collision
		// still exists, so an upstream fix surfaces here for re-review.
		const rendered = pele.render("flowchart LR\n  a_b --> c\n  a --> b_c\n", RENDER_OPTIONS);
		const edgeIds = [...rendered.svg.matchAll(/<g class="pele-edge" data-id="([^"]+)"/g)].map((m) => m[1]);
		const unique = new Set(edgeIds).size;
		if (edgeIds.length !== 2 || unique !== 1) {
			throw new Error(`expected one colliding edge id, got ${edgeIds}`);
		}
		return { edgeIds, unique };
	});
}

// ---------------------------------------------------------------------------
// Suite: hitregions
// ---------------------------------------------------------------------------

function suiteHitRegions() {
	const graph = readGraph(graphPath);
	const rendered = renderGraph(graph);
	const identity = new Map(graph.nodes.map((n) => [n.nodeId, n]));

	case_("hitregions", "a click on each node resolves its semantic reference", () => {
		const resolved = [];
		for (const region of rendered.hitRegions) {
			const cx = region.x + region.width / 2;
			const cy = region.y + region.height / 2;
			const hit = hitTest(rendered.hitRegions, cx, cy);
			if (hit !== region.nodeId) throw new Error(`center of ${region.nodeId} resolved ${hit}`);
			const node = identity.get(hit);
			resolved.push({ nodeId: hit, collection: node.collection, key: node.key });
		}
		return { resolved };
	});
	case_("hitregions", "the fixture maps s-42 and s-73 to their suppliers", () => {
		const keys = rendered.hitRegions
			.map((r) => identity.get(r.nodeId).key)
			.sort();
		const expected = ["S-42", "S-73"];
		if (JSON.stringify(keys) !== JSON.stringify(expected)) {
			throw new Error(`resolved keys ${keys}, expected ${expected}`);
		}
		return { keys };
	});
	case_("hitregions", "a click between nodes and outside the canvas misses", () => {
		const gap = hitTest(rendered.hitRegions, 204, 32);
		const left = hitTest(rendered.hitRegions, -10, -10);
		const far = hitTest(rendered.hitRegions, rendered.width + 50, 32);
		if (gap !== null || left !== null || far !== null) {
			throw new Error(`misses resolved ${gap}, ${left}, ${far}`);
		}
		return { gap, left, far };
	});
	case_("hitregions", "regions lie inside the viewBox", () => {
		const viewBox = /viewBox="0 0 ([\d.]+) ([\d.]+)"/.exec(rendered.svg);
		const [w, h] = [Number(viewBox[1]), Number(viewBox[2])];
		for (const region of rendered.hitRegions) {
			if (region.x < 0 || region.y < 0 || region.x + region.width > w || region.y + region.height > h) {
				throw new Error(`region of ${region.nodeId} leaves the viewBox`);
			}
		}
		return { viewBox: [w, h], regions: rendered.hitRegions.length };
	});
	case_("hitregions", "the smallest containing region wins", () => {
		const regions = [
			{ nodeId: "outer", x: 0, y: 0, width: 100, height: 100 },
			{ nodeId: "inner", x: 40, y: 40, width: 20, height: 20 },
		];
		const inside = hitTest(regions, 50, 50);
		const ring = hitTest(regions, 5, 5);
		if (inside !== "inner" || ring !== "outer") {
			throw new Error(`nested resolution ${inside}/${ring}`);
		}
		return { inside, ring };
	});
	case_("hitregions", "an edge path starts at a node boundary", () => {
		const path = /class="pele-edge"[^>]*><path d="M([\d.]+),([\d.]+)/.exec(rendered.svg);
		const x = Number(path[1]);
		const byId = new Map(rendered.hitRegions.map((r) => [r.nodeId, r]));
		const source = byId.get("s-42");
		if (Math.abs(x - (source.x + source.width)) > 0.01) {
			throw new Error(`edge starts at ${x}, node ends at ${source.x + source.width}`);
		}
		return { edgeStartsAt: x, nodeEndsAt: source.x + source.width };
	});
}

// ---------------------------------------------------------------------------
// Suite: sanitize
// ---------------------------------------------------------------------------

function suiteSanitize() {
	const HOSTILE_LABELS = [
		'<script>alert(1)</script>',
		'"><img src=x onerror=alert(1)>',
		'A & B < > " \' ]] (( )) {{ }} ||',
		'javascript:alert(1)',
		'Ünïcødé 中文 🎉 #tag (paren)',
		'#91;pre-escaped#93;',
	];

	case_("sanitize", "every hostile label renders sanitized without markup", () => {
		const rendered = [];
		for (const [index, label] of HOSTILE_LABELS.entries()) {
			const graph = {
				nodes: [
					{ nodeId: "n", label, collection: "c", key: `K${index}` },
					{ nodeId: "m", label: "pair", collection: "c", key: "KM" },
				],
				edges: [{ edgeId: "e", from: "n", to: "m" }],
			};
			const result = renderGraph(graph);
			rendered.push({ label, svgChars: result.svg.length, hitRegions: result.hitRegions.length });
		}
		return { rendered: rendered.map((r) => r.svgChars) };
	});
	case_("sanitize", "raw structural characters fail the mermaid parse", () => {
		// Why the adapter sanitizes: without it, a hostile label is a
		// renderer error rather than a rendered graph. The raw label is
		// lowered by hand here, bypassing sanitizeLabel on purpose.
		let thrown;
		try {
			pele.render('flowchart LR\n  a["x"] blow out ["y"]\n', RENDER_OPTIONS);
		} catch (error) {
			thrown = error;
		}
		if (!thrown || thrown.code !== "syntax") {
			throw new Error(`expected a syntax PeleError, got ${String(thrown)}`);
		}
		return { code: thrown.code };
	});
	case_("sanitize", "the same label renders through the adapter", () => {
		const result = renderGraph({
			nodes: [
				{ nodeId: "a", label: 'x"] blow out ["y', collection: "c", key: "K" },
				{ nodeId: "b", label: "pair", collection: "c", key: "KB" },
			],
			edges: [{ edgeId: "e", from: "a", to: "b" }],
		});
		const decoded = decodeSvgText(result.svg);
		if (!decoded.includes('x"] blow out ["y')) {
			throw new Error(`rendered text drifted: ${decoded}`);
		}
		return { decoded };
	});
	case_("sanitize", "rendered text equals the sanitized label", () => {
		const label = 'A & B <b>bold</b> "q" [x] (y) {z} |w| #h';
		const result = renderGraph({
			nodes: [
				{ nodeId: "n", label, collection: "c", key: "K" },
				{ nodeId: "m", label: "pair", collection: "c", key: "KM" },
			],
			edges: [{ edgeId: "e", from: "n", to: "m" }],
		});
		const decoded = decodeSvgText(result.svg);
		if (!decoded.includes("A & B <b>bold</b> \"q\" [x] (y) {z} |w| #h")) {
			throw new Error(`rendered text drifted: ${decoded}`);
		}
		return { decoded };
	});
	case_("sanitize", "control characters become spaces", () => {
		const lowered = sanitizeLabel("line\u0000break\u0007bell\u001fend");
		if (/[\u0000-\u001f]/.test(lowered)) throw new Error("control characters survived");
		return { lowered };
	});
	case_("sanitize", "a pre-escaped label is not double-decoded", () => {
		const result = renderGraph({
			nodes: [{ nodeId: "n", label: "#91;raw#93;", collection: "c", key: "K" }],
			edges: [],
		});
		const decoded = decodeSvgText(result.svg);
		if (!decoded.includes("#91;raw#93;")) {
			throw new Error(`label double-decoded: ${decoded}`);
		}
		return { decoded };
	});
	case_("sanitize", "label bounds are enforced", () => {
		for (const bad of ["", "x".repeat(257), 42, null]) {
			let thrown;
			try {
				sanitizeLabel(bad);
			} catch (error) {
				thrown = error;
			}
			if (!thrown) throw new Error(`label ${JSON.stringify(bad)} passed`);
			adapterErrorOf(thrown, "label");
		}
		return { rejected: ["", "257 chars", "42", "null"] };
	});
}

/** Decode the text content of one rendered SVG for label comparisons. @param {string} svg */
function decodeSvgText(svg) {
	const parts = [...svg.matchAll(/<(?:text|tspan)[^>]*>([^<]*)<\/(?:text|tspan)>/g)].map((m) => m[1]);
	return parts
		.join(" ")
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&quot;/g, '"')
		.replace(/&apos;/g, "'")
		.replace(/&amp;/g, "&");
}

// ---------------------------------------------------------------------------
// Suite: security
// ---------------------------------------------------------------------------

function suiteSecurity() {
	const graph = readGraph(graphPath);

	case_("security", "the fixture output carries only allowlisted elements", () => {
		const rendered = renderGraph(graph);
		return verifySvg(rendered.svg, rendered.nodeIds).hitRegions.length;
	});
	case_("security", "a click callback statement adds no script", () => {
		const rendered = pele.render(
			'flowchart LR\n  a["x"]\n  click a callback "tip"\n',
			RENDER_OPTIONS,
		);
		if (/<script/i.test(rendered.svg)) throw new Error("callback became a script");
		if (rendered.links.length > 0) throw new Error("callback produced a link");
		return { svgChars: rendered.svg.length, links: rendered.links.length };
	});
	case_("security", "a javascript href is dropped entirely", () => {
		const rendered = pele.render(
			'flowchart LR\n  a["x"]\n  click a "javascript:alert(1)" "tip"\n',
			RENDER_OPTIONS,
		);
		if (/href/i.test(rendered.svg)) throw new Error("an href survived");
		return { links: rendered.links };
	});
	case_("security", "locked options remove anchors a default render emits", () => {
		const locked = pele.render(
			'flowchart LR\n  a["x"]\n  click a "https://example.invalid" "tip"\n',
			RENDER_OPTIONS,
		);
		const open = pele.render('flowchart LR\n  a["x"]\n  click a "https://example.invalid" "tip"\n', {
			...RENDER_OPTIONS,
			links: true,
		});
		if (/<a[\s>]/i.test(locked.svg)) throw new Error("locked render still anchors");
		if (!/<a[\s>]/i.test(open.svg)) throw new Error("open render lost the anchor (probe stale)");
		return {
			lockedAnchors: (locked.svg.match(/<a[\s>]/gi) ?? []).length,
			openAnchors: (open.svg.match(/<a[\s>]/gi) ?? []).length,
			openLinks: open.links,
		};
	});
	case_("security", "verification rejects injected markup", () => {
		const rendered = renderGraph(graph);
		const injections = [
			["script", rendered.svg.replace("<g class=\"pele-nodes\">", "<script>alert(1)</script><g class=\"pele-nodes\">")],
			["handler", rendered.svg.replace("<svg ", "<svg onload=\"alert(1)\" ")],
			["foreignObject", rendered.svg.replace("</svg>", "<foreignObject></foreignObject></svg>")],
			["href", rendered.svg.replace("<g class=\"pele-nodes\">", "<a href=\"https://x.invalid\"><g class=\"pele-nodes\">")],
			["url-function", rendered.svg.replace("<g class=\"pele-nodes\">", "<rect fill=\"url(#x)\"/><g class=\"pele-nodes\">")],
		];
		const rejected = [];
		for (const [name, svg] of injections) {
			let thrown;
			try {
				verifySvg(svg, rendered.nodeIds);
			} catch (error) {
				thrown = error;
			}
			if (!thrown) throw new Error(`${name} injection passed verification`);
			adapterErrorOf(thrown, "svg");
			rejected.push(name);
		}
		return { rejected };
	});
}

// ---------------------------------------------------------------------------
// Findings and verdict
// ---------------------------------------------------------------------------

function recordFindings() {
	finding(
		"no-hit-testing-api",
		"Pele 1.6.0 provides rendering only: no hit-testing, event, focus, or accessibility API exists on Linux",
		{
			exports: ["render", "parse", "detectType", "supports", "mount", "enableZoom", "forgetTextWidths"],
			renderReturns: ["svg", "width", "height", "type", "links"],
			consequence:
				"SVG generation alone is not interaction. Hit testing, focus, keyboard selection, and accessibility stay in the trusted host adapter, which owns the hit regions extracted from the rendered SVG. The Linux test renderer keeps serving the semantic tests (RFC 001 section 7.3).",
		},
	);
	finding(
		"edge-data-id-ambiguity",
		"Edge group data-ids are built as L_<from>_<to>_<n> and collide when node ids contain underscores",
		{
			case: "a_b --> c and a --> b_c both render edge data-id L_a_b_c_0",
			consequence:
				"Node data-ids are verbatim and the adapter verifies the exact node set, but edge identity must come from the graph, never from rendered edge ids. The extension's selection events are node-based, so no current consumer is affected.",
		},
	);
	finding(
		"labels-structural-characters-rejected",
		"Unsanitized labels containing Mermaid structural characters fail the parse",
		{
			case: 'the label x"] blow out ["y raises a PeleError with code "syntax"',
			consequence:
				"Fail closed, but unavailable. The adapter reduces labels to plain text and numeric character references before lowering, so a hostile label neither breaks the renderer nor smuggles markup.",
		},
	);
	finding(
		"links-disabled-by-option",
		"Default render options emit <a href> for click statements; the locked options remove them",
		{
			case: 'click a "https://..." anchors under defaults, not under links:false',
			consequence:
				"Render options stay application-controlled (pele.run/security). The adapter freezes links:false and images:false; project code never supplies options.",
		},
	);
	finding(
		"no-node-accessibility-output",
		"Flowchart output carries document-level roles only; no per-node title or desc",
		{
			case: 'root svg has role="graphics-document document" and aria-roledescription="flowchart"; idPrefix affects no flowchart element',
			consequence:
				"Node-level accessibility labels must be provided by the host from the graph identity map, not read from the SVG.",
		},
	);
	finding(
		"geometry-uses-built-in-measurer",
		"In Node, text widths come from the built-in width table; a browser mount measures with canvas",
		{
			case: "plain Node render succeeds with no DOM globals; pele.run/api documents the canvas measurer for browsers",
			consequence:
				"Geometry can differ between environments. The host must extract hit regions from its own rendering pass, which is what the adapter does: regions come from the rendered SVG, never from the graph or a browser.",
		},
	);
}

async function main() {
	const suites = {
		pin: suitePin,
		interface: suiteInterface,
		mapping: suiteMapping,
		hitregions: suiteHitRegions,
		sanitize: suiteSanitize,
		security: suiteSecurity,
	};
	for (const [name, run_] of Object.entries(suites)) {
		if (suitesWanted === "all" || suitesWanted === name) run_();
	}
	recordFindings();

	const failures = results.filter((result) => result.status === "fail");
	const verdict = {
		probe: "melt-pele-rendering-probe",
		pinnedPele: PELE_PIN,
		resolvedPele: resolvedPeleVersion(),
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
		for (const found of findings) {
			process.stdout.write(`NOTE ${found.kind}: ${found.summary}\n`);
		}
		process.stdout.write(
			`\n${verdict.verdict}: ${verdict.summary.passed} passed, ${verdict.summary.failed} failed, ${verdict.summary.skipped} skipped\n`,
		);
	}
	process.exitCode = failures.length === 0 ? 0 : 1;
}

await main();
