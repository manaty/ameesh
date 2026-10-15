# Lending the CI VM in mode b1 (L159h): the operator's commands

The exact commands that lend the CI VM to ameesh agents' heavy suites, mode
(b1) only: `ameesh offload` over SSH, outside the CI's busy hours. The design
is in the [L159 study](../../docs/design/etudes/l159-capacite-des-hotes.md),
sections 2.2, 3 (CI VM) and 4.2 step 6; the offload itself in
[README.md](README.md).

Decision #47757 (owner's key, 2026-10-15, lot #193): option b1, with two
changes from the study:

* the lending windows avoid the **11:00 and 23:00 cut-offs** (Europe/Paris,
  canon `processus/lots.md` §11 bis), when each project's package runs its
  single full CI. The study's "four hours of CI in the afternoon" no longer
  hold;
* **bureau**, not the owner, runs the installation below.

## Why b1, and only b1, on this VM

The CI's `runner` user is in the `docker` group, which is root on the VM:
any CI job, a PR's code included, can read the whole disk and kill the
guest's processes. So:

* **no agent and no secret** on this VM: no guest runner (a1), no
  `guest.env`, no harness key, no forge token, no Postgres role.
  `install-pilot.sh --offload-only` installs the guard alone, and its
  `--check --offload-only` fails if a secrets file or a guest runner shows up;
* an offloaded job carries a snapshot of a working tree and brings back
  its output and exit code. **What a CI job could do to it:** read that
  snapshot (code the forge has anyway), kill it (the client then reruns it on
  the workstation), or forge its result. A green offloaded run is therefore
  never a proof of review or of merge: the frozen-head CI and the package CI
  (§3, §12) are the only ones that count, and they run on the forge's
  runners, not through `ameesh offload`.

## The offer

