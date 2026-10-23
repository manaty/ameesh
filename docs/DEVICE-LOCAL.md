# My workstation: the local state of the runners (L93)

`<ameesh-this-device>` shows what runs on **this** machine — the personas,
the lots, the turns, the sandbox resources and the lease — to the person who
owns it, even when that person is not a member of the organisation that
borrows the workstation. That state never goes through a mesh's API: each
agent-runner serves it on a **Unix socket**, in the `ameesh-device/1` schema,
read-only and without a network token (study
[`ui-integrable.md`](design/etudes/ui-integrable.md) §4).

This is the ameesh side of the lot. The Nexlink Compute integration
(NX-COMPUTE-3) consumes this state and provides the **immediate** stop; the
contract between the two is described below.

## Where the channels live

| Path | Content | Mode |
|---|---|---|
| `$AMEESH_DEVICE_DIR/device-<runner>.sock` | one `ameesh-device/1` state per connection | `0600`, directory `0700` |
| `$AMEESH_DEVICE_DIR/control-<runner>.sock` | one request/reply; the clean stop | `0600`, directory `0700` |

`AMEESH_DEVICE_DIR` defaults to `$XDG_RUNTIME_DIR/ameesh`, then to
`$AMEESH_STATE/device` for a service with no user session. Each runner has
its own pair, because the units are per agent (`ameesh-runner-agent@<agent>`);
a workstation therefore has one socket pair per agent, and the client
**merges** them (see below). `AMEESH_DEVICE=0` disables the channels;
`AMEESH_DEVICE_CONTROL=0` keeps the read-only state and removes the stop
channel.

A newly created channel directory is `0700`. An existing directory must
already be owned by the runner's Unix user and private (no group or other
permissions); otherwise channels are skipped with a diagnostic. Its permissions
are never changed: point `AMEESH_DEVICE_DIR` to a dedicated private directory,
not a shared directory or the home directory. At startup, abandoned
`device-*.sock` and `control-*.sock` names are removed only when a probe gets
`ECONNREFUSED` or `ENOENT`, including on persistent storage after a runner PID
changes. Busy, inaccessible and live sockets, symlinks and regular files are
left alone. A timeout or full backlog does not prove a socket is stale.

When the platform exposes peer credentials (`SO_PEERCRED`), the channel
refuses a connection from another Unix user. There is no TCP listener, no
HTTP route and no token: the file-mode of the socket is the boundary, like
`agent-runner stop` itself. A channel is skipped, never fatal, when the
directory is not writable, the path is too long for `AF_UNIX`, or the runner
is mediated or `--once`.

## The `ameesh-device/1` state

```json
{
  "schema": "ameesh-device/1",
  "generated_ts": 1792534585.1,
  "host": "pc-smichea",
  "runners": [{"id": "runner-alice@pc-smichea", "pid": 1234,
               "since_ts": 1792530000.0,
               "socket": "/run/user/1000/ameesh/device-runner-alice_pc-smichea.sock",
               "control_socket": "/run/user/1000/ameesh/control-runner-alice_pc-smichea.sock"}],
  "meshes": [{
    "mesh": "manaty",
    "organisation": "manaty",
    "agents": [{
      "name": "alice-dev",
      "persona": "alice-dev",
      "harness": "deepseek",
      "status": "running",
      "status_text": "turn: build",
      "project": "ameesh",
      "lot": {"id": 382, "title": "L93: my workstation", "state": "build", "app": "ameesh"},
      "turn": {"active": true, "started_ts": 1792533973.9, "label": "messages"},
      "lease": {"live": true, "owner": "runner-alice@pc-smichea", "expires_ts": 1792534773.3},
      "sandbox": {"measured": true, "cpu": 0.12, "cpu_some": 0.0,
                  "memory_bytes": 857096192, "memory_max_bytes": 6442450944,
                  "memory_used": 0.133, "throttled": null, "network": null},
      "runner": "runner-alice@pc-smichea",
      "since_ts": 1792533973.0,
      "last_turn_ts": 1792532609.4
    }]
  }],
  "resources": {"sampled_ts": 1792534580.0, "cpu_count": 8, "load1": 1.34,
                "mem_available_bytes": 28218576896, "mem_total_bytes": 33648889856,
                "app_cpu": 1.31, "guest_cpu": 0.0, "guest_mem_bytes": 28672,
                "turns_in_progress": 2},
  "compute": {"gate": {"schema": "ameesh-host-state/1", "state": "available",
                       "seq": 8, "reason": "idle"}, "lease": null},
  "unmeasured": ["network"]
}
```

