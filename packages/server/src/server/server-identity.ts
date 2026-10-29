import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { packageJsonSchema } from "./package-version.js";

// The server package ships under the original name and under the fork's owned name. Only these two identities are
// recognised; any other package.json name is not a Paseo server.
export const ORIGINAL_SERVER_PACKAGE = "@getpaseo/server";
export const OWNED_SERVER_PACKAGE = "@ivolnistov/paseo-server";
export const SERVER_PACKAGE_NAMES = [ORIGINAL_SERVER_PACKAGE, OWNED_SERVER_PACKAGE] as const;
export type ServerPackageName = (typeof SERVER_PACKAGE_NAMES)[number];

export function isServerPackageName(name: unknown): name is ServerPackageName {
  return typeof name === "string" && (SERVER_PACKAGE_NAMES as readonly string[]).includes(name);
}

export interface ServerPackage {
  root: string;
  name: ServerPackageName;
  version: string | undefined;
}

// Walks up from startPath to the nearest package.json whose name is a server identity. Matching is by the
// package.json name, so an npm alias directory (node_modules/@getpaseo/server holding @ivolnistov/paseo-server)
// resolves to its real identity. Unreadable package.json files are skipped.
export function findServerPackageFrom(startPath: string): ServerPackage | null {
  let currentDir = path.dirname(startPath);
  while (true) {
    const packageJsonPath = path.join(currentDir, "package.json");
    if (existsSync(packageJsonPath)) {
      try {
        const parsed = packageJsonSchema.safeParse(
          JSON.parse(readFileSync(packageJsonPath, "utf8")),
        );
        if (parsed.success && isServerPackageName(parsed.data.name)) {
          const version = parsed.data.version?.trim();
          return { root: currentDir, name: parsed.data.name, version: version || undefined };
        }
      } catch {
        // not JSON: keep walking
      }
    }
    const parentDir = path.dirname(currentDir);
    if (parentDir === currentDir) return null;
    currentDir = parentDir;
  }
}
