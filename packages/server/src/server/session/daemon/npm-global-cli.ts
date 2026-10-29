import { lstatSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { getErrorMessage } from "@getpaseo/protocol/error-utils";
import { z } from "zod";
import { execCommand } from "../../../utils/spawn.js";
import {
  findServerPackageFrom,
  ORIGINAL_SERVER_PACKAGE,
  OWNED_SERVER_PACKAGE,
} from "../../server-identity.js";

export const PASEO_CLI_PACKAGE = "@getpaseo/cli";

// The CLI package (and registry) a server identity updates through. The owned fork always uses its own CLI from the
// approved registry; it never falls back to the official package.
export interface PaseoCliTarget {
  packageName: string;
  registry: string | null;
}

const ORIGINAL_CLI_TARGET: PaseoCliTarget = { packageName: PASEO_CLI_PACKAGE, registry: null };
const OWNED_CLI_TARGET: PaseoCliTarget = {
  packageName: "@ivolnistov/paseo",
  registry: "https://npm.pkg.github.com",
};

export function resolvePaseoCliTarget(serverPackageName: string): PaseoCliTarget {
  if (serverPackageName === ORIGINAL_SERVER_PACKAGE) return ORIGINAL_CLI_TARGET;
  if (serverPackageName === OWNED_SERVER_PACKAGE) return OWNED_CLI_TARGET;
  throw new Error(`No Paseo CLI update target for server package ${serverPackageName}`);
}

const NPM_PROBE_TIMEOUT_MS = 10_000;
const NPM_INSTALL_TIMEOUT_MS = 300_000;
const NPM_MAX_BUFFER_BYTES = 10 * 1024 * 1024;

const NpmGlobalListSchema = z
  .object({
    path: z.string().optional(),
    dependencies: z.record(z.string(), z.unknown()).optional(),
  })
  .passthrough();

const NpmGlobalCliPackageSchema = z
  .object({
    version: z.string(),
    path: z.string(),
  })
  .passthrough();

const CommandErrorSchema = z
  .object({
    code: z.union([z.number(), z.string()]).optional(),
    stdout: z.string().optional(),
    stderr: z.string().optional(),
  })
  .passthrough();

export interface CommandOptions {
  timeout?: number;
  maxBuffer?: number;
}

export interface CommandResult {
  exitCode: number;
  stdout: string;
  stderr: string;
}

export interface NpmGlobalPaseoInstall {
  version: string;
  packagePath: string;
  globalRootPath: string | null;
  isLinked: boolean;
}

export interface NpmGlobalPaseoCli {
  inspect(options?: NpmGlobalOptions): Promise<NpmGlobalPaseoInstall>;
  installLatest(options?: NpmGlobalOptions): Promise<CommandResult>;
}

interface NpmGlobalOptions {
  prefix?: string;
}

export type CommandRunner = (
  command: string,
  args: string[],
  options?: CommandOptions,
) => Promise<CommandResult>;

async function runExternalCommand(
  command: string,
  args: string[],
  options?: CommandOptions,
): Promise<CommandResult> {
  try {
    const { stdout, stderr } = await execCommand(command, args, {
      timeout: options?.timeout,
      maxBuffer: options?.maxBuffer,
    });
    return { exitCode: 0, stdout, stderr };
  } catch (error) {
    const parsed = CommandErrorSchema.safeParse(error);
    if (!parsed.success) {
      return { exitCode: 1, stdout: "", stderr: getErrorMessage(error) };
    }

    return {
      exitCode: typeof parsed.data.code === "number" ? parsed.data.code : 1,
      stdout: parsed.data.stdout ?? "",
      stderr: parsed.data.stderr || getErrorMessage(error),
    };
  }
}

function parseNpmGlobalPaseoInstall(
  stdout: string,
  cliPackage: string,
): NpmGlobalPaseoInstall | null {
  let parsedJson: unknown;
  try {
    parsedJson = JSON.parse(stdout);
  } catch {
    return null;
  }

  const list = NpmGlobalListSchema.safeParse(parsedJson);
  if (!list.success) {
    return null;
  }

  const rawCliPackage = list.data.dependencies?.[cliPackage];
  const parsedCli = NpmGlobalCliPackageSchema.safeParse(rawCliPackage);
  if (!parsedCli.success) {
    return null;
  }

  return {
    version: parsedCli.data.version,
    packagePath: parsedCli.data.path,
    globalRootPath: list.data.path ?? null,
    // npm links an install by making its node_modules entry a symlink (a junction
    // on Windows), and `npm ls --json` reports no flag for it.
    isLinked: lstatSync(parsedCli.data.path).isSymbolicLink(),
  };
}

export class DefaultNpmGlobalPaseoCli implements NpmGlobalPaseoCli {
  constructor(
    private readonly runCommand: CommandRunner = runExternalCommand,
    readonly target: PaseoCliTarget = ORIGINAL_CLI_TARGET,
  ) {}

  async inspect(options: NpmGlobalOptions = {}): Promise<NpmGlobalPaseoInstall> {
    const prefixArgs = options.prefix ? ["--prefix", options.prefix] : [];
    const result = await this.runCommand(
      "npm",
      ["-g", "ls", this.target.packageName, "--json", "--depth=0", "--long", ...prefixArgs],
      {
        timeout: NPM_PROBE_TIMEOUT_MS,
        maxBuffer: NPM_MAX_BUFFER_BYTES,
      },
    );

    if (result.exitCode !== 0 && result.stdout.trim().length === 0) {
      throw new Error(result.stderr.trim() || "npm is not available on this host");
    }

    const install = parseNpmGlobalPaseoInstall(result.stdout, this.target.packageName);
    if (!install) {
      throw new Error(`${this.target.packageName} is not installed with npm -g on this host`);
    }
    return install;
  }

  installLatest(options: NpmGlobalOptions = {}): Promise<CommandResult> {
    const prefixArgs = options.prefix ? ["--prefix", options.prefix] : [];
    const { packageName, registry } = this.target;
    // A scoped registry flag pins only this package's scope to the approved registry.
    const registryArgs = registry ? [`--${packageName.split("/")[0]}:registry=${registry}`] : [];
    return this.runCommand(
      "npm",
      ["install", "-g", `${packageName}@latest`, ...registryArgs, ...prefixArgs],
      {
        timeout: NPM_INSTALL_TIMEOUT_MS,
        maxBuffer: NPM_MAX_BUFFER_BYTES,
      },
    );
  }
}

// The target follows the identity of the server package this daemon runs from, read when it is used. A daemon whose
// identity cannot be read refuses to inspect or update rather than guessing a package.
export function resolveRunningPaseoCliTarget(moduleUrl: string = import.meta.url): PaseoCliTarget {
  const serverPackage = findServerPackageFrom(fileURLToPath(moduleUrl));
  if (!serverPackage) {
    throw new Error("Cannot determine this daemon's server package; self-update is unavailable");
  }
  return resolvePaseoCliTarget(serverPackage.name);
}

class RunningServerNpmGlobalPaseoCli implements NpmGlobalPaseoCli {
  private delegate(): DefaultNpmGlobalPaseoCli {
    return new DefaultNpmGlobalPaseoCli(runExternalCommand, resolveRunningPaseoCliTarget());
  }

  get target(): PaseoCliTarget {
    return resolveRunningPaseoCliTarget();
  }

  inspect(options?: NpmGlobalOptions): Promise<NpmGlobalPaseoInstall> {
    return this.delegate().inspect(options);
  }

  installLatest(options?: NpmGlobalOptions): Promise<CommandResult> {
    return this.delegate().installLatest(options);
  }
}

export const npmGlobalPaseoCli: NpmGlobalPaseoCli & { readonly target: PaseoCliTarget } =
  new RunningServerNpmGlobalPaseoCli();
