import { readFileSync, writeFileSync } from "node:fs";
import { renderGraph } from "../../runtime/rendering/pele_adapter/pele_render.mjs";
const graph = JSON.parse(readFileSync(process.argv[2], "utf8"));
writeFileSync(process.argv[3], renderGraph(graph).svg);
