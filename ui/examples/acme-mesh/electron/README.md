# Reference Electron host (L92)

`main.mjs`, `preload.mjs`, `renderer.html` and `renderer.mjs` are a **reference**
for an application that embeds `@ameesh/ui` in Electron — Nexlink, or any
desktop application. Electron is not a dependency of ameesh: these files are
not run here, `npm test` only proves they parse. The full walkthrough is
`docs/ui/GUIDE-ELECTRON.md`.

What the reference shows:

* **window**: `contextIsolation: true`, `sandbox: true`,
  `nodeIntegration: false`, and one `partition: "persist:ameesh-<mesh>"` per
  mesh (no cookie or storage crosses);
* **preload**: exactly the host contract crosses `contextBridge` — no
  `ipcRenderer`, no Node, no module;
* **main process**: the token is sealed at rest by `safeStorage`, approvals
  open in the system browser (`shell.openExternal`) and only on a known
  origin, notifications are shown by the main process, and the event stream is
  held there so an alert still notifies with the window closed;
* **renderer**: the application's own page, bundled with `@ameesh/ui`, under a
  strict CSP whose `connect-src` names the mesh.

To use it in a real application: copy the four files into the application's
Electron entry points, run `npm install @ameesh/ui electron`, bundle the
renderer, and replace `MESHES` by the application's list (entered by the human
or supplied by the organisation). The sign-in flow (authorization code +
PKCE, RFC 8252) fills `ameesh:set-token`; nothing else in the contract
changes.