To be written in the VM's `Host` card of the canon (`hotes/<vm>.md`,
`policy.lend`) by a PR on manaty/home, and given to the guard as
`--lend`. `<…>` are the values bureau reads on the VM before the PR
(16 vCPU and 64 GB in the study's inventory of 11/10):

```json
{
  "cpu_max": 6,
  "cpu_weight": 10,
  "memory_max": "18GiB",
  "agent_memory_max": "6GiB",
  "disk_max": "40GiB",
  "windows": ["mon-sun 01:00-10:30 Europe/Paris", "mon-sun 13:00-22:30 Europe/Paris"],
  "reclaim": {
    "memory_available_min": "8GiB",
    "cpu_pressure_max": "20%",
    "memory_pressure_max": "2%",
    "io_pressure_max": "20%",
    "disk_free_min": "10GiB",
    "disk_paths": ["/", "/srv/ameesh-guest"]
  },
  "drain_seconds": 90,
  "calm_seconds": 600
}
```

* **Windows.** The VM lends nothing from 30 min before each cut-off to
  2 h after it: 10:30–13:00 and 22:30–01:00, Paris time, every day (the
  zone follows summer time). A package CI that runs longer than 2 h still
  takes the VM back through `busy_units`.
* **No `busy_units`.** The runners' services `actions.runner.*` stay
  active between jobs: counting them counts installed runners, not running
  jobs, and with more runners than `busy_max` the gate would never open. A
  job has no unit of its own (its `Runner.Worker` lives in the service's
  cgroup). Outside the cut-offs, only a lot that unblocks the CI or fixes an
  incident runs a CI (§11 bis): the pressure and memory thresholds above
  take the VM back from the guest when such a CI loads it, and
  `cpu_weight` 10 makes the CI win every contention meanwhile. A per-job
  unit (the runner's job hooks) is for the CI's maintainers to offer; until
  then the offer names none.
* **Caps.** `cpu_max` 6 and `memory_max` 18 GiB are the study's
  off-peak margin (≈ 7 vCPU, ≈ 18 GB). Three jobs of `agent_memory_max`
  6 GiB fit. `disk_max` 40 GiB is the volume `volume-pilot.sh` makes (40 GB,
  about €3.80 a month): nothing else holds it.
* **Who enforces the caps.** In b1, the guard's caps would bound
  `ameesh-guest.slice`, where guest runners live, and there are none here.
  The offloaded jobs run in `user-<uid>.slice` of the jobs' user (#354), and
  the shell that receives them in the guest's own. `install.sh` writes the
  caps on both, from its own options, which default to 75 % of the CPUs and
  60 % of the RAM. Step 3 therefore passes the offer's values explicitly:
  `--cpu-share` is `cpu_max` divided by `nproc` (0.375 on 16 vCPU),
  `--memory-max` is `memory_max`, `--job-memory-max` is `agent_memory_max`
  and `--slots 3`. Each of the two slices gets these caps in full; the
  shell only unpacks snapshots, so the jobs' slice is the one that counts.
  The guard keeps the gate (windows and reclaim); the gate does not read
  the caps.

## Before

* The L159g pilot report qualified by the project pilot, before any host
  operation (volume included). Decision #47757 chooses b1; it does not waive
  this prerequisite (L159 study §5.1, lot #193, mesh-design 2026-10-15).
* On `main`: the guard (L159e), the gate (L159f), `install.sh` (L159b),
  and `install-pilot.sh --offload-only` (this lot).
* The dedicated UID of the jobs (#354, L159b2) merged and part of the
  checkout: bureau checks it before step 2.
* A checkout only root can change, at the reviewed commit of `main`:

  ```sh
  sudo git clone https://github.com/manaty/ameesh /root/ameesh   # or: git -C /root/ameesh pull
  cd /root/ameesh && git log -1 --oneline
  ```

* The guest's volume, mounted on `/srv/ameesh-guest` (study §4.1; a
  guest that fills it never reaches the CI's disk). `deploy/guest/volume-pilot.sh`
  does it, as in [PILOT-ameesh-agents-1.md](PILOT-ameesh-agents-1.md#before),
  with `--server <the VM>` and the VM's project (`--zone` if it is not in
  `fr-par-1`).

## Steps (bureau, as root on the VM)

1. **Read the VM.** Nothing changes:

   ```sh
   nproc; free -g; df -h / /srv/ameesh-guest
   systemctl list-units --plain --no-legend 'actions.runner.*'   # the CI's runners, left alone
   id runner                                                       # docker: expected
   ss -ltnp | awk 'NR>1 {print $4}'                                # loopback ports to deny
   ```

2. **The guard, alone, in observation.** Read the dry run first:

   ```sh
   cd /root/ameesh
   sudo deploy/guest/install-pilot.sh --dry-run --offload-only --host <vm> --lend <offer.json>
   sudo deploy/guest/install-pilot.sh --offload-only --host <vm> --lend <offer.json>
   ```

3. **The offload**, obeying the guard's gate. It comes after step 2: it
   sets `/srv/ameesh-guest` to the dedicated-UID layout of #354 (`2710`,
   shell-owned, with traversal for the jobs' group), after the pilot
   installer's `0755`:

   ```sh
   CAPS="--slots 3 --cpu-share 0.375 --memory-max 18G --memory-high 15G --job-memory-max 6G"
   sudo deploy/guest/install.sh --dry-run --gate file $CAPS --key "$(cat poste.pub)" \
     --from <workstation IP>,<mesh VM IP> --deny-port <ports of step 1>
   sudo deploy/guest/install.sh --gate file $CAPS --key "$(cat poste.pub)" \
     --from <workstation IP>,<mesh VM IP> --deny-port <ports of step 1>
   ```

   `0.375` is 6 CPUs out of 16: with another `nproc`, use 6 divided by it.
   The dry run's `caps on user-….slice` lines must show `MemoryMax=18G` and
   `CPUQuota=600%`, for the guest and for the jobs' user. Any other
   value means the offer and the caps differ: stop there.

4. **Check.** Every line `ok`:

   ```sh
   sudo deploy/guest/install-pilot.sh --check --offload-only --host <vm>
   sudo deploy/guest/install.sh --check
   ```

   From the workstation: `ameesh offload status` gives the gate (closed
   outside the windows), and `ameesh offload run -- uname -n` gives the VM's
   name inside a window.

5. **After a day of observation** (the guard's measures cover a full day
   with its two cut-offs):

   ```sh
   sudo deploy/guest/install-pilot.sh --offload-only --set-guard-mode enforce
   ```

## Rollback (study §4.3)

On the workstation, remove `AMEESH_OFFLOAD_HOST`: the agents test there
again. Then on the VM:

```sh
sudo deploy/guest/install.sh --uninstall
sudo deploy/guest/install-pilot.sh --uninstall [--purge]
```

The CI's runners and their units are never touched.
