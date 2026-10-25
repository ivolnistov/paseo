import { useVoiceAudioEngineOptional } from "@/contexts/voice-context";
import { useEffect, useMemo } from "react";
import type { DaemonClient } from "@getpaseo/client/internal/daemon-client";
import { useHostFeature } from "@/runtime/host-features";
import { useHostRuntimeIsConnected } from "@/runtime/host-runtime";
import { pluginRegistry } from "./registry";
import { useSessionStore } from "@/stores/session-store";
import { createHostPluginClientBundleStore } from "./client-bundle-storage";
import { syncPluginCatalog } from "./catalog-sync-model";

export function PluginCatalogSync({
  serverId,
  client,
}: {
  serverId: string;
  client: DaemonClient;
}) {
  const audio = useVoiceAudioEngineOptional();
  const connected = useHostRuntimeIsConnected(serverId);
  const supported = useHostFeature(serverId, "plugins");
  const timelineSync = useSessionStore((state) => state.sessions[serverId]?.viewedTimelineSync);
  const bundles = useMemo(() => createHostPluginClientBundleStore(serverId), [serverId]);

  useEffect(
    () =>
      syncPluginCatalog({
        serverId,
        client,
        audio,
        connected,
        supported,
        bundles,
        catchUp: timelineSync,
        registry: pluginRegistry,
      }),
    [audio, bundles, client, connected, serverId, supported, timelineSync],
  );

  useEffect(() => () => pluginRegistry.removeHost(serverId), [serverId]);
  return null;
}
