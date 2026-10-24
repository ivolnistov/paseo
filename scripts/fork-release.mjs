#!/usr/bin/env node
// Fork package release: the seven owned distribution packages for GitHub Packages.
//
//   node scripts/fork-release.mjs prepare --src DIR --version EXACT --out DIR
//     Packs packages/{cli,server,client,protocol,highlight,plugin,relay} of a clean checkout under their owned
//     names at one exact version, with every internal @getpaseo/* edge aliased to the owned package, and writes
//     <out>/manifest.json. It never builds and never publishes: the build outputs must already exist. Source
//     workspace names, imports and the lockfile are not changed; only the packed package.json is rewritten.
//   node scripts/fork-release.mjs plan-publish --manifest FILE --registry URL
//     Validates a prepared manifest against its tarballs (re-hashed and re-read) and prints the publish order. It
//     never runs npm publish or writes to a registry.
//
// Exit codes: 0 ok, 2 usage, 3 precondition, 4 validation failure.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

export const REGISTRY = "https://npm.pkg.github.com";
export const REPOSITORY_URL = "git+https://github.com/ivolnistov/paseo.git";

// workspace dir -> [source name, owned name]
export const PACKAGES = {
  protocol: ["@getpaseo/protocol", "@ivolnistov/paseo-protocol"],
  highlight: ["@getpaseo/highlight", "@ivolnistov/paseo-highlight"],
  relay: ["@getpaseo/relay", "@ivolnistov/paseo-relay"],
  client: ["@getpaseo/client", "@ivolnistov/paseo-client"],
  plugin: ["@getpaseo/plugin", "@ivolnistov/paseo-plugin"],
  server: ["@getpaseo/server", "@ivolnistov/paseo-server"],
  cli: ["@getpaseo/cli", "@ivolnistov/paseo"],
};
// Publish order: components first, the CLI entry last (each package after the ones it depends on).
export const PUBLISH_ORDER = Object.values(PACKAGES).map(([, owned]) => owned);
const TARGET_OF = Object.fromEntries(Object.values(PACKAGES));
const DEP_SECTIONS = [
  "dependencies",
  "optionalDependencies",
  "peerDependencies",
  "devDependencies",
];
const EXACT_VERSION = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/;

class Stop extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function parseArgs(argv, names, optional = []) {
  const out = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i]?.replace(/^--/, "");
    if (![...names, ...optional].includes(key) || argv[i + 1] === undefined)
      throw new Stop(2, `unexpected argument ${argv[i]}`);
    out[key] = argv[i + 1];
  }
  for (const name of names) if (!out[name]) throw new Stop(2, `--${name} is required`);
  return out;
}

function sha256(file) {
  return createHash("sha256").update(readFileSync(file)).digest("hex");
}

function git(src, ...args) {
  return execFileSync("git", ["-C", src, ...args], { encoding: "utf8" }).trim();
}

// The aliased form of an internal edge: npm:<owned>@<version>.
function aliasOf(sourceName, version) {
  return `npm:${TARGET_OF[sourceName]}@${version}`;
}

function rewriteEdges(pkg, version) {
  const out = {};
  for (const section of DEP_SECTIONS) {
    if (!pkg[section]) continue;
    out[section] = Object.fromEntries(
      Object.entries(pkg[section]).map(([key, spec]) => {
        if (!key.startsWith("@getpaseo/")) return [key, spec];
        if (!TARGET_OF[key])
          throw new Stop(4, `${pkg.name} ${section} ${key} is not an owned package`);
        // Peers keep their key and get the same exact alias; an npm resolver proof of alias peers is pending.
        return [key, aliasOf(key, version)];
      }),
    );
  }
  return out;
}

