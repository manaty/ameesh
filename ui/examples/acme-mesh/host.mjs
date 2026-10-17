// SPDX-License-Identifier: AGPL-3.0-only
// The host object of the L92 example (study §2.3): what a host application
// offers the components — a token per mesh, an approval opened in the
// foreground, a system notification, the list of its meshes, navigation.
// In Electron this same object crosses `contextBridge` from the preload
// (electron/preload.cjs); here it lives in the page, which is what a web host
// would do with its own session.
import { DEMO_TOKEN, MESH_ID, ORGANISATION } from "./fixtures.mjs";

/**
 * @param {object} options
 * @param {Window} options.window  the page (or a jsdom window in the proofs)
 * @param {string} options.apiBase public base URL of the mesh
 * @param {string} [options.token] token handed to the components
 * @param {(text: string) => void} [options.status] one line for the human
 */
export function createHost({ window, apiBase, token = DEMO_TOKEN, status = () => {} }) {
  const meshes = [{ id: MESH_ID, name: ORGANISATION, apiBase }];
  return {
    // Asked at each request; never stored by the components. `null` would ask
    // the human: an unknown mesh has no token here.
    getToken: (mesh) => {
      if (mesh && mesh !== MESH_ID) return null;
      status(`token requested for mesh ${mesh ?? MESH_ID}`);
      return token;
    },
    // An approval is a human passkey on the team's origin: it opens in the
    // foreground, never in a frame (decision 0012). Electron uses
    // shell.openExternal; a web host would use its own navigation.
    openApproval: (url) => {
      status(`approval to sign: ${url}`);
      window.open(url, "_blank", "noopener");
    },
    // The host decides how to notify; with the window closed, only a main
    // process that holds the stream can (GUIDE-ELECTRON.md).
    notify: (alert) => {
      const title = alert.title ?? "ameesh";
      if (typeof window.Notification === "function" && window.Notification.permission === "granted") {
        new window.Notification(title, { body: alert.text });
      }
      status(`notification (${title}): ${alert.text}`);
    },
    meshes: async () => meshes,
    // Optional: the host owns navigation; the components only raise events.
    open: (target) => {
      status(`open ${target.kind} ${target.id} on ${target.mesh ?? MESH_ID}`);
      window.location.hash = `#/${target.kind}/${encodeURIComponent(target.id)}`;
    },
    theme: "auto",
    language: "en",
  };
}
