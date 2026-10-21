# Embedding the interface in an Electron application

How a desktop application (Nexlink, or another organisation's) embeds
`@ameesh/ui` — L92, study [§2.3](../design/etudes/ui-integrable.md). The
reference files are in `ui/examples/acme-mesh/electron/`: they are not run by
CI (Electron is not a dependency of ameesh); `npm test` proves they parse.

## Principles

* the components are loaded **in the renderer**, from the application's own
  bundle (npm package) — no remote code is ever executed;
* `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false`; a
  **preload** exposes exactly the host contract through `contextBridge`;
* **no `<webview>`** (discouraged by Electron). An application that prefers
  the standalone page uses a `WebContentsView` with a **partition per mesh**
  (`persist:ameesh-<id>`);
* the main process holds the tokens and can hold the event stream, so an
  alert is notified even with the window closed.

## Renderer

The application's own page, bundled with the package (Vite, esbuild,
webpack…): `import { connectHost } from "@ameesh/ui"` — the module registers
the elements. If the application instead displays the page served by
`ameesh serve`, it is an `ameesh-config token-form` page, not an embedded one.

```html
<ameesh-config>
  <ameesh-app></ameesh-app>
</ameesh-config>
<script type="module" src="renderer.mjs"></script>
```

```js
// renderer.mjs
import { connectHost } from "@ameesh/ui";
const off = connectHost(window.ameeshHost, { root: document, mesh: "acme" });
const offNavigate = window.ameeshHost.onNavigate((target) => router.go(target));
window.addEventListener("beforeunload", () => { offNavigate(); off(); });
```

