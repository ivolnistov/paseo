import path from "node:path";
import { fileURLToPath } from "node:url";
import { isRealpathInsideRoot } from "../../../utils/path.js";
import { findServerPackageFrom } from "../../server-identity.js";
import { npmGlobalPaseoCli, type NpmGlobalPaseoInstall } from "./npm-global-cli.js";

export interface DaemonInstallOriginRuntime {
  resolveCurrentServerPackageRoot(): string | null;
}

export const daemonInstallOriginRuntime: DaemonInstallOriginRuntime = {
  resolveCurrentServerPackageRoot,
};

export function resolveNpmGlobalPrefix(serverPackageRoot: string | null): string | undefined {
  if (!serverPackageRoot) return undefined;
  let directory = serverPackageRoot;
  let prefix: string | undefined;
  while (path.dirname(directory) !== directory) {
    if (path.basename(directory) === "node_modules") {
      const root = path.dirname(directory);
      if (process.platform === "win32") prefix = root;
      else if (path.basename(root) === "lib") prefix = path.dirname(root);
    }
    directory = path.dirname(directory);
  }
  return prefix;
}

// The CLI package this daemon's identity updates through, for messages only.
function cliPackageLabel(): string {
  try {
    return npmGlobalPaseoCli.target.packageName;
  } catch {
    return "Paseo CLI";
  }
}

export function validateDaemonInstallOrigin(
  install: NpmGlobalPaseoInstall,
  daemonVersion: string | null,
  runtime: DaemonInstallOriginRuntime = daemonInstallOriginRuntime,
): string | null {
  if (install.isLinked) {
    return `The global ${cliPackageLabel()} install is linked; self-update only supports normal npm global installs.`;
  }

  if (daemonVersion && install.version !== daemonVersion) {
    return `This daemon is not running from the npm global ${cliPackageLabel()} install (global npm has ${install.version}, daemon is ${daemonVersion}).`;
  }

  const currentServerPackageRoot = runtime.resolveCurrentServerPackageRoot();
  if (!currentServerPackageRoot) {
    return "Unable to verify that this daemon is running from an npm global install.";
  }

  if (!isCurrentServerUnderNpmInstall(currentServerPackageRoot, install)) {
    return `This daemon is not running from the npm global ${cliPackageLabel()} install.`;
  }

  return null;
}

function isCurrentServerUnderNpmInstall(
  currentServerPackageRoot: string,
  install: NpmGlobalPaseoInstall,
): boolean {
  const roots = install.globalRootPath
    ? [install.packagePath, globalNodeModulesPath(install.globalRootPath)]
    : [install.packagePath];

  return roots.some((root) => isRealpathInsideRoot(root, currentServerPackageRoot));
}

function globalNodeModulesPath(globalRootPath: string): string {
  const normalized = path.normalize(globalRootPath);
  return path.basename(normalized) === "node_modules"
    ? normalized
    : path.join(normalized, "node_modules");
}

function resolveCurrentServerPackageRoot(): string | null {
  return resolveServerPackageRootFrom(fileURLToPath(import.meta.url));
}

// Root of the nearest enclosing server package, original or owned name only (see server-identity.ts).
export function resolveServerPackageRootFrom(startPath: string): string | null {
  return findServerPackageFrom(startPath)?.root ?? null;
}
