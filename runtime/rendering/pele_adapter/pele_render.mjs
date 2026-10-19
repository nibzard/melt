/**
 * Application-owned Pele rendering interface (RFC 001 section 7.3).
 *
 * Melt code never calls the pinned renderer directly. Every render goes
 * through this module, which lowers one validated graph to Mermaid, locks
 * the render options, sanitizes labels, and verifies the output before the
 * SVG is returned.
 *
 * What this module proves and enforces:
 *
 * - Rendered-element to node-ID mapping. Pele 1.6.0 emits every flowchart
 *   node group with `data-id="<mermaid node id>"`. Because the lowering
 *   uses the graph's own node IDs as Mermaid node IDs, `verifySvg` checks
 *   that the SVG carries exactly the graph's node ID set, no more and no
 *   less. The graph and its identity map stay authoritative; the SVG is
 *   derived (RFC 001 section 7.3).
 * - Host-owned hit regions. Pele exposes no hit-testing or event API. The
 *   adapter extracts each node's shape bounds from the SVG it rendered, so
 *   the trusted host can map a click in SVG user space to a graph node ID,
 *   then through the identity map to a semantic reference. Hit testing,
 *   focus, keyboard selection, and accessibility stay in the host.
 * - Label sanitization and no external resources. Labels are reduced to
 *   plain text and Mermaid numeric character references before lowering,
 *   `links` and `images` are disabled in the locked options, and the
 *   output is verified against a tag allowlist plus scans for handlers,
 *   URL references, and CSS imports.
 *
 * SVG generation alone is not interaction. The renderer never receives
 * events and never resolves identities; those duties belong to the host
 * adapter that owns the hit regions this module extracts.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import pelePackage from "pele/package.json" with { type: "json" };
import { render } from "pele";

export const PELE_PIN = "1.6.0";
/** The pinned renderer's documented limits, stated so an upstream default change cannot loosen them. */
export const PELE_SOURCE_LIMIT = 50_000;
export const PELE_OUTPUT_LIMIT = 4_000_000;
export const PELE_MAX_EDGES = 5_000;

/** The label bounds of the graph contract (python/melt/schema/semantic.py, RFC 001 section 7.1). */
export const LABEL_MIN_CHARS = 1;
export const LABEL_MAX_CHARS = 256;

/** The entity ID syntax of SPEC.md section 3, applied to graph node IDs. */
export const NODE_ID_PATTERN = /^[a-z][a-z0-9_-]{0,63}$/;

/**
 * Locked render options. `links: false` and `images: false` remove every
 * anchor and image reference from the output (probe evidence: with the
 * defaults, a `click` statement emits an `<a href>`; locked, it emits
 * none). Project code never supplies options (pele.run/security).
 */
export const RENDER_OPTIONS = Object.freeze({
	links: false,
	images: false,
	responsive: true,
	fontFamily: "sans-serif",
	fontFamilyMono: "monospace",
	fontSize: 16,
	limit: PELE_SOURCE_LIMIT,
	outputLimit: PELE_OUTPUT_LIMIT,
	maxEdges: PELE_MAX_EDGES,
});

const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/g;

/**
 * Characters that terminate or restructure Mermaid node text, or that an
 * HTML-label path could reinterpret. Each maps to its Mermaid numeric
 * character reference; Pele then escapes the decoded character again for
 * XML, so hostile labels never become markup or structure.
 */
const LABEL_CHARACTER_REFERENCES = new Map([
	["#", "#35;"],
	['"', "#34;"],
	["&", "#38;"],
	["<", "#60;"],
	[">", "#62;"],
	["[", "#91;"],
	["]", "#93;"],
	["(", "#40;"],
	[")", "#41;"],
	["{", "#123;"],
	["}", "#125;"],
	["|", "#124;"],
]);

/** The SVG element allowlist. Pele escapes text, so every `<` opens a tag; anything outside this set fails verification. */
const ALLOWED_TAGS = new Set([
	"svg",
	"g",
	"rect",
	"circle",
	"ellipse",
	"polygon",
	"polyline",
	"path",
	"text",
	"tspan",
	"title",
	"desc",
	"defs",
	"marker",
]);

