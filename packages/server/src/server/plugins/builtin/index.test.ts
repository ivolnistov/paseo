import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, rm, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, expect, test } from "vitest";
import { WebSocket } from "ws";
import { compilePlugin } from "../compiler.js";
import { readPluginManifest } from "../manifest.js";
import type { WSOutboundMessage } from "../../messages.js";
import { DaemonClient } from "../../test-utils/daemon-client.js";
import { createTestPaseoDaemon } from "../../test-utils/paseo-daemon.js";
import { BuiltinPluginLoader, builtinPlugins, resolveBuiltinPluginsRoot } from "./index.js";

interface CachedClientBundle {
  id: string;
  clientBundleHash: string;
}

interface CatalogResponse {
  plugins: Array<{ id: string; clientBundle?: string; clientBundleHash?: string }>;
  bytes: number;
}

const roots: string[] = [];
afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

async function fixture(root: string, id: string, client = false): Promise<string> {
  const directory = path.join(root, id);
  await mkdir(directory, { recursive: true });
  await writeFile(
    path.join(directory, "paseo-plugin.json"),
    JSON.stringify({ id, requirements: { paseo: ">=0.9.2" } }),
  );
  await writeFile(
    path.join(directory, "index.server.ts"),
    "export default function contribute() { return () => {}; }",
  );
  if (client) {
    await writeFile(
      path.join(directory, "index.client.tsx"),
      "export default function contribute() { const marker = 'builtin-client-marker'; void marker; return () => {}; }",
    );
  }
  return directory;
}

test("listed built-ins resolve to matching manifests and compile", async () => {
  const root = resolveBuiltinPluginsRoot();
  for (const id of builtinPlugins) {
    const directory = path.join(root, id);
    expect((await readPluginManifest(directory)).id).toBe(id);
    const findEntry = async (names: string[]) => {
      for (const name of names) {
        const entry = path.join(directory, name);
        if ((await stat(entry).catch(() => null))?.isFile()) return entry;
      }
      return null;
    };
    const bundles = await compilePlugin({
      server: await findEntry(["index.server.ts", "index.server.tsx"]),
      client: await findEntry(["index.client.ts", "index.client.tsx"]),
    });
    expect(bundles.serverBundle).toBeTruthy();
  }
});

test("listed client bundle is published while plugins are disabled; unlisted directory is inert", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "paseo-builtin-fixture-"));
  roots.push(root);
  await fixture(root, "listed", true);
  await fixture(root, "unlisted", true);
  const daemon = await createTestPaseoDaemon({
    daemonVersion: "0.9.2",
    pluginsEnabled: false,
    builtinPlugins: new BuiltinPluginLoader(root, ["listed"]),
  });
  const client = new DaemonClient({ url: `ws://127.0.0.1:${daemon.port}/ws`, appVersion: "0.9.2" });
  try {
    await client.connect();
    expect(await client.listPlugins()).toEqual([]);
    const catalog = await client.getPluginCatalog();
    expect(catalog.map(({ id }) => id)).toEqual(["listed"]);
    expect(catalog[0]?.clientBundle).toContain("builtin-client-marker");
  } finally {
    await client.close();
    await daemon.close();
  }
}, 60_000);

// Speaks the WebSocket protocol directly: the request must name the bundles it holds,
// and the response size on the wire is what the cache saves.
async function connectCatalogPeer(port: number): Promise<{
  getCatalog: (cachedClientBundles?: CachedClientBundle[]) => Promise<CatalogResponse>;
  close: () => void;
}> {
  const socket = new WebSocket(`ws://127.0.0.1:${port}/ws`);
  const frames: WSOutboundMessage[] = [];
  socket.on("message", (data, binary) => {
    if (!binary) frames.push(JSON.parse(data.toString()));
  });
  try {
    await new Promise<void>((resolve, reject) => {
      socket.once("open", resolve);
      socket.once("error", reject);
    });
    socket.send(
      JSON.stringify({
        type: "hello",
        clientType: "browser",
        clientId: "clid_builtin_catalog_cache",
        protocolVersion: 1,
        appVersion: "0.9.2",
      }),
    );
    await expect
      .poll(() =>
        frames.some(
          (frame) =>
            frame.type === "session" &&
            frame.message.type === "status" &&
            frame.message.payload.status === "server_info",
        ),
      )
      .toBe(true);
  } catch (error) {
    socket.terminate();
    throw error;
  }
  let nextRequest = 0;
  return {
    async getCatalog(cachedClientBundles) {
      nextRequest += 1;
      const requestId = `catalog-${nextRequest}`;
      socket.send(
        JSON.stringify({
          type: "session",
          message: {
            type: "plugin.catalog.get.request",
            requestId,
            ...(cachedClientBundles ? { cachedClientBundles } : {}),
          },
        }),
      );
      let response: CatalogResponse | undefined;
      await expect
        .poll(() => {
          for (const frame of frames) {
            if (
              frame.type === "session" &&
              frame.message.type === "plugin.catalog.get.response" &&
              frame.message.payload.requestId === requestId
            ) {
              response = {
                plugins: frame.message.payload.plugins,
                bytes: JSON.stringify(frame.message).length,
              };
            }
          }
          return response !== undefined;
        })
        .toBe(true);
      return response!;
    },
    close: () => socket.close(),
  };
}