* `meshes[]` groups the personas by canon (the organisation); a per-agent
  runner reports its own agents, so the client merges the meshes of every
  socket. `organisation` is the canon's id.
* `project` and `lot` are the work the agent's current session cites (or its
  latest assigned open lot); the title is a copy, never an authority.
* `turn.active` and `turn.started_ts` come from the lease accounting; a
  running turn always has a live lease.
* `sandbox` is the agent's cgroup on this host, as `ameesh hosts` already
  publishes it. **Network is not measured** by ameesh; it is `null` and named
  in `unmeasured`, never invented as zero.
* `compute.gate` is the host gate state (`ameesh-host-state/1`, L112) when a
  Compute/guard gate exists; `compute.lease` is `null` until
  NX-COMPUTE-3 provides the hub lease. The field is additive: no version
  bump when it is filled.
* No token, key, mail body or free text from the mesh is included.

### Protocol

* **State**: connect, the server writes **one** JSON object followed by a
  newline, then closes. Poll at the client's own cadence (the server caches
  the state for about a second). A client may send anything; the state
  channel is read-only and ignores it.
* **Control**: connect, write one JSON line, read one JSON line. The only
  accepted message is:

  ```json
  {"op": "stop", "agent": "alice-dev"}
  ```

  The reply is `{"schema": "ameesh-device-control/1", "ok": true, "agent":
  "...", "returned": [382]}` or `{"ok": false, "error": "..."}`. A runner
  only accepts an agent it serves (`--agents`); an omitted or empty filter
  serves all agents on this host, with the same host check on every stop. The
  stop is the exact gesture of `agent-runner stop` — status `stopped`, reason `manuel`, open lots
  returned to the team queue, current turn ending at its safe point (0030).
  It never touches an approval, a gated action, a token or the Compute lease.

### Discovery and merge

Read every `device-*.sock` in the directory and merge their states: the
`ameesh-device/1` schema keeps `runners[]` and one row per agent. For a
duplicate (same mesh, same name), the newest report wins by `generated_ts`,
never the one whose turn started earliest: a stale `running` row does not
hide a newer `idle` or `stopped` one. From the command line:

```sh
ameesh device view              # human summary
ameesh device view --json       # the merged ameesh-device/1
ameesh device stop alice-dev    # clean stop through the control socket
```

`ameesh device view` reads only the sockets (no database, no API). It accepts
`--socket PATH` (repeatable) and `--dir DIR` for tests and diagnosis; the
default is the same `AMEESH_DEVICE_DIR`/`XDG_RUNTIME_DIR` rule.

## The interface

`<ameesh-this-device>` renders the state and raises the host events; it never
opens the socket, calls the mesh or keeps a token. The host reads the socket
in its **main process** and supplies the state:

