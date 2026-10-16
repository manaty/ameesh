# The host contract (`ameeshHost`)

The interface (`@ameesh/ui`) never decides what belongs to the host: it does
not keep a token, does not open a window, does not notify, does not navigate
and does not touch the host page's storage. It asks. This document is the
contract between the two — L92, study
[§2.2/§2.3](../design/etudes/ui-integrable.md).

A *host* is one object with four required members and three optional ones.
In Electron it crosses `contextBridge` from the preload
([GUIDE-ELECTRON.md](GUIDE-ELECTRON.md)); in a web page it is built from the
human's session ([GUIDE-PAGE-WEB.md](GUIDE-PAGE-WEB.md)). The example
application `ui/examples/acme-mesh/` implements it (`host.mjs`).

## The object

```ts
import { connectHost, type AmeeshHost } from "@ameesh/ui";

const host: AmeeshHost = {
  getToken: (mesh) => session.tokenOf(mesh),          // required
  openApproval: (url) => shell.openExternal(url),     // required
  notify: (alert) => systemNotification(alert),       // required
  meshes: () => [{ id: "acme", name: "Acme", apiBase: "https://mesh.acme.example" }],
  open: (target) => router.go(target),
  theme: "auto",
  language: "en",
};
```

| Member | Required | Role |
|---|---|---|
| `getToken(mesh?)` | yes | the token of a mesh; `null` asks the human (the standalone form does this) |
| `openApproval(url)` | yes | open an approval URL **in the foreground**; never a frame |
| `notify(alert)` | yes | a system notification; the main process may notify with the window closed |
| `meshes()` | no | the meshes the host knows; the first is the default |
| `open(target)` | no | navigate to `{ kind, id, mesh?, row? }` instead of leaving it to the page |
| `theme` | no | `auto` (default), `light`, `dark` |
| `language` | no | `fr`, `en` |

### `getToken(mesh?)`

Called **at each request**, never cached by the components — a token renewed
by the host is used by the next request. It may be synchronous or a promise.
The string is sent as `Authorization: Bearer <token>`; the client never sends
it to another origin (`createClient` refuses a path that leaves the mesh).

Returning `null` is a legitimate answer ("no session yet"): components show
"token missing or refused" and raise `ameesh:unauthorized` instead of
retrying in a loop. A host that needs a sign-in opens its own flow and calls
`connectHost` again (or sets `config.tokenProvider`) once it has one.

### `openApproval(url)`

The API files an approval and answers a `/a/<token>` link on the
ameesh-approve origin. The components raise `ameesh:approval-requested` with
that URL; the host opens it **in the foreground** — system browser, or the
application's native facade with its device key. The approval page keeps
`frame-ancestors 'none'`: it is never displayed in a frame, so the human sees
the address asking for their passkey. Approval stays a human passkey
(decision 0012); no token, no digital double signs.

### `notify(alert)`

```ts
interface AmeeshHostAlert {
  text: string;            // the notification body; rendered as text, never HTML
  title?: string;
  kind?: string;           // "budget", "stale", "host"… (ameesh-alert/1 kinds)
  url?: string;            // what to open when the human clicks it
  mesh?: string;           // set by connectHost when the component named none
  row?: Record<string, unknown>;  // the row the component read, for a richer host
}
```

The host decides the channel: system notification, tray, e-mail, nothing. To
notify **with the window closed**, the main process must hold the event stream
itself (GUIDE-ELECTRON.md); a renderer-only host can only notify while the
page lives.

### `meshes()` and `open(target)`

`meshes()` returns `{ id?, name?, apiBase }`. `id` defaults to `apiBase`; it
is what `getToken` receives and what events carry. `connectHost` uses the
first mesh as the page's default when the page declares none.

`open` receives what a selection asks for:

```ts
interface AmeeshHostTarget { kind: "project" | "agent" | "lot" | string; id: string;
  mesh?: string; row?: Record<string, unknown>; }
```

If the host does not provide `open`, the events are still raised and the host
page may listen to them directly. `<ameesh-app>` also handles them internally
(it switches its own section); an embedded screen does not.

## Binding: `connectHost`

```ts
const unbind = connectHost(host, {
  root: document,            // where <ameesh-config> is searched (default: document)
  config,                    // the <ameesh-config> to use (default: the first, else one is created)
  mesh: "acme",              // passed to getToken and carried in the events
  apiBase: "https://mesh.acme.example",   // single-mesh host with no meshes()
});
```

`connectHost`:

* feeds the `<ameesh-config>` (sets `tokenProvider`, `apiBase`, `theme`,
  `lang`);
* routes `ameesh:open-project|agent|lot`, `ameesh:approval-requested` and
  `ameesh:alert` to `open`, `openApproval` and `notify`;
* returns an unbind function — a page that unmounts the components calls it.

### Events

| Event (bubbling, composed) | `detail` | Host member |
|---|---|---|
| `ameesh:open-project`, `ameesh:open-agent`, `ameesh:open-lot` | the row | `open(target)` |
| `ameesh:approval-requested` | `{ url }` or the URL string | `openApproval(url)` |
| `ameesh:alert` | `AmeeshHostAlert` or the text | `notify(alert)` |
| `ameesh:unauthorized` | — | listener: renew the token, ask the human |

### Several meshes

One mesh, one client, one `<ameesh-config>`: no call crosses two meshes
(study §3). A host that shows two meshes mounts one config per mesh and binds
each one:

```js
const configs = meshes.map((mesh) => {
  const config = document.createElement("ameesh-config");
  config.setAttribute("api-base", mesh.apiBase);
  document.body.appendChild(config);
  connectHost(host, { config, mesh: mesh.id });
  return config;
});
```

## What the components guarantee

* **No storage**: no cookie, `localStorage`, `sessionStorage`, `indexedDB`;
  the token lives in the host only.
* **No navigation**: no `window.open`, no `location.href`; the only way out
  is an event the host answers.
* **No raw HTML**: agent and thread texts are rendered as text; no `innerHTML`
  of data, no `eval`, no `new Function`. The components work under a strict
  CSP (`script-src 'self'`, constructed stylesheets), Trusted Types
  compatible.
* **Isolation**: Shadow DOM per component, no global state, no access to the
  host page's DOM outside its own element.

What the host owes in return: a token that is actually renewed, an approval
that opens in the foreground, and a CSP whose `connect-src` names the mesh.

## Theme and language

`theme` (`auto|light|dark`) and `language` (`fr|en`) are hints the host gives
at bind time; it may change them later on the `<ameesh-config>` element:

```js
config.theme = "dark";
config.setAttribute("lang", "fr");
```

The host page styles itself with the published tokens
(`import "@ameesh/ui/tokens"`, or the served `ameesh-ui.css`) and may set
`data-ameesh-theme="dark"` on `<html>` to follow; it never reaches into a
component's shadow root.

## Status of the contract

The package is **prepared, not published** (decision 0013, release gesture
with the owner's go). The contract is additive: new optional members appear
with the lots that need them (L90 adds the decision and approval screens,
L93 the workstation view). `HOST_EVENTS` and the exported types
(`AmeeshHost`, `AmeeshHostAlert`, `AmeeshHostTarget`, `AmeeshMesh`) are the
stable names to import.
