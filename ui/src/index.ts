// SPDX-License-Identifier: AGPL-3.0-only
// @ameesh/ui (L88): embeddable components of an ameesh mesh. Importing this
// module defines the elements (once, even if loaded twice).
import { AmeeshApp } from "./components/ameesh-app";
import { AmeeshProjects, AmeeshTimeline, AmeeshAgent, AmeeshAlerts, AmeeshHosts, AmeeshBudget, AmeeshThread } from "./components/read-screens";
import { AmeeshConfig } from "./components/ameesh-config";

export { ApiError, createClient, expand } from "./api/client";
export type { Body, Client, ClientOptions, ReadPath, Schemas, TokenProvider } from "./api/client";
export { EventStreamParser, readStream } from "./sse";
export type { StreamEvent, StreamOptions } from "./sse";
export { LANGUAGES, translate } from "./i18n/index";
export { HOST_EVENTS, connectHost } from "./host";
export type { AmeeshHost, AmeeshHostAlert, AmeeshHostTarget, AmeeshMesh, ConnectHostOptions, HostRow } from "./host";
export { AmeeshApp, AmeeshConfig, AmeeshProjects, AmeeshTimeline, AmeeshAgent, AmeeshAlerts, AmeeshHosts, AmeeshBudget, AmeeshThread };
export { configOf } from "./components/ameesh-config";

for (const [name, element] of [["ameesh-config", AmeeshConfig], ["ameesh-app", AmeeshApp],
  ["ameesh-projects", AmeeshProjects], ["ameesh-timeline", AmeeshTimeline], ["ameesh-agent", AmeeshAgent],
  ["ameesh-alerts", AmeeshAlerts], ["ameesh-hosts", AmeeshHosts], ["ameesh-budget", AmeeshBudget], ["ameesh-thread", AmeeshThread]] as const) {
  if (!customElements.get(name)) customElements.define(name, element);
}
