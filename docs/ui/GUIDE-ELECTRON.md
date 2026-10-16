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
window.addEventListener("ameesh:navigate", (event) => router.go(event.detail));
window.addEventListener("beforeunload", off);
```

The preload is the only crossing:

```js
// preload.mjs
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
```

Nothing else crosses: no `ipcRenderer`, no `require`, no module. Validate
every value that comes back from the renderer (URLs especially).

## Window and partitions

```js
const win = new BrowserWindow({
  webPreferences: {
    preload: path.join(__dirname, "preload.mjs"),
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
```

* **Sign-in**: authorization code + PKCE (RFC 8252) in the system browser, a
  loopback redirect, then `safeStorage`. `getToken` returns `null` until the
  session exists — the components ask again, they never loop.
* **Approvals**: `shell.openExternal` only, after an origin check against the
  meshes the application knows. The approval page refuses frames
  (`frame-ancestors 'none'`), so it is never embedded; it is not opened
  through a `BrowserWindow` either, or the human could not see their passkey
  origin.
* **Alerts with the window closed**: the main process holds
  `GET /api/v1/events` with the mesh token and shows the notification itself
  (see `followAlerts` in the reference `main.mjs`). Reconnection,
  `Last-Event-ID`, filtering by role and rate limiting belong to the
  application; the reference keeps the frame parsing minimal.

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
- [ ] the preload exposes the host contract and nothing else;
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
