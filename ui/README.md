# @ameesh/ui

Embeddable web components of an [ameesh](https://github.com/manaty/ameesh) mesh:
projects, timeline, agent, alerts, hosts, budget and threads, as standard custom
elements (Lit, Shadow DOM) — no framework is imposed on the host application.

The same sources are served by `ameesh serve` as a standalone page (`/ui/`, the
reference client); this package is the form for an application that embeds the
interface: an Electron desktop application, or a web page.

Design: `docs/design/etudes/ui-integrable.md` (study §2). Licence:
**AGPL-3.0-only**; a third party that embeds the components in a proprietary
application receives a written exception from the owner, case by case
(decision 0013).

**Not published yet.** The package is prepared (`npm run dist`, `npm pack`); the
publication is a release gesture, with the owner's go.

## Install

```sh
npm install @ameesh/ui
```

`lit` is a dependency of the package: let your bundler resolve it, do not bundle
a second copy (two registries of custom elements).

## Use

```html
<ameesh-config api-base="https://mesh.example.org">
  <ameesh-app></ameesh-app>
</ameesh-config>
```

```js
import { connectHost } from "@ameesh/ui";

const host = {
  // the host holds the token (Electron: the main process, sealed); the
  // components never store it
  getToken: (mesh) => tokens.for(mesh),
  // an approval always opens in the foreground: system browser or native facade
  openApproval: (url) => shell.openExternal(url),
  // system notification; the main process can notify with the window closed
  notify: (alert) => new Notification(alert.title ?? "ameesh", { body: alert.text }),
  // meshes this host knows; the first one is the default
  meshes: () => [{ id: "org", name: "Org", apiBase: "https://mesh.example.org" }],
  // optional navigation, when the host opens the screens itself
  open: (target) => router.go(target),
};

const unbind = connectHost(host, { root: document });
```

`connectHost` feeds the `<ameesh-config>` found under `root` (or creates one) and
routes the components' events to the host:

| Event | Host member |
|---|---|
| `ameesh:open-project`, `ameesh:open-agent`, `ameesh:open-lot` | `open(target)` |
| `ameesh:approval-requested` (`detail.url`) | `openApproval(url)` |
| `ameesh:alert` (`detail.text`) | `notify(alert)` |

Nothing else crosses: no `window.open`, no cookie, no local storage, no access
to the host page's navigation (study §2.2).

## Host contract

| Member | Required | Role |
|---|---|---|
| `getToken(mesh?)` | yes | token of the mesh; `null` asks the user (the standalone form) |
| `openApproval(url)` | yes | open an approval URL in the foreground |
| `notify(alert)` | yes | system notification (`{ text, title?, kind?, url?, mesh? }`) |
| `meshes()` | no | meshes known; the first is the default (`{ id?, name?, apiBase }`) |
| `open(target)` | no | navigate to `{ kind, id, mesh? }` instead of leaving it to the page |
| `theme` | no | `auto` (default), `light`, `dark` |
| `language` | no | `fr`, `en` |

In Electron, expose exactly this object through `contextBridge` from a preload
with `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false` — see
`docs/ui/GUIDE-ELECTRON.md`.

## Screens

The bundle served at `/ui/ameesh-ui.js` registers `<ameesh-app>` and seven
independent elements: `<ameesh-projects>`, `<ameesh-timeline>`, `<ameesh-agent>`,
`<ameesh-alerts>`, `<ameesh-hosts>`, `<ameesh-budget>` and `<ameesh-thread>`.
The standalone `/ui/` page assembles them with internal navigation.

An embedded screen uses the nearest `<ameesh-config api-base="…">`, or a
client supplied by the host:

```js
import { createClient } from './ameesh-ui.js';
const screen = document.querySelector('ameesh-timeline');
screen.client = createClient({ baseUrl: meshUrl, tokenProvider: getCurrentToken });
screen.project = 'ameesh';
```

`project` selects the timeline, project thread and optional plan gauges in the
budget screen. `agent` selects the agent card. `lot` narrows a thread to a lot;
without it the thread is the project's. The projects screen accepts an optional
`project` filter. Without a required selection, the component displays a prompt.
`lang="fr"` / `lang="en"`, `theme` and the L88 CSS tokens apply to each screen.

Selection buttons emit bubbling, composed `ameesh:open-project`,
`ameesh:open-agent` and `ameesh:open-lot` events. Their `detail` contains the
selected `project`, `agent` or `lot`; the host chooses navigation. The app handles
these events internally while leaving them available to its host. Thread text
is rendered literally. These screens do not expose write actions.

Views using the same client share one `/api/v1/events` connection. Event bursts
coalesce into at most one scheduled refresh per second per view; a change during
a read schedules another read afterward. Reconnection and `reset` also reload
the snapshot. Replacing a view during navigation keeps the shared stream;
removing the last view aborts it after the current DOM update. A missing SSE endpoint
keeps the initial snapshot and manual Refresh button available with a visible
status. The live service is supplied by L85 (#373).

## Content Security Policy and theme

The components work under a strict CSP: `script-src 'self'` (or the module's
hash), no `eval`, no inline style (constructed stylesheets), `connect-src`
limited to the mesh's origin. Texts are rendered as text, never as HTML.

The theme goes through public CSS tokens (`--ameesh-color-page`,
`--ameesh-color-ink`, `--ameesh-color-good`, `--ameesh-color-critical`,
`--ameesh-font`, `--ameesh-radius`…), light and dark, and the `theme` attribute.
`import "@ameesh/ui/tokens"` writes the token sheet.

## Guides

* `docs/ui/CONTRAT-HOTE.md` — the host contract, in detail;
* `docs/ui/GUIDE-ELECTRON.md` — preload, partitions per mesh, notifications, CSP;
* `docs/ui/GUIDE-PAGE-WEB.md` — ES module, SRI, CORS, token provider;
* `ui/examples/` — a fictional organisation, opened with the components.

## Develop

Run `npm run typecheck`, `npm test`, `npm run build`, then `npm run check` in
`ui/`. The build is committed in `src/ameesh/ui/static/` for Python packaging.
`npm run dist` writes `dist/` (ES module with `lit` external, type declarations)
and the licence: what `npm pack` would ship.
