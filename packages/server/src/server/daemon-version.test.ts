import { afterEach, describe, expect, it } from "vitest";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { DaemonVersionResolutionError, resolveDaemonVersion } from "./daemon-version.js";
import { resolveServerPackageRootFrom } from "./session/daemon/install-origin.js";

const createdDirs: string[] = [];

function createTempDir(): string {
  const dir = mkdtempSync(path.join(tmpdir(), "paseo-daemon-version-"));
  createdDirs.push(dir);
  return dir;
}

afterEach(() => {
  for (const dir of createdDirs.splice(0, createdDirs.length)) {
    rmSync(dir, { recursive: true, force: true });
  }
});

describe("resolveDaemonVersion", () => {
  it("resolves server version by walking up to @getpaseo/server package.json", () => {
    const root = createTempDir();
    writeFileSync(
      path.join(root, "package.json"),
      JSON.stringify({ name: "@getpaseo/server", version: "9.8.7" }),
      "utf8",
    );
    const nestedDir = path.join(root, "dist", "server");
    mkdirSync(nestedDir, { recursive: true });

    const moduleUrl = pathToFileURL(path.join(nestedDir, "index.js")).href;
    expect(resolveDaemonVersion(moduleUrl)).toBe("9.8.7");
  });

  // npm aliases keep the install directory name but not metadata.name, so a fork install of the server
  // reports its own package name. Only the original and the one owned name are server identities.
  it("resolves the version of the owned fork server package", () => {
    const root = createTempDir();
    writeFileSync(
      path.join(root, "package.json"),
      JSON.stringify({ name: "@ivolnistov/paseo-server", version: "0.13.0-fork.1" }),
      "utf8",
    );
    const nestedDir = path.join(root, "dist", "server");
    mkdirSync(nestedDir, { recursive: true });

    const moduleUrl = pathToFileURL(path.join(nestedDir, "index.js")).href;
    expect(resolveDaemonVersion(moduleUrl)).toBe("0.13.0-fork.1");
  });

  it("rejects a server package under any other name", () => {
    const root = createTempDir();
    writeFileSync(
      path.join(root, "package.json"),
      JSON.stringify({ name: "@someone-else/paseo-server", version: "1.2.3" }),
      "utf8",
    );
    const nestedDir = path.join(root, "dist", "server");
    mkdirSync(nestedDir, { recursive: true });

    const moduleUrl = pathToFileURL(path.join(nestedDir, "index.js")).href;
    expect(() => resolveDaemonVersion(moduleUrl)).toThrow(DaemonVersionResolutionError);
  });

  it("throws when @getpaseo/server package metadata cannot be resolved", () => {
    const root = createTempDir();
    writeFileSync(
      path.join(root, "package.json"),
      JSON.stringify({ name: "not-getpaseo-server", version: "1.2.3" }),
      "utf8",
    );
    const nestedDir = path.join(root, "dist", "server");
    mkdirSync(nestedDir, { recursive: true });

    const moduleUrl = pathToFileURL(path.join(nestedDir, "index.js")).href;
    expect(() => resolveDaemonVersion(moduleUrl)).toThrow(DaemonVersionResolutionError);
  });

  it("throws when @getpaseo/server version is missing", () => {
    const root = createTempDir();
    writeFileSync(
      path.join(root, "package.json"),
      JSON.stringify({ name: "@getpaseo/server" }),
      "utf8",
    );
    const nestedDir = path.join(root, "dist", "server");
    mkdirSync(nestedDir, { recursive: true });

    const moduleUrl = pathToFileURL(path.join(nestedDir, "index.js")).href;
    expect(() => resolveDaemonVersion(moduleUrl)).toThrow(DaemonVersionResolutionError);
  });
});

describe("resolveServerPackageRootFrom", () => {
  function serverAt(root: string, name: string): string {
    mkdirSync(path.join(root, "dist", "server"), { recursive: true });
    writeFileSync(
      path.join(root, "package.json"),
      JSON.stringify({ name, version: "1.0.0" }),
      "utf8",
    );
    return path.join(root, "dist", "server", "index.js");
  }

  it("finds the original and the owned server package roots", () => {
    for (const name of ["@getpaseo/server", "@ivolnistov/paseo-server"]) {
      const root = createTempDir();
      expect(resolveServerPackageRootFrom(serverAt(root, name))).toBe(root);
    }
  });

  it("finds an owned package installed under the original alias directory", () => {
    // npm alias: node_modules/@getpaseo/server holds @ivolnistov/paseo-server; metadata.name keeps the owned name.
    const root = path.join(createTempDir(), "node_modules", "@getpaseo", "server");
    expect(resolveServerPackageRootFrom(serverAt(root, "@ivolnistov/paseo-server"))).toBe(root);
  });

  it("does not take a package under any other name for the server", () => {
    const root = createTempDir();
    expect(resolveServerPackageRootFrom(serverAt(root, "@someone-else/paseo-server"))).toBeNull();
  });
});