/** Attribute and value scans over the rendered SVG. Any match fails verification. */
const FORBIDDEN_PATTERNS = [
	["handler-attribute", /<[a-zA-Z][^>]*\son[a-z][a-z0-9-]*\s*=/i],
	["url-attribute", /[\s"']href\s*=/i],
	["url-function", /url\(/i],
	["css-import", /@import/i],
	["doctype", /<!DOCTYPE/i],
	["entity-declaration", /<!ENTITY/i],
	["comment", /<!--/],
];

const NODE_GROUP_PATTERN =
	/<g class="pele-node[^"]*" data-id="([^"]+)"([^>]*)>([\s\S]*?)<\/g>/g;
const TRANSFORM_PATTERN = /^translate\((-?[\d.eE+]+),(-?[\d.eE+]+)\)$/;

export class PeleAdapterError extends Error {
	/**
	 * @param {string} code
	 * @param {string} message
	 * @param {Record<string, unknown> | undefined} facts
	 */
	constructor(code, message, facts = undefined) {
		super(message);
		this.name = "PeleAdapterError";
		this.code = code;
		this.facts = facts;
	}
}

/** Resolved version of the installed renderer package. */
export function resolvedPeleVersion() {
	return pelePackage.version;
}

/** Fail fast when the installed renderer is not the pinned one. */
export function assertPinnedPele() {
	const resolved = resolvedPeleVersion();
	if (resolved !== PELE_PIN) {
		throw new PeleAdapterError(
			"pin",
			`the installed Pele is ${resolved}, but this adapter is pinned to ${PELE_PIN}`,
			{ resolved, pinned: PELE_PIN },
		);
	}
	return resolved;
}

/**
 * Reduce one label to safe Mermaid node text.
 *
 * Control characters become spaces; the structural characters of
 * `LABEL_CHARACTER_REFERENCES` become numeric character references the
 * Mermaid parser decodes back to the original character. The rendered text
 * therefore equals the stored label (with control characters replaced), and
 * no label content can open or close node syntax.
 *
 * @param {unknown} label
 * @returns {string}
 */
export function sanitizeLabel(label) {
	if (typeof label !== "string") {
		throw new PeleAdapterError("label", "a graph node label must be a string", {
			type: typeof label,
		});
	}
	if (label.length < LABEL_MIN_CHARS || label.length > LABEL_MAX_CHARS) {
		throw new PeleAdapterError(
			"label",
			`a graph node label must hold ${LABEL_MIN_CHARS} through ${LABEL_MAX_CHARS} characters; got ${label.length}`,
			{ length: label.length },
		);
	}
	const flattened = label.replace(CONTROL_CHARACTERS, " ");
	let lowered = "";
	for (const character of flattened) {
		lowered += LABEL_CHARACTER_REFERENCES.get(character) ?? character;
	}
	return lowered;
}

/**
 * Validate one graph of the RFC 001 section 7.1 contract and lower it to
 * Mermaid flowchart text.
 *
 * Node IDs become Mermaid node IDs verbatim, so the SVG `data-id` of each
 * rendered node group names the graph node. Collection and key never enter
 * the lowered text; the identity map stays authoritative and separate.
 *
 * @param {unknown} graph
 * @returns {{ mermaid: string, nodeIds: string[] }}
 */
export function lowerGraphToMermaid(graph) {
	if (typeof graph !== "object" || graph === null) {
		throw new PeleAdapterError("graph", "a graph must be an object", {
			type: typeof graph,
		});
	}
	const nodes = graph.nodes;
	const edges = graph.edges ?? [];
	if (!Array.isArray(nodes) || nodes.length === 0) {
		throw new PeleAdapterError("graph", "a graph must carry at least one node", {
			nodeCount: Array.isArray(nodes) ? nodes.length : null,
		});
	}
	if (!Array.isArray(edges)) {
		throw new PeleAdapterError("graph", "graph edges must be an array", {
			type: typeof edges,
		});
	}
	const seen = new Set();
	const nodeIds = [];
	const lines = ["flowchart LR"];
	for (const node of nodes) {
		if (typeof node !== "object" || node === null) {
			throw new PeleAdapterError("graph", "every graph node must be an object", {
				index: nodeIds.length,
			});
		}
		const { nodeId, label } = node;
		if (typeof nodeId !== "string" || !NODE_ID_PATTERN.test(nodeId)) {
			throw new PeleAdapterError(
				"graph",
				`graph node id ${JSON.stringify(nodeId)} does not match the entity ID syntax`,
				{ nodeId },
			);
		}
		if (seen.has(nodeId)) {
			throw new PeleAdapterError(
				"graph",
				`graph node id ${JSON.stringify(nodeId)} repeats; node ids are unique`,
				{ nodeId },
			);
		}
		seen.add(nodeId);
		nodeIds.push(nodeId);
		lines.push(`  ${nodeId}["${sanitizeLabel(label)}"]`);
	}
	for (const [edgeIndex, edge] of edges.entries()) {
		if (typeof edge !== "object" || edge === null) {
			throw new PeleAdapterError("graph", "every graph edge must be an object", {
				index: edgeIndex,
			});
		}
		const { from, to } = edge;
		for (const endpoint of [from, to]) {
			if (!seen.has(endpoint)) {
				throw new PeleAdapterError(
					"graph",
					`a graph edge names node ${JSON.stringify(endpoint)}, which the graph does not carry`,
					{ endpoint },
				);
			}
		}
		lines.push(`  ${from} --> ${to}`);
	}
	return { mermaid: `${lines.join("\n")}\n`, nodeIds };
}

/**
 * Verify one rendered SVG against the locked output policy and the graph's
 * node ID set, and extract the host-owned hit regions.
 *
 * @param {string} svg
 * @param {string[]} expectedNodeIds
 * @returns {{ hitRegions: Array<{nodeId: string, x: number, y: number, width: number, height: number}> }}
 */
export function verifySvg(svg, expectedNodeIds) {
	if (typeof svg !== "string" || svg.length === 0) {
		throw new PeleAdapterError("svg", "the renderer returned no SVG", {
			type: typeof svg,
		});
	}
	if (svg.length > PELE_OUTPUT_LIMIT) {
		throw new PeleAdapterError("svg", "the rendered SVG exceeds the output limit", {
			length: svg.length,
			limit: PELE_OUTPUT_LIMIT,
		});
	}
	for (const [name, pattern] of FORBIDDEN_PATTERNS) {
		const match = pattern.exec(svg);
		if (match) {
			throw new PeleAdapterError(
				"svg",
				`the rendered SVG contains forbidden ${name} content`,
				{ name, at: match.index, excerpt: svg.slice(Math.max(0, match.index - 40), match.index + 60) },
			);
		}
	}
	const unexpectedTags = [];
	for (const match of svg.matchAll(/<([a-zA-Z][a-zA-Z0-9:-]*)/g)) {
		if (!ALLOWED_TAGS.has(match[1])) {
			unexpectedTags.push(match[1]);
		}
	}
	if (unexpectedTags.length > 0) {
		throw new PeleAdapterError(
			"svg",
			"the rendered SVG contains elements outside the allowlist",
			{ tags: [...new Set(unexpectedTags)] },
		);
	}
	const renderedIds = [];
	for (const match of svg.matchAll(/<g class="pele-node[^"]*" data-id="([^"]+)"/g)) {
		renderedIds.push(match[1]);
	}
	const expected = [...expectedNodeIds];
	const renderedSorted = [...renderedIds].sort();
	const expectedSorted = [...expected].sort();
	if (renderedSorted.length !== expectedSorted.length ||
		renderedSorted.some((id, index) => id !== expectedSorted[index])) {
		throw new PeleAdapterError(
			"svg",
			"the rendered node identity map does not match the graph",
			{ rendered: renderedSorted, expected: expectedSorted },
		);
	}
	const duplicates = renderedIds.filter((id, index) => renderedIds.indexOf(id) !== index);
	if (duplicates.length > 0) {
		throw new PeleAdapterError(
			"svg",
			"the rendered SVG repeats a node identity",
			{ duplicates: [...new Set(duplicates)] },
		);
	}
	return { hitRegions: extractHitRegions(svg) };
}

/**
 * Extract one hit region per rendered node, in document order.
 *
 * Regions are axis-aligned bounds in SVG user space, taken from each node
 * group's translate and its first shape element. The trusted host maps a
 * click through these bounds to a graph node ID, then through the graph's
 * identity map to a semantic reference.
 *
 * @param {string} svg
 */
export function extractHitRegions(svg) {
	const regions = [];
	for (const match of svg.matchAll(NODE_GROUP_PATTERN)) {
		const [, nodeId, attributes, body] = match;
		const transform = /transform="([^"]*)"/.exec(attributes)?.[1] ?? "";
		const translated = TRANSFORM_PATTERN.exec(transform);
		if (!translated) {
			throw new PeleAdapterError(
				"svg",
				`node ${nodeId} carries an unsupported transform`,
				{ transform },
			);
		}
		const tx = Number(translated[1]);
		const ty = Number(translated[2]);
		regions.push({ nodeId, ...shapeBounds(nodeId, body, tx, ty) });
	}
	return regions;
}

/**
 * Bounds of the first shape element of one node group, offset by the
 * group's translate.
 *
 * @param {string} nodeId
 * @param {string} body
 * @param {number} tx
 * @param {number} ty
 */
function shapeBounds(nodeId, body, tx, ty) {
	const rect = /<rect\s[^>]*x="(-?[\d.eE+]+)"[^>]*y="(-?[\d.eE+]+)"[^>]*width="([\d.eE+]+)"[^>]*height="([\d.eE+]+)"/.exec(body);
	if (rect) {
		return boundsFromBox(nodeId, tx + Number(rect[1]), ty + Number(rect[2]), Number(rect[3]), Number(rect[4]));
	}
	const circle = /<circle\s[^>]*r="([\d.eE+]+)"/.exec(body);
	if (circle) {
		const r = Number(circle[1]);
		return boundsFromBox(nodeId, tx - r, ty - r, 2 * r, 2 * r);
	}
	const ellipse = /<ellipse\s[^>]*rx="([\d.eE+]+)"[^>]*ry="([\d.eE+]+)"/.exec(body);
	if (ellipse) {
		const rx = Number(ellipse[1]);
		const ry = Number(ellipse[2]);
		return boundsFromBox(nodeId, tx - rx, ty - ry, 2 * rx, 2 * ry);
	}
	const polygon = /<polygon\s[^>]*points="([^"]+)"/.exec(body);
	if (polygon) {
		const numbers = polygon[1].trim().split(/[\s,]+/).map(Number);
		const xs = numbers.filter((_, index) => index % 2 === 0);
		const ys = numbers.filter((_, index) => index % 2 === 1);
		const x1 = tx + Math.min(...xs);
		const y1 = ty + Math.min(...ys);
		return boundsFromBox(
			nodeId,
			x1,
			y1,
			Math.max(...xs) - Math.min(...xs),
			Math.max(...ys) - Math.min(...ys),
		);
	}
	throw new PeleAdapterError(
		"svg",
		`node ${nodeId} carries no shape the hit-region extractor supports`,
		{ body: body.slice(0, 120) },
	);
}

/**
 * @param {string} nodeId
 * @param {number} x
 * @param {number} y
 * @param {number} width
 * @param {number} height
 */
function boundsFromBox(nodeId, x, y, width, height) {
	if (![x, y, width, height].every(Number.isFinite) || width < 0 || height < 0) {
		throw new PeleAdapterError(
			"svg",
			`node ${nodeId} carries non-finite shape geometry`,
			{ x, y, width, height },
		);
	}
	return { x, y, width, height };
}

/**
 * Resolve one click in SVG user space to a graph node ID.
 *
 * When regions overlap, the smallest region wins, so a node drawn inside a
 * larger shape still receives its own clicks. Ties break by document order.
 *
 * @param {Array<{nodeId: string, x: number, y: number, width: number, height: number}>} regions
 * @param {number} x
 * @param {number} y
 * @returns {string | null}
 */
export function hitTest(regions, x, y) {
	let best = null;
	let bestArea = Infinity;
	for (const region of regions) {
		if (x < region.x || x > region.x + region.width) continue;
		if (y < region.y || y > region.y + region.height) continue;
		const area = region.width * region.height;
		if (area < bestArea) {
			best = region;
			bestArea = area;
		}
	}
	return best?.nodeId ?? null;
}

/**
 * Render one validated graph through the pinned renderer.
 *
 * @param {unknown} graph
 * @returns {{
 *   mermaid: string,
 *   svg: string,
 *   width: number,
 *   height: number,
 *   type: string,
 *   links: unknown[],
 *   nodeIds: string[],
 *   hitRegions: Array<{nodeId: string, x: number, y: number, width: number, height: number}>,
 * }}
 */
export function renderGraph(graph) {
	assertPinnedPele();
	const { mermaid, nodeIds } = lowerGraphToMermaid(graph);
	let rendered;
	try {
		rendered = render(mermaid, RENDER_OPTIONS);
	} catch (error) {
		const pele = /** @type {any} */ (error);
		throw new PeleAdapterError(
			"render",
			`the pinned renderer rejected the lowered graph: ${pele?.message ?? String(error)}`,
			{
				peleCode: pele?.code ?? null,
				peleType: pele?.type ?? null,
				line: pele?.line ?? null,
				column: pele?.column ?? null,
			},
		);
	}
	const { hitRegions } = verifySvg(rendered.svg, nodeIds);
	return {
		mermaid,
		svg: rendered.svg,
		width: rendered.width,
		height: rendered.height,
		type: rendered.type,
		links: rendered.links ?? [],
		nodeIds,
		hitRegions,
	};
}

/** The fixture graph this probe renders by default (fixtures/semantic/supplier-network.graph.json). */
export function defaultGraphPath() {
	return join(
		dirname(fileURLToPath(import.meta.url)),
		"../../../",
		"fixtures/semantic/supplier-network.graph.json",
	);
}

/** Read one graph fixture from disk. @param {string} path */
export function readGraph(path) {
	return JSON.parse(readFileSync(path, "utf8"));
}
