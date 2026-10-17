# Embedding the interface in a web page

How a web page embeds `@ameesh/ui` and reads a mesh — L92, study
[§2.3/§2.4](../design/etudes/ui-integrable.md). For an Electron application,
see [GUIDE-ELECTRON.md](GUIDE-ELECTRON.md); the contract itself is
[CONTRAT-HOTE.md](CONTRAT-HOTE.md).

## Two ways to get the module

| Way | For | How |
|---|---|---|
| npm package | an application with a bundler | `npm install @ameesh/ui`, `import { connectHost } from "@ameesh/ui"` |
| served module | the standalone page of `ameesh serve`, or a page that copies it | `<script type="module" src="https://mesh.example.org/ui/ameesh-ui.js">` |

Same sources, same exports. The package keeps `lit` as a dependency: let the
bundler resolve it once, never bundle a second copy (two registries of custom
elements).

**Not published yet**: `private` is still set in `ui/package.json`; the
publication is a release gesture (decision 0013) with the owner's go. Until
then, build from the repository (`npm run dist`) or use the served module.

## Minimal page (bundler)

```html
<ameesh-config>
  <ameesh-app></ameesh-app>
</ameesh-config>
```

```js
import { connectHost } from "@ameesh/ui";
import "@ameesh/ui/tokens";        // the published CSS tokens, once

const host = {
  // from the human's session; the components never store it
  getToken: (mesh) => session.tokenFor(mesh),
  // an approval opens in the foreground: system browser, or a native facade
  openApproval: (url) => window.open(url, "_blank", "noopener"),
  // the page's own notification channel
  notify: (alert) => notifyHuman(alert),
  meshes: () => [{ id: "acme", name: "Acme", apiBase: "https://mesh.acme.example" }],
  open: (target) => router.go(target),
};

const unbind = connectHost(host, { root: document, mesh: "acme" });
```

Individual screens work the same way, inside their own `<ameesh-config>`:

```html
<ameesh-config api-base="https://mesh.acme.example">
  <ameesh-timeline project="acme-portal"></ameesh-timeline>
</ameesh-config>
```

## Without a bundler

The module is an ES module with bare `lit` imports; a page that imports it
directly needs an import map, or the bundle served by `ameesh serve` (which
inlines Lit — that is `/ui/ameesh-ui.js`).

```html
<script type="module"
        src="https://mesh.example.org/ui/ameesh-ui.js"
        integrity="sha384-…"
        crossorigin="anonymous"></script>
```

Subresource Integrity pins the exact bytes the page executes: compute the
hash on the file the mesh serves and update it on every interface upgrade.

```sh
openssl dgst -sha384 -binary ameesh-ui.js | openssl base64 -A
```

The module registers the elements when it loads, and exports the same names
as the package: a bundler-less page imports it by URL from its own module
(an external `app.mjs`, so that `script-src 'self'` still holds).

```js
// app.mjs, next to the page
import { connectHost } from "https://mesh.example.org/ui/ameesh-ui.js";
```

## Content Security Policy

The components run under a strict CSP:

```html
<meta http-equiv="Content-Security-Policy" content="
  default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self';
  connect-src https://mesh.acme.example; base-uri 'none'; form-action 'none'">
```

* `script-src 'self'` (or the module's hash): no `eval`, no inline script;
* `style-src 'self'`: constructed stylesheets only, no inline style;
* `connect-src` names **only** the meshes the page may read — the read calls
  and the event stream are `fetch`es, so they need that origin;
* thread and agent texts are rendered as text: no `innerHTML`, Trusted Types
  compatible.

## CORS and the token

The API answers `Access-Control-Allow-Origin` only for origins the mesh
declares (study §2.4: "the mesh lists the page's origin in its allowed CORS
origins"). A cross-origin page needs:

* `Access-Control-Allow-Origin: https://portal.example.org` (exact origin,
  `Vary: Origin`);
* `Access-Control-Allow-Headers: Authorization, Last-Event-ID` — both are
  outside the CORS-safelisted set, so the preflight must accept them;
* no cookies: the client sends `credentials: "omit"`; the token travels in
  the `Authorization` header only;
* for the event stream, the same origin and headers on
  `GET /api/v1/events`.

**Not available yet**: the v1 local service (`ameesh serve`) serves the API
and the standalone page on one origin and emits no CORS header. Cross-origin
embedding — the Nexlink page case — arrives with the v2 API service
(L84/L86); until then, embed the interface on the mesh's own origin, or put
the page behind the same origin. The fixture example
(`ui/examples/acme-mesh`) is same-origin by design.

The token itself is the human's session token (OIDC in v2; the local token
file in v1). The page must not keep it in `localStorage`: keep it in memory,
or in the server session behind a token provider.

## Frameworks

The components are standard custom elements: no framework is imposed.

```jsx
// React
function Mesh() {
  const ref = useRef(null);
  useEffect(() => {
    const off = connectHost(window.ameeshHost, { root: ref.current });
    return off;
  }, []);
  return <div ref={ref}><ameesh-config><ameesh-app /></ameesh-config></div>;
}
```

* set **properties**, not attributes, for objects and booleans
  (`el.project = "acme-portal"`, not `el.setAttribute("project", …)`);
* listen to the events (`addEventListener("ameesh:open-lot", …)`) — they
  bubble and cross shadow boundaries;
* never render data with `innerHTML` into a component; pass the row as a
  property.

## Real time

All views of one client share a single `GET /api/v1/events` connection
(`text/event-stream`): opening a project does not open a new stream, and the
last view unmounted closes it. Reconnection uses `Last-Event-ID`; a mesh that
does not implement the stream yet still shows the initial snapshot and the
Refresh button.

## Standalone page

`ameesh serve` serves the reference page on `/ui/`, same origin as the API,
with its own CSP (`connect-src 'self'`, no frame): the human pastes the local
token (`<ameesh-config token-form>`, default file
`~/.config/ameesh/serve-token`). It is the reference client, not an embedded
one.

## Check it live

`node ui/examples/acme-mesh/fixtures-server.mjs` opens a fictional
organisation ("Acme") with the same components, a fixtures API, a token
refused without `Authorization`, and a stream — the page in
`ui/examples/acme-mesh/index.html` is a complete host page to copy from.