```js
// Electron main process (illustrative): read every channel, merge, then
// hand the state to the renderer through the preload's contextBridge.
import net from "node:net";
import { readdir } from "node:fs/promises";
import path from "node:path";

const dir = process.env.AMEESH_DEVICE_DIR ?? path.join(process.env.XDG_RUNTIME_DIR, "ameesh");

function readState(socket) {
  return new Promise((resolve, reject) => {
    const conn = net.createConnection(socket);
    let raw = "";
    conn.setEncoding("utf8");
    conn.on("data", (chunk) => { raw += chunk; });
    conn.on("end", () => { try { resolve(JSON.parse(raw.split("\n")[0])); } catch (e) { reject(e); } });
    conn.on("error", reject);
  });
}

function mergeDeviceStates(states) {
  const runners = new Map(), meshes = new Map();
  const ordered = [...states].sort((a, b) => (a.generated_ts ?? 0) - (b.generated_ts ?? 0));
  for (const state of ordered) {
    for (const runner of state.runners ?? []) runners.set(runner.id, runner);
    for (const mesh of state.meshes ?? []) {
      const target = meshes.get(mesh.mesh) ?? { ...mesh, agents: [] };
      for (const agent of mesh.agents ?? []) {
        // même mesh, même nom : le relevé le plus récent remplace l'ancien
        target.agents = target.agents.filter((a) => a.name !== agent.name);
        target.agents.push(agent);
      }
      meshes.set(mesh.mesh, target);
    }
  }
  return { ...states[0], runners: [...runners.values()],
    meshes: [...meshes.values()],
    generated_ts: Math.max(...states.map((s) => s.generated_ts ?? 0)) };
}

async function deviceState() {
  const names = (await readdir(dir)).filter((n) => n.startsWith("device-") && n.endsWith(".sock"));
  const states = (await Promise.all(names.map((n) => readState(path.join(dir, n)).catch(() => null))))
    .filter(Boolean);
  return mergeDeviceStates(states);
}
```

In the renderer:

```js
import { connectHost } from "@ameesh/ui";

const device = document.querySelector("ameesh-this-device");
device.provider = () => window.ameeshHost.deviceState();  // preload → main
device.poll = 5;                                           // seconds

connectHost({
  getToken: (mesh) => window.ameeshHost.getToken(mesh),
  openApproval: (url) => window.ameeshHost.openApproval(url),
  notify: (alert) => window.ameeshHost.notify(alert),
  deviceState: () => window.ameeshHost.deviceState(),      // optional (L93)
  stopAgent: (agent, options) => window.ameeshHost.stopAgent(agent, options),
});
```

`connectHost` routes `ameesh:stop` to `stopAgent(agent, {mode, runner, mesh})`.
No filesystem path is accepted from the renderer. The **main process**
rediscovers local runner channels, validates the requested agent and runner
against the fresh local state, and resolves the matching control socket itself.
Reject missing or ambiguous matches; `runner` and `mesh` are untrusted hints.
Validate IPC senders and arguments before acting, even when `connectHost` is
bypassed. The host performs:

* `mode: "clean"`: send `{"op":"stop","agent":"…"}` on that resolved socket (or run
  `ameesh device stop <agent>`); the runner finishes at its safe point, the
  lease is released, the lots return to the queue;
* `mode: "immediate"`: the component first shows the named agent and the
  risk of losing unfinished work, with explicit confirm/cancel buttons. The
  main process must also obtain native confirmation before acting (a page
  can forge an event or call IPC directly); cancellation does nothing. After
  confirmation the **Compute runner** destroys the sandbox and
  withdraws the offer; ameesh sees the lease expire and treats the agent as
  stopped. ameesh has no immediate-kill route of its own, and this is not an
  approval.

The component's state shape is exported as `DeviceState`, `DeviceMesh`,
`DeviceAgent`, `DeviceLot`, `DeviceRunner`; the host members are
`AmeeshHost.deviceState` and `AmeeshHost.stopAgent` (both optional).

## Limits and the Compute contract

* The state is per runner and per host; the client merges. There is no
  machine-wide daemon.
* Mediated executors (path B) do not serve the channel yet: their storage is
  remote and their host state is the server's. The Compute case will read the
  same schema on the device side; until NX-COMPUTE-3 lands, `compute.lease`
  stays `null` and the desktop routes the immediate stop to Compute itself.
* Network counters, GPU, and per-turn CPU beyond the cgroup are not measured;
  they are named in `unmeasured` rather than fabricated.
* The control channel is owner-control by Unix permission, exactly like
  `agent-runner stop`; it is not an authorisation to act in a mesh. The mesh
  still sees its events and its lot transitions.
