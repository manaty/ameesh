# Acme — example application (L92)

A fictional organisation, **Acme**, opens the interface of its mesh in a host
page: the same sources `ameesh serve` serves, bound to a host object through
the contract of `docs/ui/CONTRAT-HOTE.md`. Everything here is a **fixture**:
no real mesh, no database, no passkey, the read API is `fixtures.mjs`.

```sh
node ui/examples/acme-mesh/fixtures-server.mjs        # http://127.0.0.1:8799/
node ui/examples/acme-mesh/fixtures-server.mjs --port 0
```

Open <http://127.0.0.1:8799/>: the projects of Acme (`acme-portal`), the
timeline of lot #12, the agents, alerts, hosts, budget and threads. The
toolbar belongs to the host page, not to the components:

* **Theme** / **Language** set `<ameesh-config>` (and the host chrome follows
  through the published tokens, `data-ameesh-theme`);
* **Notify the host** raises `ameesh:alert` — the host shows a system
  notification when the browser allows it, else the status line;
* **Request an approval** raises `ameesh:approval-requested` — the host opens
  `/a/demo` in a new tab, a fixture of an approval page (nothing is signed).

| File | Role |
|---|---|
| `index.html`, `example.css` | the host page: its chrome, and the components inside `<ameesh-config>` |
| `app.mjs` | browser glue: imports the served module `/ui/ameesh-ui.js`, mounts, wires the toolbar |
| `mount.mjs` | binds the host object to the page (`connectHost`); the proof calls the same wiring |
| `host.mjs` | the host object: `getToken`, `openApproval`, `notify`, `meshes`, `open`, theme, language |
| `fixtures.mjs` | the fictional mesh: discovery, projects, progress, agents, alerts, hosts, budget, threads |
| `fixtures-server.mjs` | stdlib HTTP server: the page, `/ui/ameesh-ui.js`, the fixtures, the event stream, the approval fixture |
| `electron/` | a reference Electron host (main, preload, renderer), see its README |
| `../../test/example.test.mjs` | the targeted proof: the page opens the fixtures in jsdom, the server answers like a mesh |

The fixtures server refuses every `/api/v1/` call without
`Authorization: Bearer acme-demo-token` (401, `ameesh-error/1`), exactly like a
mesh; the example host hands that token to the components.

Guides: `docs/ui/CONTRAT-HOTE.md`, `docs/ui/GUIDE-ELECTRON.md`,
`docs/ui/GUIDE-PAGE-WEB.md`. This example only reads; the write routes
(actions, decisions) arrive with L87 and L90.
