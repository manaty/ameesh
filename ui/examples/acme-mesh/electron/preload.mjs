// SPDX-License-Identifier: AGPL-3.0-only
// Reference preload of an Electron host (L92, study §2.3): exactly the host
// contract crosses `contextBridge`, and nothing else — no ipcRenderer, no
// Node, no module. GUIDE-ELECTRON.md explains each member.
import { contextBridge, ipcRenderer } from "electron";

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
