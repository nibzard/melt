#!/usr/bin/env node
// Verify canonical hashes across the supported languages (SPEC.md
// sections 4.2 and 18.1, backlog tasks T020 and T007).
//
// Python is the reference implementation: python/melt/schema/canonical.py
// computes the hashes recorded in fixtures/valid/document-hashes.json and
// fixtures/semantic/document-hashes.json. This script proves the
// agreement of every JavaScript implementation:
//
//   1. The reference vectors of tests/contract/jcs/ canonicalize to the
//      exact expected text (the same shared fixtures the Python suite
//      runs, RFC 8785 appendix H).
//   2. Every valid v0.1 document fixture hashes to its recorded value
//      through an independent RFC 8785 canonicalizer written directly
//      in this file, together with the ad-hoc wire-form mirror below.
//   3. Every entry of both hash indexes also hashes to its recorded
//      value through the generated TypeScript models of
//      protocol/generated/melt-document.ts: those models validate the
//      document shape, normalize it into the wire form (every field
//      present, absent optional fields explicit nulls or their
//      defaults), and canonicalize the result (SPEC.md section 17).
//      The semantic fixtures join the agreement through this path, so
//      the v0.1 pass of step 2 stays unchanged.
//
// scripts/check.sh runs this verifier, and the contract suite runs it
// again through tests/contract/test_fixtures.py. The script uses no npm
// dependency, only the Node standard library. Exit status: 0 when every
// hash agrees, 1 on any disagreement.
//
// Usage: node scripts/verify_canonical_hashes.mjs [--root <repo-root>]

import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { canonicalDocumentHash } from "../protocol/generated/melt-document.ts";

const VECTOR_NAMES = [
  "arrays",
  "french",
  "structures",
  "unicode",
  "values",
  "weird",
];

function parseArguments(argv) {
  const root = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "..",
  );
  const args = { root };
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--root" && argv[index + 1] !== undefined) {
      args.root = path.resolve(argv[index + 1]);
      index += 1;
    } else {
      throw new Error(`unknown argument: ${argv[index]}`);
    }
  }
  return args;
}

// ---------------------------------------------------------------------------
// RFC 8785 canonicalization in JavaScript
// ---------------------------------------------------------------------------

function assertScalarString(value, where) {
  // Melt strings hold valid Unicode scalar values: every surrogate
  // must be a complete pair (SPEC.md section 4.2).
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const low = value.charCodeAt(index + 1);
      if (!(low >= 0xdc00 && low <= 0xdfff)) {
        throw new Error(`${where}: lone high surrogate at index ${index}`);
      }
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      throw new Error(`${where}: lone low surrogate at index ${index}`);
    }
  }
}

function canonicalize(value, where) {
  if (value === null) {
    return "null";
  }
  switch (typeof value) {
    case "boolean":
      return value ? "true" : "false";
    case "string":
      assertScalarString(value, where);
      // JSON.stringify escapes exactly the characters RFC 8785
      // requires, with lowercase hexadecimal escapes.
      return JSON.stringify(value);
    case "number":
      // 1e999 parses to Infinity; every boundary rejects non-finite
      // numbers (SPEC.md section 4.2).
      if (!Number.isFinite(value)) {
        throw new Error(`${where}: non-finite number`);
      }
      // String(n) is the ECMAScript number serialization RFC 8785
      // prescribes: shortest round-trip, -0 becomes "0".
      return String(value);
    case "object": {
      if (Array.isArray(value)) {
        const parts = value.map((item, index) =>
          canonicalize(item, `${where}[${index}]`),
        );
        return `[${parts.join(",")}]`;
      }
      // Array#sort without a comparator compares UTF-16 code units,
      // the key order RFC 8785 section 3.2.3 defines.
      const keys = Object.keys(value).sort();
      const parts = keys.map((key) => {
        assertScalarString(key, `${where}: key`);
        return `${JSON.stringify(key)}:${canonicalize(value[key], `${where}/${key}`)}`;
      });
      return `{${parts.join(",")}}`;
    }
    default:
      throw new Error(`${where}: unsupported value of type ${typeof value}`);
  }
}

function canonicalHash(value, where) {
  const bytes = canonicalize(value, where);
  return `sha256:${createHash("sha256").update(bytes, "utf8").digest("hex")}`;
}

// ---------------------------------------------------------------------------
// The document wire form
// ---------------------------------------------------------------------------

const CONSTRAINT_FIELDS = ["enum", "maxLength", "maximum", "minimum"];
const COLUMN_FIELDS = ["label", "precision", "scale"];

// The recorded hash covers the document in its wire form, the shape
// python/melt/schema/document.py serializes: absent optional bounds of
// parameter constraints, absent optional fields of table columns, and
// an absent table rowKey are explicit nulls on the wire. Number-typed
// fields serialize through the IEEE 754 double either way, so an
// integer literal and its float form hash identically. This function
// mirrors that normalization for a freshly parsed v0.1 document (it
// mutates its input); the generated TypeScript and Swift models of
// protocol/generated apply the complete rule of the vocabulary — every
// model field present, absent optional fields carrying their defaults —
// so they cover the semantic extension fixtures as well (backlog task
// T007).
function toWireForm(document) {
  for (const parameter of Object.values(document.parameters ?? {})) {
    if (parameter.constraints) {
      for (const field of CONSTRAINT_FIELDS) {
        if (!(field in parameter.constraints)) {
          parameter.constraints[field] = null;
        }
      }
    }
  }
  for (const source of Object.values(document.sources ?? {})) {
    for (const version of Object.values(source.versions ?? {})) {
      const schema = version.schema ?? {};
      if (Array.isArray(schema.columns)) {
        if (!("rowKey" in schema)) {
          schema.rowKey = null;
        }
        for (const column of schema.columns) {
          for (const field of COLUMN_FIELDS) {
            if (!(field in column)) {
              column[field] = null;
            }
          }
        }
      }
    }
  }
  return document;
}

