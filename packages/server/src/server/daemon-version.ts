import { fileURLToPath } from "node:url";
import { PackageVersionResolutionError } from "./package-version.js";
import { findServerPackageFrom, SERVER_PACKAGE_NAMES } from "./server-identity.js";

const SERVER_PACKAGE_LABEL = SERVER_PACKAGE_NAMES.join(" or ");

export class DaemonVersionResolutionError extends PackageVersionResolutionError {}

export function resolveDaemonVersion(moduleUrl: string = import.meta.url): string {
  const serverPackage = findServerPackageFrom(fileURLToPath(moduleUrl));
  if (!serverPackage?.version) {
    throw new DaemonVersionResolutionError({ moduleUrl, packageName: SERVER_PACKAGE_LABEL });
  }
  return serverPackage.version;
}
