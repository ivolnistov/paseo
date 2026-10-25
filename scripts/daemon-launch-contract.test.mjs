import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";

const repoRoot = join(import.meta.dirname, "..");

function assertNoDirectWorkerLaunch(label, command) {
  for (const workerEntrypoint of [
    "src/server/index.ts",
    "dist/server/server/index.js",
    "src/server/daemon-worker.ts",
    "dist/server/server/daemon-worker.js",
  ]) {
    assert.ok(
      !command.includes(workerEntrypoint),
      `${label} must not launch ${workerEntrypoint} directly: ${command}`,
    );
  }
}

function assertNoSpawnedWorkerEntrypoint(label, source) {
  assertNoDirectWorkerLaunch(label, source);
  assert.doesNotMatch(
    source,
    /spawn\([^)]*["'`][^"'`]*\.\.\/index\.ts["'`]/s,
    `${label} must not spawn ../index.ts directly`,
  );
}

test("every executable daemon entrypoint enters the supervisor", async () => {
  const [
    serverPackageSource,
    appIsolatedHostDaemon,
    serverConnectionOfferE2e,
    desktopRuntimePaths,
    nixPackage,
    nixModule,
  ] = await Promise.all([
    readFile(join(repoRoot, "packages/server/package.json"), "utf8"),
    readFile(join(repoRoot, "packages/app/e2e/support/helpers/isolated-host-daemon.ts"), "utf8"),
    readFile(
      join(repoRoot, "packages/server/src/server/daemon-e2e/connection-offer.e2e.test.ts"),
      "utf8",
    ),
    readFile(join(repoRoot, "packages/desktop/src/daemon/runtime-paths.ts"), "utf8"),
    readFile(join(repoRoot, "nix/package.nix"), "utf8"),
    readFile(join(repoRoot, "nix/module.nix"), "utf8"),
  ]);

  const serverPackage = JSON.parse(serverPackageSource);
  const startScript = serverPackage.scripts?.start ?? "";
  const devScript = serverPackage.scripts?.dev ?? "";
  const devTsxScript = serverPackage.scripts?.["dev:tsx"] ?? "";

  assert.match(startScript, /dist\/scripts\/supervisor-entrypoint\.js/);
  assertNoDirectWorkerLaunch("server start script", startScript);
  assert.match(devScript, /scripts\/dev-runner\.ts/);
  assertNoDirectWorkerLaunch("server dev script", devScript);
  assert.match(devTsxScript, /scripts\/dev-runner\.ts/);
  assertNoDirectWorkerLaunch("server dev:tsx script", devTsxScript);

  assert.match(
    appIsolatedHostDaemon,
    /spawnTsx\("scripts\/supervisor-entrypoint\.ts", \["--dev"\]/,
  );
  assertNoSpawnedWorkerEntrypoint("app e2e isolated host daemon", appIsolatedHostDaemon);

  assert.match(serverConnectionOfferE2e, /scripts\/supervisor-entrypoint\.ts/);
  assertNoSpawnedWorkerEntrypoint("server daemon e2e process launch", serverConnectionOfferE2e);

  assert.match(desktopRuntimePaths, /"dist", "scripts", "supervisor-entrypoint\.js"/);
  assert.match(desktopRuntimePaths, /"scripts", "supervisor-entrypoint\.ts"/);
  assertNoDirectWorkerLaunch("desktop runtime paths", desktopRuntimePaths);

  assert.match(nixPackage, /dist\/scripts\/supervisor-entrypoint\.js/);
  assertNoDirectWorkerLaunch("Nix package wrapper", nixPackage);
  assert.match(nixPackage, /--set PASEO_NODE_ENV production/);
  assert.doesNotMatch(nixPackage, /--set(-default)?\s+NODE_ENV\b/);
  assert.doesNotMatch(nixModule, /\bNODE_ENV\b\s*=/);
  assert.doesNotMatch(nixModule, /\bPASEO_NODE_ENV\b/);
});

// ---- Fork package release (fork-integration-20261024 N1 packer). scripts/fork-release.mjs:
//   prepare --src DIR --version EXACT --out DIR   -> exactly the 7 owned tarballs + manifest.json; never publishes
//   plan-publish --manifest FILE --registry URL    -> validates; never runs npm publish or writes to a registry
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";

const forkRelease = join(repoRoot, "scripts", "fork-release.mjs");
const realNpm = execFileSync("sh", ["-c", "command -v npm"]).toString().trim();
const FORK_VERSION = "0.13.0-fork.1";
const GITHUB_PACKAGES = "https://npm.pkg.github.com";
const owned = {
  cli: ["@getpaseo/cli", "@ivolnistov/paseo"],
  server: ["@getpaseo/server", "@ivolnistov/paseo-server"],
  client: ["@getpaseo/client", "@ivolnistov/paseo-client"],
  protocol: ["@getpaseo/protocol", "@ivolnistov/paseo-protocol"],
  highlight: ["@getpaseo/highlight", "@ivolnistov/paseo-highlight"],
  plugin: ["@getpaseo/plugin", "@ivolnistov/paseo-plugin"],
  relay: ["@getpaseo/relay", "@ivolnistov/paseo-relay"],
};
const targetOf = Object.fromEntries(Object.values(owned));
// The internal edges the real workspaces have, in each section.
const edges = {
  cli: { dependencies: ["@getpaseo/server", "@getpaseo/client", "@getpaseo/protocol"] },
  server: {
    dependencies: [
      "@getpaseo/protocol",
      "@getpaseo/client",
      "@getpaseo/highlight",
      "@getpaseo/relay",
      "@getpaseo/plugin",
    ],
  },
  client: { dependencies: ["@getpaseo/protocol", "@getpaseo/relay"] },
  protocol: {},
  highlight: {},
  plugin: { peerDependencies: ["@getpaseo/client", "@getpaseo/protocol"] },
  relay: {},
};

function forkFixture({ webUi = true } = {}) {
  const root = mkdtempSync(join(tmpdir(), "paseo-fork-release-"));
  const src = join(root, "src");
  for (const [dir, [name]] of Object.entries(owned)) {
    const pkg = {
      name,
      version: "0.13.0",
      license: "AGPL-3.0",
      main: "dist/index.js",
      files: ["dist"],
    };
    for (const [section, deps] of Object.entries(edges[dir])) {
      pkg[section] = Object.fromEntries(deps.map((dep) => [dep, "*"]));
    }
    mkdirSync(join(src, "packages", dir, "dist"), { recursive: true });
    writeFileSync(join(src, "packages", dir, "package.json"), JSON.stringify(pkg, null, 2));
    writeFileSync(
      join(src, "packages", dir, "dist", "index.js"),
      `export const name = ${JSON.stringify(name)};\n`,
    );
  }
  // The server serves the daemon web UI built by scripts/build-daemon-web-ui.mjs into dist/server/web-ui.
  if (webUi) {
    mkdirSync(join(src, "packages", "server", "dist", "server", "web-ui"), { recursive: true });
    writeFileSync(
      join(src, "packages", "server", "dist", "server", "web-ui", "index.html"),
      "<!doctype html>\n",
    );
  }
  writeFileSync(
    join(src, "package.json"),
    JSON.stringify({ name: "paseo", private: true, workspaces: ["packages/*"] }),
  );
  const git = (...args) =>
    execFileSync("git", ["-C", src, "-c", "user.name=t", "-c", "user.email=t@t", ...args]);
  git("init", "-q");
  git("add", "-A");
  git("commit", "-qm", "fixture");
  const sha = git("rev-parse", "HEAD").toString().trim();
  // A recording npm first on PATH: plan-publish must never reach `npm publish`.
  const bin = join(root, "bin");
  mkdirSync(bin);
  const calls = join(root, "npm-calls.log");
  writeFileSync(
    join(bin, "npm"),
    `#!/bin/sh\necho "npm $*" >> "${calls}"\n[ "$1" = publish ] && exit 0\nexec "${realNpm}" "$@"\n`,
  );
  chmodSync(join(bin, "npm"), 0o755);
  return {
    root,
    src,
    sha,
    out: join(root, "out"),
    calls,
    env: { ...process.env, PATH: `${bin}:${process.env.PATH}` },
  };
}

function run(fx, ...args) {
  return spawnSync(process.execPath, [forkRelease, ...args], {
    cwd: fx.root,
    env: fx.env,
    encoding: "utf8",
  });
}

function prepared() {
  const fx = forkFixture();
  const result = run(fx, "prepare", "--src", fx.src, "--version", FORK_VERSION, "--out", fx.out);
  assert.equal(result.status, 0, `prepare failed: ${result.stderr}${result.stdout}`);
  const manifestPath = join(fx.out, "manifest.json");
  return { ...fx, manifestPath, manifest: JSON.parse(readFileSync(manifestPath, "utf8")) };
}

function packedManifest(tarball) {
  return JSON.parse(execFileSync("tar", ["-xzOf", tarball, "package/package.json"]).toString());
}

function refuses(fx, manifest, registry = GITHUB_PACKAGES) {
  const path = join(fx.root, `manifest-${Math.random().toString(16).slice(2)}.json`);
  writeFileSync(path, JSON.stringify(manifest));
  const result = run(fx, "plan-publish", "--manifest", path, "--registry", registry);
  assert.notEqual(result.status, 0, `plan-publish accepted: ${result.stdout}`);
  assert.ok(
    !existsSync(fx.calls) || !readFileSync(fx.calls, "utf8").includes("npm publish"),
    "never publishes",
  );
}

test("prepare packs exactly the seven owned packages with exact aliased internal edges", () => {
  const fx = prepared();
  try {
    const { manifest } = fx;
    assert.equal(manifest.version, FORK_VERSION);
    assert.equal(manifest.source.sha, fx.sha);
    assert.deepEqual(manifest.packages.map((p) => p.name).sort(), Object.values(targetOf).sort());
    for (const entry of manifest.packages) {
      assert.equal(
        targetOf[entry.sourceName],
        entry.name,
        "source and target identities are recorded separately",
      );
      assert.equal(entry.version, FORK_VERSION);
      const tarball = join(fx.out, entry.tarball);
      assert.equal(createHash("sha256").update(readFileSync(tarball)).digest("hex"), entry.sha256);
      const pkg = packedManifest(tarball);
      assert.equal(pkg.name, entry.name);
      assert.equal(pkg.version, FORK_VERSION);
      assert.equal(pkg.publishConfig?.registry, GITHUB_PACKAGES);
      const internalEdges = ["dependencies", "optionalDependencies"].flatMap((section) =>
        Object.entries(pkg[section] ?? {})
          .filter(([key]) => key.startsWith("@getpaseo/"))
          .map(([key, spec]) => ({ section, key, spec })),
      );
      for (const { section, key, spec } of internalEdges) {
        assert.equal(
          spec,
          `npm:${targetOf[key]}@${FORK_VERSION}`,
          `${entry.name} ${section} ${key}`,
        );
      }
    }
    // Plugin peers are recorded for an actual npm resolver proof (next slice); they are never left as a bare
    // official range that an official package could satisfy.
    const plugin = manifest.packages.find((p) => p.sourceName === "@getpaseo/plugin");
    const peers = packedManifest(join(fx.out, plugin.tarball)).peerDependencies ?? {};
    for (const key of ["@getpaseo/client", "@getpaseo/protocol"]) {
      assert.ok(key in peers || `${targetOf[key]}` in peers, `plugin keeps its ${key} peer edge`);
      assert.notEqual(peers[key], "*", "the peer range is not left open to the official package");
    }
  } finally {
    rmSync(fx.root, { recursive: true, force: true });
  }
});

test("plan-publish accepts the prepared closure for GitHub Packages without publishing", () => {
  const fx = prepared();
  try {
    const result = run(
      fx,
      "plan-publish",
      "--manifest",
      fx.manifestPath,
      "--registry",
      GITHUB_PACKAGES,
    );
    assert.equal(result.status, 0, result.stderr);
    for (const name of Object.values(targetOf))
      assert.match(result.stdout, new RegExp(name.replace("/", "\\/")));
    assert.ok(!existsSync(fx.calls) || !readFileSync(fx.calls, "utf8").includes("npm publish"));
  } finally {
    rmSync(fx.root, { recursive: true, force: true });
  }
});

test("plan-publish refuses a wrong registry, official names, gaps, aliases, versions and digests", () => {
  const fx = prepared();
  try {
    const clone = () => JSON.parse(JSON.stringify(fx.manifest));
    refuses(fx, fx.manifest, "https://registry.npmjs.org");
    refuses(fx, fx.manifest, "https://npm.pkg.github.com.evil.example");
    const official = clone();
    official.packages[0].name = official.packages[0].sourceName;
    refuses(fx, official);
    const missing = clone();
    missing.packages.pop();
    refuses(fx, missing);
    const unaliased = clone();
    const server = unaliased.packages.find((p) => p.sourceName === "@getpaseo/server");
    server.dependencies["@getpaseo/protocol"] = FORK_VERSION;
    refuses(fx, unaliased);
    const version = clone();
    version.packages[1].version = "0.13.0-fork.2";
    refuses(fx, version);
    const digest = clone();
    digest.packages[2].sha256 = "0".repeat(64);
    refuses(fx, digest);
  } finally {
    rmSync(fx.root, { recursive: true, force: true });
  }
});

// ---- Clean-consumer resolver proof (fixture scope). Real npm installs the produced fixture tarballs from a
// loopback registry that serves only the 7 owned packages. This proves npm resolves every aliased internal edge,
// including the plugin's aliased peers, to an owned package with no official fallback. It is not a proof of the
// real built packages, their native install scripts or GitHub Packages.
import { spawn } from "node:child_process";
import { createServer } from "node:http";

test("a clean consumer resolves every internal edge, including plugin peers, to the owned packages", async () => {
  const fx = prepared();
  const requests = [];
  const packuments = new Map();
  const tarballs = new Map();
  const server = createServer((req, res) => {
    const path = decodeURIComponent(req.url.split("?")[0]);
    requests.push(path);
    if (tarballs.has(path)) {
      res.writeHead(200, { "content-type": "application/octet-stream" });
      res.end(tarballs.get(path));
      return;
    }
    const doc = packuments.get(path.replace(/^\//, ""));
    if (!doc) {
      res.writeHead(404, { "content-type": "application/json" });
      res.end('{"error":"not found"}');
      return;
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(doc));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    for (const entry of fx.manifest.packages) {
      const bytes = readFileSync(join(fx.out, entry.tarball));
      const pkg = packedManifest(join(fx.out, entry.tarball));
      const tarPath = `/tarballs/${entry.tarball}`;
      tarballs.set(tarPath, bytes);
      const integrity = `sha512-${createHash("sha512").update(bytes).digest("base64")}`;
      packuments.set(entry.name, {
        name: entry.name,
        "dist-tags": { latest: entry.version },
        versions: {
          [entry.version]: { ...pkg, dist: { tarball: `${base}${tarPath}`, integrity } },
        },
      });
    }
    const consumer = join(fx.root, "consumer");
    const home = join(fx.root, "consumer-home");
    mkdirSync(consumer, { recursive: true });
    mkdirSync(home, { recursive: true });
    writeFileSync(
      join(consumer, "package.json"),
      JSON.stringify({ name: "consumer", private: true }),
    );
    const env = {
      PATH: process.env.PATH,
      HOME: home,
      npm_config_userconfig: join(home, ".npmrc"),
      npm_config_globalconfig: join(home, "global-npmrc"),
      npm_config_cache: join(home, "npm-cache"),
      npm_config_registry: `${base}/`,
      "npm_config_@ivolnistov:registry": `${base}/`,
      npm_config_audit: "false",
      npm_config_fund: "false",
      npm_config_update_notifier: "false",
    };
    const result = await new Promise((resolve) => {
      const child = spawn(
        realNpm,
        ["install", `@ivolnistov/paseo@${FORK_VERSION}`, "--no-package-lock"],
        { cwd: consumer, env },
      );
      let output = "";
      child.stdout.on("data", (d) => (output += d));
      child.stderr.on("data", (d) => (output += d));
      child.on("close", (code) => resolve({ code, output }));
    });
    assert.equal(result.code, 0, result.output);

    const metadataRequests = requests.filter((p) => !p.startsWith("/tarballs/"));
    assert.ok(metadataRequests.length > 0);
    for (const path of metadataRequests) {
      assert.ok(
        !path.includes("@getpaseo"),
        `npm never asked the registry for an official package: ${path}`,
      );
    }
    assert.deepEqual(
      [...new Set(metadataRequests.map((p) => p.replace(/^\//, "")))].sort(),
      Object.values(targetOf).sort(),
      "exactly the 7 owned packages were resolved",
    );
    // Installed tree: the CLI at its own name; every internal dependency (and plugin peer) under its @getpaseo key,
    // holding the owned package at the exact version.
    const installed = (dir) =>
      JSON.parse(readFileSync(join(consumer, "node_modules", dir, "package.json"), "utf8"));
    assert.equal(installed("@ivolnistov/paseo").version, FORK_VERSION);
    for (const [source, target] of Object.entries(targetOf)) {
      if (source === "@getpaseo/cli") continue;
      const pkg = installed(source);
      assert.equal(pkg.name, target, `node_modules/${source} holds ${target}`);
      assert.equal(pkg.version, FORK_VERSION);
    }
    writeFileSync(
      join(fx.root, "resolver-receipt.json"),
      JSON.stringify({ requests, base }, null, 2),
    );
    console.log(`resolver receipt: ${JSON.stringify([...new Set(requests)])}`);
  } finally {
    server.close();
    rmSync(fx.root, { recursive: true, force: true });
  }
});

// ---- N8: artifact completeness, dispatch bindings and existing-version idempotence (prepare/plan-publish).
test("prepare refuses a server package without the daemon web UI build", () => {
  // A clean committed checkout whose server simply lacks the web UI output, so only completeness can refuse it.
  const fx = forkFixture({ webUi: false });
  try {
    const result = run(fx, "prepare", "--src", fx.src, "--version", FORK_VERSION, "--out", fx.out);
    assert.equal(
      result.status,
      3,
      `prepare accepted a server without its web UI: ${result.stdout}${result.stderr}`,
    );
    assert.ok(
      !existsSync(join(fx.out, "manifest.json")),
      "no manifest for an incomplete artifact set",
    );
  } finally {
    rmSync(fx.root, { recursive: true, force: true });
  }
});

test("the packed server carries the daemon web UI", () => {
  const fx = prepared();
  try {
    const server = fx.manifest.packages.find((p) => p.sourceName === "@getpaseo/server");
    const entries = execFileSync("tar", ["-tzf", join(fx.out, server.tarball)])
      .toString()
      .split("\n");
    assert.ok(entries.includes("package/dist/server/web-ui/index.html"), entries.join(" "));
  } finally {
    rmSync(fx.root, { recursive: true, force: true });
  }
});

function plan(fx, ...extra) {
  return run(
    fx,
    "plan-publish",
    "--manifest",
    fx.manifestPath,
    "--registry",
    GITHUB_PACKAGES,
    ...extra,
  );
}

test("plan-publish binds the dispatch version and source commit", () => {
  const fx = prepared();
  try {
    const ok = plan(fx, "--version", FORK_VERSION, "--source-sha", fx.sha);
    assert.equal(ok.status, 0, ok.stderr);
    assert.notEqual(
      plan(fx, "--version", "0.13.0-fork.2", "--source-sha", fx.sha).status,
      0,
      "version must match the dispatch input",
    );
    assert.notEqual(
      plan(fx, "--version", FORK_VERSION, "--source-sha", "0".repeat(40)).status,
      0,
      "source must match the dispatch SHA",
    );
  } finally {
    rmSync(fx.root, { recursive: true, force: true });
  }
});

test("plan-publish skips an identical existing version and refuses a differing one", () => {
  const fx = prepared();
  try {
    const relay = fx.manifest.packages.find((p) => p.name === "@ivolnistov/paseo-relay");
    const integrity = `sha512-${createHash("sha512")
      .update(readFileSync(join(fx.out, relay.tarball)))
      .digest("base64")}`;
    const existing = (value) => {
      const path = join(fx.root, `existing-${Math.random().toString(16).slice(2)}.json`);
      // name@version -> dist.integrity as the registry reports it (npm view <name>@<version> dist.integrity)
      writeFileSync(path, JSON.stringify({ [`${relay.name}@${relay.version}`]: value }));
      return path;
    };
    const same = plan(
      fx,
      "--version",
      FORK_VERSION,
      "--source-sha",
      fx.sha,
      "--existing",
      existing(integrity),
    );
    assert.equal(same.status, 0, same.stderr);
    const relayLine = same.stdout
      .split("\n")
      .find((line) => line.includes("@ivolnistov/paseo-relay"));
    assert.match(
      relayLine ?? "",
      /skip/i,
      "an identical published version is skipped, not republished",
    );
    const other = same.stdout
      .split("\n")
      .find((line) => line.includes("@ivolnistov/paseo-protocol"));
    assert.doesNotMatch(other ?? "", /skip/i, "versions not yet published are still planned");
    const differs = plan(
      fx,
      "--version",
      FORK_VERSION,
      "--source-sha",
      fx.sha,
      "--existing",
      existing("sha512-AAAA"),
    );
    assert.equal(differs.status, 4, "a different existing tarball for the same version is refused");
    assert.ok(!existsSync(fx.calls) || !readFileSync(fx.calls, "utf8").includes("npm publish"));
  } finally {
    rmSync(fx.root, { recursive: true, force: true });
  }
});