The preload is the only crossing. A **sandboxed** preload cannot use ESM
(`import`): Electron loads it before any bundler, so it is a self-contained
CommonJS file, `require("electron")` only — `.cjs`, never `.mjs`
([sandbox](https://www.electronjs.org/docs/latest/tutorial/sandbox),
[ESM](https://www.electronjs.org/docs/latest/tutorial/esm)). The renderer
stays ESM; the main process may be ESM on Electron ≥ 28.

```js
// preload.cjs — self-contained CommonJS, no import/export
const { contextBridge, ipcRenderer } = require("electron");
const invoke = (channel, ...args) => ipcRenderer.invoke(channel, ...args);

// `ameesh:open` answers on `ameesh:navigate`: one channel, callbacks only.
const navigateListeners = new Set();
ipcRenderer.on("ameesh:navigate", (_event, target) => {
  for (const listener of [...navigateListeners]) {
    try { listener(target); } catch { /* one listener must not break the channel */ }
  }
});

contextBridge.exposeInMainWorld("ameeshHost", {
  getToken: (mesh) => invoke("ameesh:get-token", mesh),
  openApproval: (url) => invoke("ameesh:open-approval", url),
  notify: (alert) => invoke("ameesh:notify", alert),
  meshes: () => invoke("ameesh:meshes"),
  open: (target) => invoke("ameesh:open", target),
  onNavigate: (listener) => {
    if (typeof listener !== "function") throw new TypeError("onNavigate expects a function");
    navigateListeners.add(listener);
    return () => {
      navigateListeners.delete(listener);
    };
  },
  theme: "auto",
  language: "en",
});
```

Nothing else crosses: the page never sees `ipcRenderer`, no module, no
bundler at preload time. Validate every value that comes back from the
renderer (URLs especially).

`onNavigate` is the **only** listener channel the preload exposes: it accepts a
function, returns an unsubscribe, and a throwing callback cannot break the
others. It is not part of the `@ameesh/ui` host contract — `connectHost` calls
`open(target)`, and the application's router is what consumes the target the
main process sends back.

## Window and partitions

```js
const win = new BrowserWindow({
  webPreferences: {
    preload: path.join(__dirname, "preload.cjs"),
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    // one partition per mesh: cookies and storage never cross (study §3)
    partition: "persist:ameesh-acme",
  },
});
```

One window per mesh, or one window with a `WebContentsView` per mesh — never
two meshes in one client, and no call from one mesh to the other. A view that
gathers ("all my decisions") is composed in the application, with the
organisation shown on each entry.

## Main process

```js
// the token is sealed at rest, never written by the renderer
await writeFile(tokenPath(mesh), safeStorage.encryptString(token), { mode: 0o600 });

ipcMain.handle("ameesh:get-token", (_event, id) => readToken(meshOf(id)));
ipcMain.handle("ameesh:open-approval", (_event, url) => {
  const target = new URL(url);
  if (!APPROVAL_ORIGINS.has(target.origin)) throw new Error("refused: " + target.origin);
  return shell.openExternal(target.href);   // foreground, system browser
});
ipcMain.handle("ameesh:notify", (_event, alert) =>
  new Notification({ title: alert.title ?? "ameesh", body: alert.text }).show());
// Navigation stays the application's: the renderer's router listens.
ipcMain.handle("ameesh:open", (_event, target) => {
  win?.webContents.send("ameesh:navigate", target);
});
```

* **Sign-in**: authorization code + PKCE (RFC 8252) in the system browser, a
  loopback redirect, then `safeStorage`. `getToken` returns `null` until the
  session exists — the components ask again, they never loop.
* **Approvals**: `shell.openExternal` only, after an origin check against the
  meshes the application knows. The approval page refuses frames
  (`frame-ancestors 'none'`), so it is never embedded; it is not opened
  through a `BrowserWindow` either, or the human could not see their passkey
  origin.
* **Navigation**: `ameesh:open` is the host contract's `open(target)`; the main
  process echoes it on `ameesh:navigate`, which the preload bridges to
  `onNavigate`. The application's router owns what a target means — the
  components never navigate themselves.
* **Alerts with the window closed**: the main process holds
  `GET /api/v1/events` with the mesh token and shows the notification itself
  (see `followAlerts` in the reference `main.mjs`). Reconnection,
  `Last-Event-ID`, filtering by role and rate limiting belong to the
  application; the reference keeps the frame parsing minimal.
* **My workstation (L93)**: the local state of this machine's runners is read
  in the main process from the Unix sockets (`device-*.sock`,
  `control-*.sock`), never in the renderer — a page has no socket API. Merge
  the states and expose them as `deviceState()`; a clean stop writes
  `{"op":"stop","agent":"…"}` on the agent's control socket, an immediate
  stop belongs to the Compute runner. Nothing of this crosses the mesh. See
  [`../DEVICE-LOCAL.md`](../DEVICE-LOCAL.md).

## Content Security Policy

Serve the application's own page with a strict CSP — a `<meta>` tag is enough
for a local page; headers through the session are better (`onHeadersReceived`
or a custom protocol):

```html
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self';
               connect-src https://mesh.acme.example">
```

* `script-src 'self'` (or the bundle's hash): no `eval`, no inline script;
* `style-src 'self'`: the components use constructed stylesheets, no inline
  style;
* `connect-src` names **only** the meshes the window may call — the event
  stream is a `fetch`, so it needs the same origin;
* `frame-ancestors 'none'` for everything the application serves itself.

## Checklist

- [ ] `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false`,
      no `webview`, `webSecurity` left on (`true`);
- [ ] the preload is a self-contained CommonJS `.cjs` (`require("electron")`
      only; no `import`), and exposes the host contract and nothing else;
- [ ] `open(target)` comes back through the preload's `onNavigate`; the
      renderer never sees `ipcRenderer` and listens to no DOM event nothing
      dispatches;
- [ ] `openApproval` checks the origin and opens in the foreground;
- [ ] tokens sealed by `safeStorage`, `0600`, one per mesh;
- [ ] one partition per mesh; no storage shared between meshes;
- [ ] CSP with `connect-src` limited to the known meshes;
- [ ] the renderer is the application's bundle: no remote code, no
      `file://` page calling the API cross-origin.

## Not in this lot

The package is prepared but **not published** (decision 0013; publication is
a release gesture with the owner's go). The decision and approval screens
(`<ameesh-decisions>`) arrive with L90; until then an application can raise
`ameesh:approval-requested` itself, as the example toolbar does.
