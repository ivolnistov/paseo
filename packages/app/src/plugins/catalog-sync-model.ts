import type { AudioEngine } from "@/audio";
import type {
  DaemonClient,
  PluginClientBundleStore,
} from "@getpaseo/client/internal/daemon-client";
import { createCatalogRefresh, type CatalogCatchUpSignal } from "./catalog-refresh";
import type { PluginRegistry } from "./registry";
import { pluginSettingsKey, pluginSettingsPrefix } from "./settings/query";

interface CatalogSyncInput {
  serverId: string;
  client: Pick<DaemonClient, "getPluginCatalog" | "observeEvents">;
  audio: Pick<AudioEngine, "play"> | null;
  connected: boolean;
  supported: boolean;
  /** The host's persistent client bundle cache, passed to every catalog fetch. */
  bundles: PluginClientBundleStore;
  /** Timeline catch-up for this host; observation starts only once it exists. */
  catchUp: CatalogCatchUpSignal | null | undefined;
  registry: Pick<PluginRegistry, "installCatalog" | "getSnapshot" | "removeHost">;
  schedule?: (task: () => void, delayMs: number) => () => void;
}

function defaultSchedule(task: () => void, delayMs: number): () => void {
  const timer = setTimeout(task, delayMs);
  return () => clearTimeout(timer);
}

/**
 * Keeps one host's installed plugins in step with its catalog. A dropped socket reconnects on the
 * same client, so installations stay loaded while disconnected; the registry replaces them when
 * the client changes. Returns the cleanup for this run.
 */
export function syncPluginCatalog({
  serverId,
  client,
  audio,
  connected,
  supported,
  bundles,
  catchUp,
  registry,
  schedule = defaultSchedule,
}: CatalogSyncInput): () => void {
  if (!supported || !audio) {
    registry.removeHost(serverId);
    return () => {};
  }
  if (!connected) return () => {};
  // SessionProvider creates the catch-up owner in an effect; observe only after it is published.
  if (!catchUp) return () => {};
  const installationOptions = { client: client as DaemonClient, audio };
  let cancelled = false;
  let snapshotPending = false;
  let refreshQueue = Promise.resolve();

  function invalidateHostSettings() {
    // Retained replicas miss offline writes and do not refetch when a screen mounts.
    for (const plugin of registry.getSnapshot()) {
      if (plugin.serverId !== serverId) continue;
      void plugin.queryClient.invalidateQueries({
        queryKey: pluginSettingsPrefix,
        refetchType: "all",
      });
    }
  }

  function refresh(replacePluginId?: string) {
    const afterSnapshot = snapshotPending;
    snapshotPending = false;
    refreshQueue = refreshQueue.then(() =>
      client
        .getPluginCatalog(bundles)
        .then((catalog) => {
          if (cancelled) return undefined;
          registry.installCatalog(serverId, catalog, { ...installationOptions, replacePluginId });
          if (afterSnapshot) invalidateHostSettings();
          return undefined;
        })
        .catch((error) => {
          if (!cancelled) console.warn(`[Plugins] Failed to load catalog for ${serverId}`, error);
          return undefined;
        }),
    );
  }

  const catalogRefresh = createCatalogRefresh({ catchUp, refresh, schedule });
  const observation = client.observeEvents([
    "status.plugin_catalog_changed",
    "status.plugin_settings_changed",
  ]);
  observation.subscribe({
    snapshot: () => {
      // An explicit change may run the deferred snapshot's refresh; the settings refresh still follows it.
      snapshotPending = true;
      catalogRefresh.snapshot();
    },
    update: (message) => {
      if (message.type !== "status") return;
      if (message.payload.status === "plugin_settings_changed") {
        const { pluginId, settingsId } = message.payload;
        if (typeof settingsId === "string") {
          const plugin = registry
            .getSnapshot()
            .find((item) => item.serverId === serverId && item.id === pluginId);
          void plugin?.queryClient.invalidateQueries({ queryKey: pluginSettingsKey(settingsId) });
        }
      }
      if (message.payload.status === "plugin_catalog_changed") {
        const pluginId = message.payload.pluginId;
        if (typeof pluginId === "string") catalogRefresh.changed(pluginId);
      }
    },
  });
  return () => {
    cancelled = true;
    catalogRefresh.dispose();
    void observation
      .release()
      .catch((error) => console.warn("[Plugins] Failed to release catalog", error));
  };
}
