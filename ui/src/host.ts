// SPDX-License-Identifier: AGPL-3.0-only
// The host contract (L92, study §2.3): what an application embedding the
// components offers them — a token per mesh, the opening of an approval, a
// system notification, the meshes it knows. `connectHost` binds that object
// to a page: it feeds <ameesh-config> (one token provider per mesh) and
// routes the components' DOM events, so navigation, the opening of an
// approval and system notifications stay the host's decisions (study §2.2).
// Nothing else crosses: no popup, no cookie, no storage.
import { AmeeshConfig } from "./components/ameesh-config";
import type { DeviceState } from "./components/this-device";

/** A mesh known to the host: a public API base, and what to call it. */
export interface AmeeshMesh {
  /** Identifier used by the host and carried in the events; default: apiBase. */
  id?: string;
  /** Display name (a mesh, an organisation). */
  name?: string;
  /** Public base URL of the mesh, e.g. `https://mesh.example.org`. */
  apiBase: string;
}

/** An alert a component hands to the host for a system notification. */
export interface AmeeshHostAlert {
  /** Mesh that raised the alert, when known. */
  mesh?: string;
  /** Machine kind (`ameesh-alert/1`: budget, stale, host…). */
  kind?: string;
  /** One-line title for the notification. */
  title?: string;
  /** Text; the host renders it as text, never as HTML. */
  text: string;
  /** URL to open when the human clicks the notification. */
  url?: string;
  /** The row the component read, when the host wants more than the text. */
  row?: HostRow;
}

/** A navigation the components ask the host to perform. */
export interface AmeeshHostTarget {
  /** What to open: `project`, `agent`, `lot`. */
  kind: string;
  /** Identifier in the mesh (`id`, `lot` or `name` of the row). */
  id: string;
  /** The mesh the row came from, when known. */
  mesh?: string;
  /** The row as read, for a host that shows more. */
  row?: HostRow;
}

/** A row of an ameesh read route, as the components receive it. */
export type HostRow = Record<string, unknown>;

/** What a host application exposes (through `contextBridge` in Electron). */
export interface AmeeshHost {
  /** Token of a mesh; the main process holds it. `null` asks the user. */
  getToken(mesh?: string): string | null | Promise<string | null>;
  /** Open an approval URL (system browser or native facade), never a popup. */
  openApproval(url: string): void | Promise<void>;
  /** System notification; the main process can notify with the window closed. */
  notify(alert: AmeeshHostAlert): void | Promise<void>;
  /** Meshes this host knows; the first one is the default. */
  meshes?(): AmeeshMesh[] | Promise<AmeeshMesh[]>;
  /** Optional navigation: the host opens the project, agent or lot itself. */
  open?(target: AmeeshHostTarget): void | Promise<void>;
  /** Optional theme imposed by the host (default: `auto`). */
  theme?: "auto" | "light" | "dark";
  /** Optional language imposed by the host (fr, en). */
  language?: string;
  /** L93: read the local state of this machine's runners (the main process
   * reads the Unix sockets; a renderer cannot). Null: no local runner. */
  deviceState?(): DeviceState | null | Promise<DeviceState | null>;
  /** L93: perform the stop the <ameesh-this-device> screen asked for. The
   * clean stop goes to the runner's control socket; the immediate stop is
   * the Compute runner's own gesture, never an ameesh approval. */
  stopAgent?(agent: string, options: DeviceStopOptions): void | Promise<void>;
}

/** A stop asked by <ameesh-this-device>; the host decides how. */
export interface DeviceStopOptions {
  mode: "clean" | "immediate";
  runner?: string;
  mesh?: string;
  control_socket?: string;
}

/** The detail of the `ameesh:stop` event. */
export interface DeviceStop extends DeviceStopOptions {
  agent: string;
}

export interface ConnectHostOptions {
  /** Where <ameesh-config> is searched (default: the document). */
  root?: ParentNode;
  /** The <ameesh-config> to use; default: the first under `root`, else one is created. */
  config?: AmeeshConfig;
  /** Mesh identifier passed to `getToken` and carried in the events. */
  mesh?: string;
  /** API base of a single-mesh host that has no `meshes()`. */
  apiBase?: string;
}

/** Event names the components raise for the host (study §2.2). */
export const HOST_EVENTS = {
  open: ["ameesh:open-project", "ameesh:open-agent", "ameesh:open-lot"],
  approval: "ameesh:approval-requested",
  alert: "ameesh:alert",
  deviceStop: "ameesh:stop",
} as const;

function firstConfig(root: ParentNode): AmeeshConfig | null {
  return (root as ParentNode & { querySelector?: (s: string) => Element | null })
    .querySelector?.("ameesh-config") as AmeeshConfig | null ?? null;
}

/** The mesh of `base` in the host's list; trailing slashes are ignored. */
function meshForBase(list: AmeeshMesh[], base: string): AmeeshMesh | null {
  const wanted = base.replace(/\/+$/, "");
  return list.find((item) => (item.apiBase ?? "").replace(/\/+$/, "") === wanted) ?? null;
}

