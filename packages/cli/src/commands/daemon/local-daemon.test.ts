import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { resolveServerRunnerFromDir } from "./local-daemon.js";

const createdDirs: string[] = [];

function serverPackage(
  name: string,
  root = mkdtempSync(path.join(tmpdir(), "paseo-local-daemon-")),
): string {
  createdDirs.push(root);
  mkdirSync(path.join(root, "dist", "scripts"), { recursive: true });
  writeFileSync(path.join(root, "dist", "scripts", "supervisor-entrypoint.js"), "");
  writeFileSync(path.join(root, "package.json"), JSON.stringify({ name, version: "1.0.0" }));
  return root;
}

afterEach(() => {
  for (const dir of createdDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe("resolveServerRunnerFromDir", () => {
  it("runs the supervisor of the original and the owned server package", () => {
    for (const name of ["@getpaseo/server", "@ivolnistov/paseo-server"]) {
      const root = serverPackage(name);
      expect(resolveServerRunnerFromDir(root)).toBe(
        path.join(root, "dist", "scripts", "supervisor-entrypoint.js"),
      );
    }
  });

  it("runs an owned server installed under the original alias directory", () => {
    const base = mkdtempSync(path.join(tmpdir(), "paseo-local-daemon-"));
    createdDirs.push(base);
    const root = serverPackage(
      "@ivolnistov/paseo-server",
      path.join(base, "node_modules", "@getpaseo", "server"),
    );
    expect(resolveServerRunnerFromDir(root)).toBe(
      path.join(root, "dist", "scripts", "supervisor-entrypoint.js"),
    );
  });

  it("does not run a package under any other name", () => {
    expect(resolveServerRunnerFromDir(serverPackage("@someone-else/paseo-server"))).toBeNull();
    const empty = mkdtempSync(path.join(tmpdir(), "paseo-local-daemon-"));
    createdDirs.push(empty);
    expect(resolveServerRunnerFromDir(empty)).toBeNull();
  });
});