// The files npm itself would pack from the source package directory (honours files/.npmignore), without
// running lifecycle scripts.
function packedFileList(dir) {
  const json = execFileSync("npm", ["pack", "--dry-run", "--json", "--ignore-scripts"], {
    cwd: dir,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  const [result] = JSON.parse(json);
  return result.files.map((f) => f.path);
}

function prepare(argv) {
  const args = parseArgs(argv, ["src", "version", "out"]);
  const src = path.resolve(args.src);
  const out = path.resolve(args.out);
  const { version } = args;
  if (!EXACT_VERSION.test(version))
    throw new Stop(2, `--version must be an exact version: ${version}`);
  if (existsSync(out) && readdirSync(out).length > 0)
    throw new Stop(3, `${out} is not empty; use a new --out`);
  let sha;
  try {
    sha = git(src, "rev-parse", "HEAD");
  } catch {
    throw new Stop(3, `${src} is not a git checkout`);
  }
  if (git(src, "status", "--porcelain"))
    throw new Stop(3, `${src} has uncommitted or untracked changes`);
  const tree = git(src, "rev-parse", "HEAD^{tree}");

  const staging = mkdtempSync(path.join(tmpdir(), "paseo-fork-release-"));
  const packOut = path.join(staging, "packed");
  mkdirSync(packOut);
  const entries = [];
  try {
    for (const [dir, [sourceName, name]] of Object.entries(PACKAGES)) {
      const pkgDir = path.join(src, "packages", dir);
      const pkg = JSON.parse(readFileSync(path.join(pkgDir, "package.json"), "utf8"));
      if (pkg.name !== sourceName)
        throw new Stop(3, `packages/${dir} is ${pkg.name}, expected ${sourceName}`);
      if (pkg.private) throw new Stop(3, `${sourceName} is private`);
      // Build outputs are required inputs: the packer never builds.
      const required = [pkg.main, ...(pkg.files ?? [])].filter(
        (f) => typeof f === "string" && !f.includes("*"),
      );
      // The daemon serves its web UI from dist/server/web-ui (npm run build:daemon-web-ui).
      if (dir === "server") required.push("dist/server/web-ui/index.html");
      const missing = required.filter((f) => !existsSync(path.join(pkgDir, f)));
      if (missing.length) {
        throw new Stop(
          3,
          `${sourceName} build outputs missing: ${missing.join(", ")}; build them first`,
        );
      }
      const stage = path.join(staging, dir);
      for (const file of packedFileList(pkgDir)) {
        if (file === "package.json") continue;
        mkdirSync(path.dirname(path.join(stage, file)), { recursive: true });
        cpSync(path.join(pkgDir, file), path.join(stage, file));
      }
      const edges = rewriteEdges(pkg, version);
      const dist = {
        ...pkg,
        ...edges,
        name,
        version,
        publishConfig: { registry: REGISTRY },
        repository: { type: "git", url: REPOSITORY_URL, directory: `packages/${dir}` },
      };
      writeFileSync(path.join(stage, "package.json"), `${JSON.stringify(dist, null, 2)}\n`);
      const packed = JSON.parse(
        execFileSync("npm", ["pack", "--json", "--ignore-scripts", "--pack-destination", packOut], {
          cwd: stage,
          encoding: "utf8",
          stdio: ["ignore", "pipe", "pipe"],
        }),
      )[0].filename;
      entries.push({
        sourceName,
        name,
        version,
        tarball: packed,
        sha256: sha256(path.join(packOut, packed)),
        dependencies: edges.dependencies ?? {},
        optionalDependencies: edges.optionalDependencies ?? {},
        peerDependencies: edges.peerDependencies ?? {},
      });
    }
    mkdirSync(out, { recursive: true });
    for (const entry of entries)
      renameSync(path.join(packOut, entry.tarball), path.join(out, entry.tarball));
    const manifest = {
      schema: "paseo-fork-release-v1",
      version,
      registry: REGISTRY,
      source: { sha, tree },
      packages: entries,
    };
    const tmp = path.join(out, `.manifest.json.tmp-${process.pid}`);
    writeFileSync(tmp, `${JSON.stringify(manifest, null, 2)}\n`);
    renameSync(tmp, path.join(out, "manifest.json"));
    for (const entry of entries)
      console.log(`packed ${entry.name}@${version} ${entry.tarball} ${entry.sha256}`);
    console.log(`manifest ${path.join(out, "manifest.json")} (source ${sha}); nothing published`);
  } finally {
    rmSync(staging, { recursive: true, force: true });
  }
}

function readPackedManifest(tarball) {
  return JSON.parse(
    execFileSync("tar", ["-xzOf", tarball, "package/package.json"], { encoding: "utf8" }),
  );
}

const SECTIONS = ["dependencies", "optionalDependencies", "peerDependencies"];

function identityProblems(entry, version) {
  const label = entry?.name ?? "(unnamed)";
  const problems = [];
  if (TARGET_OF[entry?.sourceName] !== entry?.name) {
    problems.push(`${label}: name is not the owned name of ${entry?.sourceName}`);
  }
  if (String(entry?.name).startsWith("@getpaseo/")) problems.push(`${label}: official name`);
  if (entry?.version !== version)
    problems.push(`${label}: version ${entry?.version} differs from ${version}`);
  return problems;
}

function edgeProblems(entry, version) {
  const label = entry?.name ?? "(unnamed)";
  const problems = [];
  for (const section of SECTIONS) {
    for (const [key, spec] of Object.entries(entry?.[section] ?? {})) {
      if (key.startsWith("@getpaseo/") && spec !== aliasOf(key, version)) {
        problems.push(`${label}: ${section} ${key} is ${spec}, not ${aliasOf(key, version)}`);
      }
      if (String(spec).includes("@getpaseo/"))
        problems.push(`${label}: ${section} ${key} targets an official package`);
    }
  }
  return problems;
}

// Re-hashes the tarball and re-reads its package.json: the manifest alone is not trusted.
function tarballProblems(entry, version, baseDir) {
  const label = entry?.name ?? "(unnamed)";
  const rel = String(entry?.tarball ?? "");
  const file = path.resolve(baseDir, rel);
  if (
    !rel.endsWith(".tgz") ||
    path.isAbsolute(rel) ||
    path.dirname(file) !== path.resolve(baseDir)
  ) {
    return [`${label}: tarball path ${rel} is not a .tgz inside the manifest directory`];
  }
  if (!existsSync(file)) return [`${label}: tarball ${rel} missing`];
  if (sha256(file) !== entry.sha256) return [`${label}: tarball sha256 does not match`];
  const packed = readPackedManifest(file);
  const problems = [];
  if (packed.name !== entry.name || packed.version !== version)
    problems.push(`${label}: tarball identity differs`);
  if (packed.publishConfig?.registry !== REGISTRY)
    problems.push(`${label}: tarball registry is not ${REGISTRY}`);
  for (const section of SECTIONS) {
    if (JSON.stringify(packed[section] ?? {}) !== JSON.stringify(entry[section] ?? {})) {
      problems.push(`${label}: tarball ${section} differ from the manifest`);
    }
  }
  return problems;
}

function validate(manifest, baseDir) {
  const problems = [];
  const { version } = manifest;
  if (typeof version !== "string" || !EXACT_VERSION.test(version))
    problems.push("version is not an exact version");
  if (!/^[0-9a-f]{40}$/.test(String(manifest.source?.sha ?? "")))
    problems.push("source.sha is not a full commit SHA");
  const packages = Array.isArray(manifest.packages) ? manifest.packages : [];
  const names = new Set(packages.map((p) => p?.name));
  const expected = Object.values(TARGET_OF);
  if (packages.length !== expected.length || expected.some((n) => !names.has(n))) {
    problems.push(`packages must be exactly ${expected.join(", ")}`);
  }
  for (const entry of packages) {
    problems.push(...identityProblems(entry, version), ...edgeProblems(entry, version));
    problems.push(...tarballProblems(entry, version, baseDir));
  }
  return problems;
}

// --existing: JSON map "name@version" -> the registry's dist.integrity (sha512) for versions already published.
function readExisting(file) {
  if (!file) return {};
  let existing;
  try {
    existing = JSON.parse(readFileSync(path.resolve(file), "utf8"));
  } catch (error) {
    throw new Stop(3, `cannot read --existing ${file}: ${error.message}`);
  }
  if (!existing || typeof existing !== "object" || Array.isArray(existing)) {
    throw new Stop(3, "--existing must be a JSON object of name@version to integrity");
  }
  return existing;
}

function sha512Integrity(file) {
  return `sha512-${createHash("sha512").update(readFileSync(file)).digest("base64")}`;
}

function planPublish(argv) {
  const args = parseArgs(argv, ["manifest", "registry"], ["version", "source-sha", "existing"]);
  if (args.registry !== REGISTRY)
    throw new Stop(4, `registry must be exactly ${REGISTRY}: ${args.registry}`);
  const manifestPath = path.resolve(args.manifest);
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  } catch (error) {
    throw new Stop(3, `cannot read ${manifestPath}: ${error.message}`);
  }
  const problems = validate(manifest, path.dirname(manifestPath));
  // The dispatch inputs the manifest must have been prepared for.
  if (args.version !== undefined && args.version !== manifest.version) {
    problems.push(`manifest version ${manifest.version} is not --version ${args.version}`);
  }
  if (args["source-sha"] !== undefined && args["source-sha"] !== manifest.source?.sha) {
    problems.push(
      `manifest source ${manifest.source?.sha} is not --source-sha ${args["source-sha"]}`,
    );
  }
  if (problems.length) throw new Stop(4, `manifest rejected:\n  ${problems.join("\n  ")}`);
  const existing = readExisting(args.existing);
  const byName = Object.fromEntries(manifest.packages.map((p) => [p.name, p]));
  const lines = [];
  for (const name of PUBLISH_ORDER) {
    const entry = byName[name];
    const published = existing[`${name}@${entry.version}`];
    const integrity = sha512Integrity(path.resolve(path.dirname(manifestPath), entry.tarball));
    if (published !== undefined && published !== integrity) {
      throw new Stop(
        4,
        `${name}@${entry.version} is already published with different contents (${published})`,
      );
    }
    // An identical published version is skipped, so a retried release does not republish it.
    lines.push(
      published === undefined
        ? `would publish ${name}@${entry.version} from ${entry.tarball} (sha256 ${entry.sha256}) to ${REGISTRY}`
        : `skip ${name}@${entry.version}: already published with identical integrity`,
    );
  }
  for (const line of lines) console.log(line);
  console.log(`plan only: source ${manifest.source.sha}; nothing was published`);
}

function main(argv) {
  const [command, ...rest] = argv;
  try {
    if (command === "prepare") prepare(rest);
    else if (command === "plan-publish") planPublish(rest);
    else throw new Stop(2, "usage: fork-release.mjs prepare|plan-publish ...");
    return 0;
  } catch (error) {
    console.error(`FAIL: ${error.message}`);
    return error instanceof Stop ? error.code : 3;
  }
}

process.exitCode = main(process.argv.slice(2));
