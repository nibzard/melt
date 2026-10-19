# Pele rendering adapter probe

Backlog task T028. External prerequisite for the RFC 001 semantic
extension (section 7.3): pin a Pele version, establish its actual Linux
interface, prove the rendered-element to node-ID mapping, sanitize labels,
disable external resources in rendered output, and report limitations
instead of claiming interaction from SVG generation alone.

## Pin

| Item | Value |
|---|---|
| Renderer package | `pele` (obsidianmd/pele, pele.run) |
| Pinned version | `1.6.0` (exact, no range) |
| Source | npm registry, recorded in `package-lock.json` |
| Node engine | `>=20` (renderer requirement); probed on v24.18.0 |
| Used entry points | `render`, `parse`, `PeleError`, `detectType` |
| Not used | `mount`, `enableZoom` (browser-side), `icons`, `config` |

Install the pin with `make pele-setup` (or `npm ci` here).
`scripts/check.sh` installs it before the test suite runs. The pin is
asserted at run time by `pele_render.mjs`, so a different installed
version fails the render instead of silently changing output behavior.

## Actual Linux interface of Pele 1.6.0

Established by this probe, not assumed from documentation:

- `render(source: string | DiagramModel, options?: RenderOptions)` runs
  synchronously in plain Node with no browser globals and returns
  `{ svg, width, height, type, links }`. `svg` is a string; there is no
  DOM node, no event callback, and no hit-testing entry point anywhere
  in the module surface (`mount` and `enableZoom` are browser-side).
- Failures throw `PeleError` with `code` in `unsupported-diagram`,
  `syntax`, `semantic`, `limit`, plus `type`, `line`, `column`, and a
  `snippet`. The adapter surfaces these as `PeleAdapterError` with code
  `render`.
- Flowchart output is `<svg class="pele pele-flowchart">` with inline
  `style` attributes referencing `var(--pele-*)` custom properties only.
  Every rendered node is `<g class="pele-node ..." data-id="<node id>"
  transform="translate(x,y)">` holding one shape element and its label
  text. Edge groups carry `data-id` values of the form
  `L_<from>_<to>_<n>`.
- Documented limits, pinned explicitly in `RENDER_OPTIONS` so an
  upstream default change cannot loosen them: 50,000 source characters,
  4,000,000 output characters, 5,000 edges.

## Files

| File | Role |
|---|---|
| `pele_render.mjs` | Application-owned rendering interface. Lowers one validated graph to Mermaid with the graph's own node IDs, sanitizes labels, locks render options, verifies the output, and extracts host-owned hit regions. |
| `probe.mjs` | Probe driver. Emits a JSON verdict with per-case results, findings, and environment facts. Writes no files. |
| `package.json`, `package-lock.json` | The pin. `node_modules/` is generated and ignored. |

## Proven properties

All cases below pass against the pin (35 cases, under a second). The
pytest wrapper `tests/integration/test_pele_rendering.py` re-runs the
probe and asserts the verdict, so `make check` gates on it.

**Rendered-element to node-ID mapping (the verified renderer mapping)**

- The lowering uses each graph node ID verbatim as the Mermaid node ID,
  and Pele emits that ID verbatim in the node group's `data-id`. The
  probe verifies the rendered set equals the graph set for the supplier
  fixture, for entity-syntax IDs including hyphens, underscores, and the
  full 64-character form, and for a 120-node chain.
- `verifySvg` fails closed when the rendered identity set differs from
  the graph's, when an ID repeats, or when any element, handler,
  `href`, `url(`, `@import`, doctype, entity declaration, or comment
  appears outside policy.
- Identical input renders byte-identical SVG. A label change keeps
  every `data-id` while the geometry moves, so identity never rides on
  labels, geometry, or element order.

**Host-owned hit regions (RFC 001 section 7.3's alternative path)**

- `extractHitRegions` reads each node group's translate and shape
  bounds (rect, circle, ellipse, polygon) and returns axis-aligned
  regions in SVG user space. `hitTest` resolves a click to the smallest
  containing region, so a node drawn inside a larger shape keeps its
  own clicks.
- The probe clicks every node center of the supplier fixture and
  resolves `s-42 -> suppliers/S-42` and `s-73 -> suppliers/S-73`
  through the graph's identity map. Clicks in the gap between nodes,
  outside the canvas, and outside the viewBox miss.
- An edge path starts exactly at the source node's boundary, the
  regions lie inside the viewBox, and the 120-node stress graph lays
  out pairwise-disjoint regions.
- Identity resolution always reads the graph's identity map. Nothing
  derives an object reference from a label, a hit-test position, or the
  SVG element order.

**Label sanitization and closed output**

- Labels are reduced to plain text before lowering: control characters
  become spaces, the bounds 1–256 of the graph contract are enforced,
  and the Mermaid structural characters (`# " & < > [ ] ( ) { } |`)
  become numeric character references the parser decodes back to the
  original character. Rendered text therefore equals the stored label,
  pre-escaped sequences are not double-decoded, and Unicode and emoji
  survive.
- Unsanitized structural characters fail the Mermaid parse (fail
  closed). The probe shows the same hostile label rendering cleanly
  through the adapter.
- Render options are frozen with `links: false` and `images: false`.
  The probe shows a `click` statement emitting an `<a href>` under
  default options and no anchor under the locked ones, which is why
  options stay application-controlled and project code never supplies
  them (pele.run/security).
- `click` callbacks are ignored (no script), `javascript:` hrefs are
  dropped, and injected markup (script, handler, `foreignObject`,
  `href`, `url(`) is rejected by verification.

## Findings and limitations

These are reported, not worked around silently. They bound what the
extension may claim about the pinned renderer.

- **F1 — no hit-testing or event API.** Pele 1.6.0 renders a string and
  nothing more. Hit testing, focus, keyboard selection, and
  accessibility belong to the trusted host adapter, which owns the hit
  regions extracted here. SVG generation alone is not interaction; the
  Linux semantic tests keep driving the test renderer
  (`runtime/harness/test_renderer.py`), and native interaction is the
  macOS round.
- **F2 — rendered edge IDs are ambiguous.** Edge `data-id`s are
  `L_<from>_<to>_<n>`; `a_b --> c` and `a --> b_c` both render
  `L_a_b_c_0`. Node `data-id`s are verbatim and set-verified, and the
  extension's selection events are node-based, so no current consumer
  is affected. Any future edge-level identity must come from the graph.
- **F3 — structural labels fail the parse unsanitized.** A label such
  as `x"] blow out ["y` is a renderer error, not a rendered graph. The
  adapter's sanitization keeps hostile labels renderable and inert.
- **F4 — link and image output is option-controlled.** External
  references disappear only because the adapter disables them; the
  defaults anchor and load images. The lock is the boundary.
- **F5 — no per-node accessibility output.** Flowchart SVG carries the
  document-level `role` and `aria-roledescription` only; `idPrefix`
  prefixed no flowchart element. Node-level accessibility labels must
  come from the host reading the identity map.
- **F6 — geometry depends on the text measurer.** In Node, widths come
  from the built-in table; a browser `mount` measures with canvas, so
  geometry can differ between environments. The host must derive hit
  regions from its own rendering pass — which is what this adapter
  does: regions are extracted from the rendered SVG, never predicted
  from the graph.

## Environment

Probed on Linux 6.8.0-147-generic, Node v24.18.0, `pele@1.6.0` from the
committed lockfile, with no browser globals present. The probe writes no
files and spawns no servers; its only subprocess is `npm ls` to confirm
the installed tree matches the lock.
