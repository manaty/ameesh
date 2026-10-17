// SPDX-License-Identifier: AGPL-3.0-only
// Reference preload of an Electron host (L92, study §2.3): exactly the host
// contract crosses `contextBridge`, and nothing else — no ipcRenderer leaked,
// no Node, no module.
//
// CommonJS, self-contained, `require("electron")` only: a *sandboxed* preload
// cannot use ESM (`import`), and Electron loads it before any bundler runs.
// The renderer itself stays ESM (renderer.mjs); the main process may be ESM
// on Electron ≥ 28. See GUIDE-ELECTRON.md.
const { contextBridge, ipcRenderer } = require("electron");

const invoke = (channel, ...args) => ipcRenderer.invoke(channel, ...args);

/**
 * The main process answers `ameesh:open` on `ameesh:navigate` (main.mjs): the
 * renderer's router subscribes here — not on a DOM event nothing dispatches,
 * and not on `ipcRenderer`, which never crosses the bridge. One channel,
 * callbacks only, and an unsubscribe: a bounded bridge, not the IPC surface.
 */
const navigateListeners = new Set();
ipcRenderer.on("ameesh:navigate", (_event, target) => {
  for (const listener of [...navigateListeners]) {
    try {
      listener(target);
    } catch {
      // one renderer listener must not break the channel for the others
    }
  }
});

contextBridge.exposeInMainWorld("ameeshHost", {
  getToken: (mesh) => invoke("ameesh:get-token", mesh),
  openApproval: (url) => invoke("ameesh:open-approval", url),
  notify: (alert) => invoke("ameesh:notify", alert),
  meshes: () => invoke("ameesh:meshes"),
  open: (target) => invoke("ameesh:open", target),
  onNavigate: (listener) => {
    if (typeof listener !== "function") {
      throw new TypeError("onNavigate expects a function");
    }
    navigateListeners.add(listener);
    return () => {
      navigateListeners.delete(listener);
    };
  },
  theme: "auto",
  language: "en",
});