/**
 * Binds a host object to a page: sets the token provider of the
 * <ameesh-config> around (or creates one) and routes the components' events
 * to `host.open`, `host.openApproval` and `host.notify`. Returns the
 * unbinding function (a page that unmounts the components calls it).
 *
 * One binding, one <ameesh-config>: the events are listened to on that
 * element, so a page that shows two meshes never hands one mesh's event to
 * the other's host (a config the binding creates wraps no component, so its
 * events are listened to on `root`). The config's `apiBase` (or `options.apiBase`) selects
 * the mesh in `meshes()`, and therefore the token: no mesh's token is sent
 * to another origin.
 */
export function connectHost(host: AmeeshHost, options: ConnectHostOptions = {}): () => void {
  const root: ParentNode = options.root ?? document;
  let config = options.config ?? firstConfig(root);
  const created = !config;
  if (!config) {
    config = document.createElement("ameesh-config") as AmeeshConfig;
    const parent = (root as Document).body ?? (root as Element);
    parent.appendChild(config);
  }
  const target = config;
  // a created config wraps none of the components: their events reach it
  // only through `root`
  const listener: EventTarget = created ? root : target;
  let mesh = options.mesh;
  let disposed = false;
  const tokenProvider = () => host.getToken(mesh);
  target.tokenProvider = tokenProvider;
  if (options.apiBase) target.apiBase = options.apiBase;
  if (host.theme) target.theme = host.theme;
  if (host.language) target.setAttribute("lang", host.language);
  if (host.meshes) {
    // a discovery that lands after the unbinding (or after a rebinding, or a
    // new selection) must touch nothing: the config is not ours any more
    void Promise.resolve(host.meshes()).then((list) => {
      if (disposed || target.tokenProvider !== tokenProvider) return;
      const known = Array.isArray(list) ? list : [];
      if (!known.length) return;
      if (!mesh) {
        // the API base chooses the mesh; without one, the first mesh is the
        // default. An unknown base keeps its host's `getToken(undefined)`
        // rather than borrowing another origin's token.
        const match = target.apiBase ? meshForBase(known, target.apiBase) : known[0];
        if (match) {
          mesh = match.id ?? match.apiBase;
          if (!target.apiBase) target.apiBase = match.apiBase;
        }
      } else if (!target.apiBase) {
        const match = known.find((item) => (item.id ?? item.apiBase) === mesh);
        if (match) target.apiBase = match.apiBase;
      }
    }).catch(() => undefined);
  }

  const on = (type: string, fn: (event: Event) => void) => listener.addEventListener(type, fn);
  const off = (type: string, fn: (event: Event) => void) => listener.removeEventListener(type, fn);

  const opened = (kind: string) => (event: Event) => {
    const row = (event as CustomEvent).detail as HostRow | undefined;
    // the views carry the identifier under the kind's own field
    // (`detail.project`, `detail.agent`, `detail.lot`); id and name stay
    // accepted for other emitters
    const value = row?.[kind] ?? row?.id ?? row?.lot ?? row?.name;
    host.open?.({ kind, id: value === undefined || value === null ? "" : String(value), mesh, row });
  };
  const openFns = HOST_EVENTS.open.map((type) => [type, opened(type.slice("ameesh:open-".length))] as const);
  const approval = (event: Event) => {
    const detail = (event as CustomEvent).detail as string | HostRow | undefined;
    const url = typeof detail === "string" ? detail : detail?.url;
    if (typeof url === "string" && url) void host.openApproval(url);
  };
  const alert = (event: Event) => {
    const detail = (event as CustomEvent).detail as AmeeshHostAlert | string | undefined;
    const value: AmeeshHostAlert = typeof detail === "string" ? { text: detail } : detail ?? { text: "" };
    if (value.text) void host.notify({ ...value, mesh: value.mesh ?? mesh });
  };
  // L93: the device screen asks for a stop; the host performs it (its main
  // process holds the control socket, the renderer never opens it).
  const deviceStop = (event: Event) => {
    const detail = (event as CustomEvent).detail as DeviceStop | undefined;
    if (!detail || typeof detail.agent !== "string" || !detail.agent) return;
    if (!host.stopAgent) return;
    void host.stopAgent(detail.agent, {
      mode: detail.mode === "immediate" ? "immediate" : "clean",
      runner: detail.runner, mesh: detail.mesh, control_socket: detail.control_socket,
    });
  };
  for (const [type, fn] of openFns) on(type, fn);
  on(HOST_EVENTS.approval, approval);
  on(HOST_EVENTS.alert, alert);
  // the device screen is not bound to a mesh: it may live outside the
  // <ameesh-config>, so its stop event is listened to on the host root
  const rootEvents = root as unknown as EventTarget;
  rootEvents.addEventListener(HOST_EVENTS.deviceStop, deviceStop);

  return () => {
    disposed = true;
    for (const [type, fn] of openFns) off(type, fn);
    off(HOST_EVENTS.approval, approval);
    off(HOST_EVENTS.alert, alert);
    rootEvents.removeEventListener(HOST_EVENTS.deviceStop, deviceStop);
    if (target.tokenProvider === tokenProvider) target.tokenProvider = undefined;
  };
}
