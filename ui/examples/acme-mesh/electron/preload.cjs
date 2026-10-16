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

contextBridge.exposeInMainWorld("ameeshHost", {
  getToken: (mesh) => invoke("ameesh:get-token", mesh),
  openApproval: (url) => invoke("ameesh:open-approval", url),
  notify: (alert) => invoke("ameesh:notify", alert),
  meshes: () => invoke("ameesh:meshes"),
  open: (target) => invoke("ameesh:open", target),
  theme: "auto",
  language: "en",
});