// ---------------------------------------------------------------------------
// The checks
// ---------------------------------------------------------------------------

function checkReferenceVectors(root, failures) {
  const directory = path.join(root, "tests", "contract", "jcs");
  for (const name of VECTOR_NAMES) {
    const inputText = readFileSync(
      path.join(directory, `input-${name}.json`),
      "utf8",
    );
    const expected = readFileSync(
      path.join(directory, `expected-${name}.json`),
      "utf8",
    );
    // Note: JSON.parse keeps the last of duplicate object keys; the
    // strict-parse rules are enforced by the Python suite. The
    // reference vectors carry no duplicate keys.
    const canonical = canonicalize(JSON.parse(inputText), `vector ${name}`);
    if (canonical !== expected) {
      failures.push(`reference vector ${name} disagrees`);
      process.stderr.write(
        `vector ${name}:\n  javascript: ${canonical}\n  expected:   ${expected}\n`,
      );
    }
  }
  return VECTOR_NAMES.length;
}

function checkDocumentHashes(root, failures) {
  const validDirectory = path.join(root, "fixtures", "valid");
  const recorded = JSON.parse(
    readFileSync(path.join(validDirectory, "document-hashes.json"), "utf8"),
  ).documents;
  const fixtureNames = readdirSync(validDirectory)
    .filter((name) => name.endsWith(".json") && name !== "document-hashes.json")
    .sort();
  const recordedNames = Object.keys(recorded).sort();
  if (fixtureNames.join("\n") !== recordedNames.join("\n")) {
    const unrecorded = fixtureNames.filter((name) => !(name in recorded));
    const absent = recordedNames.filter(
      (name) => !fixtureNames.includes(name),
    );
    failures.push(
      "document-hashes.json does not list exactly the valid fixtures",
    );
    if (unrecorded.length > 0) {
      process.stderr.write(`unrecorded fixtures: ${unrecorded.join(", ")}\n`);
    }
    if (absent.length > 0) {
      process.stderr.write(`recorded but absent: ${absent.join(", ")}\n`);
    }
  }
  let agreed = 0;
  for (const name of fixtureNames) {
    if (!(name in recorded)) {
      continue;
    }
    const text = readFileSync(path.join(validDirectory, name), "utf8");
    const hash = canonicalHash(toWireForm(JSON.parse(text)), name);
    if (hash === recorded[name]) {
      agreed += 1;
    } else {
      failures.push(`${name}: canonical hash disagrees with the recorded value`);
      process.stderr.write(
        `${name}:\n  javascript: ${hash}\n  recorded:   ${recorded[name]}\n`,
      );
    }
  }
  return agreed;
}

function checkGeneratedModelHashes(root, failures) {
  // The generated TypeScript models verify both hash indexes: the v0.1
  // set of fixtures/valid and the semantic set of fixtures/semantic
  // (kept separate so the v0.1 suite stays unchanged, backlog task
  // T007).
  const indexes = [
    { directory: "fixtures/valid", index: "document-hashes.json" },
    { directory: "fixtures/semantic", index: "document-hashes.json" },
  ];
  let agreed = 0;
  let total = 0;
  for (const { directory, index } of indexes) {
    const directoryPath = path.join(root, directory);
    const recorded = JSON.parse(
      readFileSync(path.join(directoryPath, index), "utf8"),
    ).documents;
    for (const name of Object.keys(recorded).sort()) {
      total += 1;
      const text = readFileSync(path.join(directoryPath, name), "utf8");
      let hash;
      try {
        hash = canonicalDocumentHash(text);
      } catch (error) {
        failures.push(
          `${directory}/${name}: the generated TypeScript models reject` +
            ` the document (${error.message})`,
        );
        continue;
      }
      if (hash === recorded[name]) {
        agreed += 1;
      } else {
        failures.push(
          `${directory}/${name}: the generated TypeScript models` +
            " disagree with the recorded hash",
        );
        process.stderr.write(
          `${directory}/${name}:\n  typescript: ${hash}\n  recorded:   ${recorded[name]}\n`,
        );
      }
    }
  }
  return { agreed, total };
}

function main() {
  const { root } = parseArguments(process.argv.slice(2));
  const failures = [];
  const vectors = checkReferenceVectors(root, failures);
  const documents = checkDocumentHashes(root, failures);
  const generated = checkGeneratedModelHashes(root, failures);
  if (failures.length > 0) {
    process.stderr.write(
      `verify-canonical-hashes: ${failures.length} disagreement(s)` +
        " between the JavaScript and Python implementations\n",
    );
    process.exitCode = 1;
    return;
  }
  process.stdout.write(
    `verify-canonical-hashes: ${vectors} reference vectors and ${documents}` +
      " document hashes agree between the JavaScript and Python" +
      " implementations\n",
  );
  process.stdout.write(
    `verify-canonical-hashes: ${generated.agreed} of ${generated.total}` +
      " recorded document hashes agree through the generated" +
      " TypeScript models (fixtures/valid and fixtures/semantic)\n",
  );
}

try {
  main();
} catch (error) {
  process.stderr.write(`verify-canonical-hashes: ${error.message}\n`);
  process.exitCode = 1;
}
