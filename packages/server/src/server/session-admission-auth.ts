import { compare } from "bcryptjs";
import type { WSHelloMessage } from "@getpaseo/protocol/messages";
import { OWNER_PERMISSIONS } from "./authorization/index.js";
import { matchesLocalCredential } from "./local-credential.js";
import type { SessionAdmission } from "./websocket-server.js";

export type AdmissionFailure = "password_required" | "incorrect_password";

type AdmissionResolution = { admission: SessionAdmission } | { rejection: AdmissionFailure };

export async function resolveSessionAdmission(input: {
  credential: WSHelloMessage["auth"];
  passwordHash: string | undefined;
  localCredential: string | null;
  transport: "direct" | "relay";
}): Promise<AdmissionResolution> {
  const { credential, passwordHash, localCredential, transport } = input;
  if (!passwordHash) {
    return { admission: { principalId: "owner", permissions: OWNER_PERMISSIONS } };
  }
  if (!credential) {
    // COMPAT(relayPasswordOptional): owner-fork restoration of the branch upstream #7668 removed; a relay hello with
    // no credential is the owner. Remove when the owner ends this compatibility exception.
    if (transport === "relay") {
      return { admission: { principalId: "owner", permissions: OWNER_PERMISSIONS } };
    }
    return { rejection: "password_required" };
  }
  if (credential.kind === "localCredential") {
    if (localCredential && matchesLocalCredential(localCredential, credential.token)) {
      return { admission: { principalId: "owner", permissions: OWNER_PERMISSIONS } };
    }
    return { rejection: "incorrect_password" };
  }
  return (await compare(credential.password, passwordHash))
    ? { admission: { principalId: "owner", permissions: OWNER_PERMISSIONS } }
    : { rejection: "incorrect_password" };
}
