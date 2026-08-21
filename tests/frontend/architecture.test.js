import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const frontend = path.join(root, "frontend");

async function javascriptFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map((entry) => {
      const target = path.join(directory, entry.name);
      return entry.isDirectory() ? javascriptFiles(target) : entry.name.endsWith(".js") ? [target] : [];
    })
  );
  return nested.flat();
}

function localImports(source, file) {
  return [...source.matchAll(/^import\s+.*?from\s+["']([^"']+)["'];?$/gm)]
    .map((match) => match[1])
    .filter((specifier) => !specifier.startsWith("/vendor/"))
    .map((specifier) =>
      specifier.startsWith("/")
        ? path.join(frontend, specifier.slice(1))
        : path.resolve(path.dirname(file), specifier)
    );
}

test("main.js remains a composition-only entrypoint", async () => {
  const source = await readFile(path.join(frontend, "main.js"), "utf8");
  assert.ok(source.trim().split("\n").length <= 10);
  assert.match(source, /createApp[(][)][.]mount[(][)]/);
});

test("review controller delegates feature responsibilities", async () => {
  const directory = path.join(frontend, "modules", "review");
  const controller = await readFile(path.join(directory, "controller.js"), "utf8");
  assert.ok(
    controller.trim().split("\n").length <= 700,
    "review/controller.js should remain an orchestration layer"
  );
  for (const moduleName of [
    "analysis-runner.js",
    "artifacts.js",
    "chat.js",
    "coach.js",
    "graph.js",
    "navigation.js",
    "notation.js",
    "progress.js",
    "retry.js",
    "summary-view.js",
    "variation.js",
    "workspace-view.js",
  ]) {
    assert.match(controller, new RegExp(`from ["']\\./${moduleName.replace(".", "\\.")}["']`));
  }
});

test("business modules do not bypass the HTTP client", async () => {
  const files = await javascriptFiles(path.join(frontend, "modules"));
  for (const file of files) {
    if (file.endsWith(path.join("core", "http.js"))) continue;
    const source = await readFile(file, "utf8");
    assert.doesNotMatch(source, /\bfetch\s*[(]/, path.relative(root, file));
  }
});

test("endpoint paths stay inside API modules", async () => {
  const files = await javascriptFiles(path.join(frontend, "modules"));
  for (const file of files) {
    if (file.includes(`${path.sep}api${path.sep}`)) continue;
    const source = await readFile(file, "utf8");
    assert.doesNotMatch(source, /["'`]\/api\//, path.relative(root, file));
  }
});

test("frontend module imports are acyclic", async () => {
  const files = await javascriptFiles(path.join(frontend, "modules"));
  const graph = new Map();
  for (const file of files) {
    const source = await readFile(file, "utf8");
    graph.set(file, localImports(source, file).filter((target) => files.includes(target)));
  }
  const visiting = new Set();
  const visited = new Set();
  function visit(file) {
    if (visiting.has(file)) throw new Error(`Cyclic import at ${path.relative(frontend, file)}`);
    if (visited.has(file)) return;
    visiting.add(file);
    for (const dependency of graph.get(file) || []) visit(dependency);
    visiting.delete(file);
    visited.add(file);
  }
  for (const file of files) visit(file);
  assert.equal(visited.size, files.length);
});