test("a repeated catalog fetch leaves out the client bundle the client already holds", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "paseo-builtin-catalog-cache-"));
  roots.push(root);
  const directory = await fixture(root, "large", true);
  // About the size of a real plugin client bundle.
  await writeFile(
    path.join(directory, "index.client.tsx"),
    `const payload = ${JSON.stringify("x".repeat(1_000_000))};
export default function contribute() { void payload; return () => {}; }`,
  );
  const daemon = await createTestPaseoDaemon({
    daemonVersion: "0.9.2",
    pluginsEnabled: false,
    builtinPlugins: new BuiltinPluginLoader(root, ["large"]),
  });
  let peer: Awaited<ReturnType<typeof connectCatalogPeer>> | undefined;
  try {
    peer = await connectCatalogPeer(daemon.port);
    // A client without a cache sends no cachedClientBundles and gets every bundle.
    const first = await peer.getCatalog();
    const clientBundle = first.plugins[0]?.clientBundle;
    expect(clientBundle).toContain("x".repeat(1_000_000));
    const clientBundleHash = createHash("sha256").update(clientBundle!).digest("hex");
    expect(first.plugins).toEqual([expect.objectContaining({ id: "large", clientBundleHash })]);
    expect(first.bytes).toBeGreaterThan(1_000_000);

    const hit = await peer.getCatalog([{ id: "large", clientBundleHash }]);
    expect(hit.plugins).toHaveLength(1);
    expect(hit.plugins[0]).toMatchObject({ id: "large", clientBundleHash });
    expect(hit.plugins[0]).not.toHaveProperty("clientBundle");
    expect(hit.bytes).toBeLessThan(500);

    const staleHash = await peer.getCatalog([{ id: "large", clientBundleHash: "0".repeat(64) }]);
    const otherPlugin = await peer.getCatalog([{ id: "other", clientBundleHash }]);
    for (const miss of [staleHash, otherPlugin]) {
      expect(miss.plugins).toEqual(first.plugins);
      expect(miss.bytes).toBeGreaterThan(1_000_000);
    }
  } finally {
    peer?.close();
    await daemon.close();
  }
}, 60_000);

test("directory, Git, and npm installs reject a built-in ID", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "paseo-builtin-install-"));
  roots.push(root);
  const directory = await fixture(root, "reserved");
  const gitDirectory = await fixture(root, "git-source");
  await writeFile(
    path.join(gitDirectory, "paseo-plugin.json"),
    JSON.stringify({ id: "reserved", requirements: { paseo: ">=0.9.2" } }),
  );
  execFileSync("git", ["init", "-q", gitDirectory]);
  execFileSync("git", [
    "-C",
    gitDirectory,
    "-c",
    "user.name=Paseo Tests",
    "-c",
    "user.email=paseo@example.test",
    "add",
    ".",
  ]);
  execFileSync("git", [
    "-C",
    gitDirectory,
    "-c",
    "user.name=Paseo Tests",
    "-c",
    "user.email=paseo@example.test",
    "commit",
    "-qm",
    "fixture",
  ]);
  const daemon = await createTestPaseoDaemon({
    daemonVersion: "0.9.2",
    pluginsEnabled: false,
    builtinPlugins: new BuiltinPluginLoader(root, ["reserved"]),
  });
  const client = new DaemonClient({ url: `ws://127.0.0.1:${daemon.port}/ws`, appVersion: "0.9.2" });
  try {
    await client.connect();
    await expect(client.installDirectoryPlugin(directory)).rejects.toThrow(
      /reserved for a built-in/,
    );
    await expect(
      client.installPluginSource({ source: pathToFileURL(gitDirectory).href }),
    ).rejects.toThrow(/reserved for a built-in/);
    await expect(
      client.installPluginSource({ source: "npm:unused-fixture", id: "reserved" }),
    ).rejects.toThrow(/reserved for a built-in/);
    expect(await client.listPlugins()).toEqual([]);
  } finally {
    await client.close();
    await daemon.close();
  }
}, 60_000);
