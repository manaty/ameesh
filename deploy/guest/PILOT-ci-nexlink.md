# Lending a Nexlink CI VM to the ameesh CI queue — pilot (lot #1256)

**Status: prepared, not activated.** No runner is registered, no host is
touched. Source of the request: bureau, 2026-10-14 05:45Z (mail #31258, lot
ameesh #1256), on the measured saturation of the ameesh queue (#824: 24
`ameesh-ci` runners, 3 hosts × 8, all busy; job wait p80 22 min for an SLO of
15). The loan is a **guest** of an existing Nexlink CI VM that no autoscaler
drives, never a new VM, never a paid runner, and never a change to Nexlink's
own runners, jobs, workflows or registrations. The candidate is
`nexlink-ci-3` (`fr-par-1`, instance `961baf63-3cfd-48f7-9520-9436b84a1a28`,
IP `78.232.60.26`): `orchestrateur`, its pilot, ran the read-only preflight on
2026-10-14 07:1xZ and the offer and the prepare floor of §3 are qualified on
those measurements. The host pilot's written consent (gate 4) is still
missing, so nothing is activated.

## 1. What is lent

| | |
|---|---|
| Runners | `ameesh-nxl-<host>-1…N`: **1 to qualify**, a 2nd only if the post-job disk check of §6 passes (1 to 4); registered to `manaty/ameesh`, label `ameesh-ci` (the fleet's `runs-on`) |
| Guest | user `ameesh-guest`, slice `ameesh-guest.slice`, root `/srv/ameesh-guest`, containers in **rootless Podman** |
| Host | one static CI VM of the Nexlink fleet (`scaleway-ci`), chosen and confirmed by its pilot; nothing else on it changes |
| Duration | temporary: from the activation go to the removal below; the offer can be closed at any time by the guard or by the host's pilot |
| Cost | none: the VM is already paid for; the guard stops the guests as soon as the application's jobs need it (the fleet is not idle in bursts: ci-3 went 0/4 to 4/4 within a minute on 2026-10-14 06:30–06:33Z, so a guest running then would have been reclaimed at once) |

The application (Nexlink's own CI jobs) always wins: the guest runs in a
low-weight slice (`CPUWeight=10`, `IOWeight=10`) under hard caps
(`CPUQuota`, `MemoryHigh`, `MemoryMax`), and the capacity guard (L159e)
stops the guest runners as soon as the application needs the host
(`reclaim.busy_units: ["actions.runner.*"]` with
`reclaim.busy_tasks: ["Runner.Worker"]`, so a **job** counts and an idle
persistent listener does not — R1 #1256; plus PSI and memory thresholds).

## 2. Roles

| Role | Who | Does |
|---|---|---|
| Owner | `human:smichea` | root on the VM; runs the scripts (as for L159b/L159g); nothing else is asked of them |
| Host's pilot | `orchestrateur` (Nexlink) | confirms the VM, that no autoscaler drives it, and the window; may close the loan; records the consent on the lot |
| ameesh pilot | `mesh-design` | classes the lot, qualifies each gel, gives the merge and activation fires; owns the canon Host fiche PR |
| Bureau | `bureau` | distributes, mints the short-lived registration token (admin), opens/closes the removal |
| Auditor | `auditeur` | verifies the rules are applied and the evidence is first-hand |
| Author / operator | `mty-amh-impl-07` | prepares this plan, the script and its witness; runs the read-only preflight |

An agent never changes a host's units on its own: the VM-side commands are
run by the owner or by the host's pilot, with the script under review. The
GitHub-side deletion is run by the bureau (admin token), not on the VM.

## 3. Configuration, canon and code concerned

* **Canon** `manaty/home`, `processus/lots.md`:
  * §1 — the lot is **sensible** (prod / infra): `.github/**`, `deploy/**`
    are sensitive paths of ameesh (§8).
  * §2 — sensible/prod merges need **two independent readings** of another
    family than the author (DeepSeek fallback regime, decision 18855a: an
    independent DeepSeek is admitted for a sensible lot outside SQL,
    security and prod; **this one is prod**, so two opinions, of families
    different from each other and from the author) **and the pilot's fire**.
  * §3 — the author runs targeted tests only; CI runs once on the exact
    head; no polling of a CI or a verdict.
  * §5 — "done" means active: the lot stays open until the runners are
    active and verified, then until their removal.
* **Capacity guard contract** `docs/GARDE-DE-CAPACITE.md`: a VM that lends
  is a *lending host*; the offer (`policy.lend`) is declared in its **Host
  fiche** and reviewed by PR; the guard measures the application **outside**
  the guest and enforces the caps in the kernel; an unreadable, missing or
  expired state counts as `stopped`; the application always wins.
* **Existing installers** (same checkout): `deploy/guest/install.sh`
  (L159b: guest user, rootless Podman, subuid/subgid, linger) and
  `deploy/guest/install-pilot.sh` (L159g: slice, guard, gate), run with the
  new `--agent-units 'ameesh-guest-*.service'` so the guard also stops and
  restarts the CI runners.
* **New**: `deploy/guest/ameesh-guest-ci@.service` (the runner unit),
  `deploy/guest/ci-runner-gate.py` (installed as
  `/srv/ameesh-guest/bin/ci-runner-gate`: the unit's `ExecStart`, it starts
  `run.sh` only while the guard publishes a fresh `available` state out of
  `enforce`, and stops it when the state closes, disappears or goes stale —
  the GitHub runner knows nothing of the gate, R1 #1256),
  `deploy/guest/ci-runners.sh` (prepare/register/enable/check/remove) and
  `tests/test_1256_ci_runners.py`. `deploy/guest/install-pilot.sh` gains
  `--agent-units PATTERN` (default unchanged; the pattern must stay in the
  guest namespace, R1 #1256). The guard's job signal gains
  `reclaim.busy_tasks` (`src/ameesh/guard.py`, `src/ameesh/capacity_guard.py`,
  contract `docs/GARDE-DE-CAPACITE.md`): without it, `busy_units` counts four
  idle persistent listeners as four jobs (R1 #1256).
* **Host fiche** to merge in `manaty/home` (`hotes/ci-3.md`) — draft:

  ```yaml
  ---
  type: Host
  title: ci-3
  description: "VM CI Nexlink (flotte scaleway-ci), prêteuse : quelques runners ameesh en invités, temporairement (lot #1256)."
  responsible: human:smichea
  policy:
    max_agents: 0          # aucun agent ameesh placé ici
    lend:                  # l'offre, revue par PR ; le garde la lit sur l'hôte
      cpu_max: 2
      cpu_weight: 10
      memory_max: 6GiB
      disk_max: 4GiB         # mesuré : 8.95 GiB libres ; déclaré, le seuil du garde protège
      agent_memory_max: 3GiB
      reclaim:
        memory_available_min: 6GiB
        cpu_pressure_max: 20%
        memory_pressure_max: 2%
        io_pressure_max: 20%
        busy_units: ["actions.runner.*"]   # les unités CI de Nexlink
        busy_tasks: ["Runner.Worker"]      # un job en cours, pas l'écouteur idle
        busy_max: 2        # busy >= 2 : avertissement ; busy > 2 : l'invité s'arrête
        disk_free_min: 5GiB  # réserve de l'hôte : l'invité s'arrête sous 5 GiB libres
        disk_paths: ["/", "/srv/ameesh-guest"]
      drain_seconds: 90
      calm_seconds: 600
      restart_after_seconds: 600
  ---
  ```

  The sizes are qualified on measurements, and the pilot revises them if the
  host changes; `busy_max: 2` on four runners stops the guest as soon as the
  application runs three jobs and warns at two, so the loan never takes the
  last runner; `disk_max` is held by the guest directory only (no dedicated
  volume on this VM), so the guard's `disk_free_min`/`disk_paths` watch is
  the real protection.

  **Measurements and qualified values** (read-only SSH preflight by the
  host's pilot `orchestrateur`, 2026-10-14 07:1xZ, nothing modified):

  * `nproc` 8; MemTotal 32090 MiB, MemAvailable 29621 MiB, no swap: `cpu_max:
    2` (a quarter of the host) with `cpu_weight: 10`; PSI cpu avg10 0 /
    avg60 0.06 / avg300 0.75, memory 0, io some avg10 0.36 — all far under
    the 20 % / 2 % / 20 % thresholds at the reading (the guard decides on
    avg60).
  * `/` free 9614548992 B (8.95 GiB): `disk_max: 4GiB` (at that free space
    the contract's `disk_free − 2 GiB` = 6.95 GiB is above it, so the
    declared cap holds; as the disk fills, `disk_free − 2 GiB` takes over)
    and `disk_free_min: 5GiB` — the reserve the host keeps; the guard stops
    the guest at `disk_low` under it, so the guest may use ≈ 3.95 GiB.
  * prepare/register floor: `--disk-floor-mib 6144` = the 5 GiB guard
    threshold + 1 GiB install margin (the pinned asset is 227660598 B
    compressed; the extraction is budgeted at ≤ 1 GiB per runner and is
    measured at qualification). The 15 GiB default would refuse here.
  * four persistent `actions.runner.manaty-nexlink.nexlink-ci-3-[1-4]` units
    and one real `Runner.Worker` at the reading (listeners and jobs counted
    apart): `busy_max: 2` warns at two jobs and stops the guest above two, so
    the application keeps two of its four runners.
  * `memory_max: 6GiB` / `agent_memory_max: 3GiB` /
    `memory_available_min: 6GiB` on 29.6 GiB available, no swap.

  **Autoscaler.** Repository side: the fleet is addressed by the static label
  `[self-hosted, linux, scaleway-ci]`, the registrations are named and
  persistent (same ids across readings), and nothing in `manaty/nexlink`
  mentions `autoscal|actions-runner-controller|scale-set|gha-runner|runner-scale-set`
  outside an unrelated desktop runbook. Host side: no unit matching
  `gha-runner-scale-set*`, `arc-*` or `*autoscal*` (and `ci-runners.sh
  --prepare` refuses if one appears), and the filtered process/cron listing
  showed the four runners and Docker only. An autoscaler external to the host
  cannot be disproven from the host itself: the provider-side fact (a
  standalone Scaleway Instance, not a scaling group) and the absence of any
  controller are part of the host pilot's written consent (gate 4).
* **Nexlink rules** `AGENTS.md` (a–g, source orchestrateur 2026-10-13):
  rule (c) "no hosted runners, `runs-on` pinned to the fleet" is untouched —
  the loan adds *ameesh* runners on a Nexlink VM, it changes no Nexlink
  workflow and no `runs-on`. The host's pilot records the consent.

## 4. Isolation: what is proven, and how

| # | Property | Mechanism | Evidence (`ci-runners.sh --check`, API) |
|---|---|---|---|
| 1 | The guest is not root-equivalent | user `ameesh-guest`, locked, no sudo, no `docker` group (`install.sh` refuses it) | `id ameesh-guest`, `id -nG` |
| 2 | The containers are the guest's own | `docker` shim → **rootless Podman** (`--cgroup-manager=cgroupfs`), never `/run/docker.sock` | `podman info` `rootless=true`; host `docker ps` never shows a guest container |
| 3 | The containers are capped with the guest | `Delegate=yes` on the unit; containers are cgroups **under** the runner, inside `ameesh-guest.slice` | `systemd-cgls ameesh-guest.slice` during a job |
| 4 | The guest cannot starve the application | slice `CPUWeight=10`/`IOWeight=10`, `CPUQuota`, `MemoryHigh`, `MemoryMax`; `OOMScoreAdjust=500` | `systemctl show ameesh-guest.slice -p …` |
| 5 | The application's jobs win | guard `reclaim.busy_units: actions.runner.*` **with `busy_tasks: ["Runner.Worker"]`** (a job counts, an idle listener does not), `busy_max`, PSI and memory thresholds; levels 3/4 stop the guest runners; the unit's `ci-runner-gate` stops `run.sh` when the gate closes, disappears or goes stale | `ameesh guard check --json`, `guard.stopped` in the state; witnesses `tests/test_l159e_guard.py` (idle listener vs running job, unreadable cgroup counts) and `tests/test_1256_ci_runners.py` (the wrapper stops on a closed/observed/absent gate and restarts on `available`) |
| 6 | The application's runners are never touched | the unit matches `ameesh-guest-ci@*` only; no command of the script ever names `actions.runner.*`; the check compares the application's unit count before/after | witness `tests/test_1256_ci_runners.py`; `systemctl list-units 'actions.runner.*'` unchanged |
| 7 | The workspaces are distinct | `/srv/ameesh-guest/ci/rN` (guest) vs `/home/runner/rN/_work/nexlink` (application); `RequiresMountsFor` keeps the guest on its own root | `ci-runners.sh --check`; `ls -ld` |
| 8 | The tokens are distinct | registration minted for `manaty/ameesh` only, read from a root-only file, `.credentials` 0600 `ameesh-guest`; the script never sees a Nexlink token | `.runner` `serverUrl`; `gh api repos/manaty/ameesh/actions/runners` |
| 9 | The workspaces are separate | each runner has its own `_work`; the application's sweep is nominative (its `RUNNER_NAME`) and never looks at `ci/rN` | its `self-hosted-sweep.sh` (Nexlink repo) |
| 10 | The disk is watched | own directory on the root filesystem (no volume on this VM), `disk_free_min`/`disk_paths` in the offer, `df` floor at prepare (`--disk-floor-mib`; 15 GiB default, 6 GiB qualified on ci-3, §3) | `df -Pk /srv/ameesh-guest`; `--check`; guard state |

Residual limits, stated rather than hidden: the guest shares the kernel (a
rootless container is not a VM); a job killed at level 4 is lost, not
retried (GitHub marks it failed); `disk_max` has no volume to hold it here
(the guard's free-space threshold is the protection); `config.sh` receives
the registration token in its argument vector for the seconds it runs (the
token is short-lived, single-command, minted for the ameesh repository
only); the job signal is the `Runner.Worker` marker read in each matched
unit's cgroup (R1 #1256) — it follows the runner's implementation, and a
unit whose cgroup cannot be read counts as busy (the application wins); the
actions-runner tarball is verified by its digest at every extraction, the
cache included, so a modified cache cannot be unpacked as root; the gate
wrapper follows the guard's own state TTL (90 s, rewritten every 30 s) and a
state it cannot read is closed, so a guard that dies or hangs stops the guest
runners instead of leaving them running.

## 5. Gates (nothing starts before all of them)

1. **Two independent readings** of the PR (families other than the author's
   DeepSeek, and other than each other), one of them covering the
   root-script safety.
2. **CI green on the exact head** (required `tests`), all expected parts
   executed, none cancelled — per canon §3, and after the #1211 wave has
   relieved the queue.
3. **Pilot's fire** (`mesh-design`) for the merge, on the frozen head.
4. **Host pilot's written consent** (`orchestrateur`): the VM (identity of
   §3), that no autoscaler drives it (§3, "Autoscaler": the provider-side
   attestation is the pilot's), and the loan window.
5. **Activation go** (`mesh-design`), distinct from the merge fire, after
   the canon Host fiche PR is merged.
6. **Enforce before the first guest job** (R1 #1256): the guard is switched
   to `enforce` while the guest runners are still stopped; observation runs
   with the guests off, and `ci-runners.sh --enable` refuses in `observe`.
7. **Qualification first**: one real ameesh job on one guest runner, its
   container proven inside the slice, `/home/runner` untouched, and the
   post-job disk check of §6 (free ≥ 6 GiB, no `disk_low` episode); only then
   the others are enabled and left in service.

## 6. Activation order (operator, on the VM; each step is reversible)

```
# 0. read-only preflight: the application's units and resources, the runner
#    version/digest, the free disk; nothing is changed. Measured on ci-3 on
#    2026-10-14 07:1xZ (8 CPUs, 32 GiB RAM, 8.95 GiB free on /): the offer and
#    the floor of §3 are qualified on it.
systemctl list-units --all 'actions.runner.*'
# idle listeners are not jobs: the guard's busy_tasks counts real workers, so
# the preflight must show both (four listeners online, zero or more workers)
systemctl list-units --all --plain --no-legend 'actions.runner.*' | wc -l
ps -eo user=,args= | grep '[R]unner.Worker' | wc -l
nproc; free -h; df -h / /srv
sudo deploy/guest/ci-runners.sh --dry-run --check

# 1. canon PR merged (Host policy.lend, §3) — ameesh pilot

# 2. the guest and its containers (L159b), if not already prepared
sudo deploy/guest/install.sh --dry-run --key <owner pubkey> --from <IP>
sudo deploy/guest/install.sh --key <owner pubkey> --from <IP>
sudo deploy/guest/install.sh --check

# 3. the capped slice, the guard and the gate (L159g), in observation, with
#    the offer of the Host fiche and the widened unit glob. Observation runs
#    a day with the guests OFF: an observing guard stops nothing.
sudo deploy/guest/install-pilot.sh --dry-run --lend offer.json \
  --agent-units 'ameesh-guest-*.service' --allow-rootfs
sudo deploy/guest/install-pilot.sh --lend offer.json \
  --agent-units 'ameesh-guest-*.service' --allow-rootfs

# 4. the runners (this lot), prepared and registered but NOT started; the
#    guard must already cover them or --prepare refuses. ci-3 holds 8.95 GiB
#    free: qualify ONE runner with the floor §3 justifies (5 GiB guard
#    threshold + 1 GiB install margin), not the 15 GiB default
sudo deploy/guest/ci-runners.sh --prepare --count 1 --disk-floor-mib 6144
sudo deploy/guest/ci-runners.sh --register --token-file /root/reg.token
sudo deploy/guest/ci-runners.sh --check
du -sh /srv/ameesh-guest/ci; df -Pk /    # record the real install cost

# 5. enforce, guests still off: --enable refuses while the guard observes
#    (R1 #1256). The observation day of step 3 is done by now.
sudo deploy/guest/install-pilot.sh --set-guard-mode enforce

# 6. qualification: one real ameesh job on the guest runner (dispatch or a
#    small PR); during the job, prove the container is in the slice:
sudo deploy/guest/ci-runners.sh --enable 1
systemd-cgls ameesh-guest.slice | grep -E 'Runner.Worker|postgres'
docker ps                       # no guest container here (host daemon)

# 6bis. the disk decides the 2nd runner, not the plan: after the job, with the
#    guest idle, free must be back >= 6 GiB and the guard must never have
#    published `disk_low`. Otherwise the loan stays at one runner (or closes).
df -Pk /; /opt/ameesh/venv/bin/ameesh guard check --json | grep -E 'disk|level'

# 7. service: only after 6bis passes; the recorded count (1) is adopted, r1 is
#    kept, r2 is prepared, registered and enabled
sudo deploy/guest/ci-runners.sh --prepare --count 2 --disk-floor-mib 6144
sudo deploy/guest/ci-runners.sh --register --token-file /root/reg.token
sudo deploy/guest/ci-runners.sh --enable 2
```

## 7. Removal — only after terminal jobs

The order matters; nothing else is touched.

1. **Pause**: `ci-runners.sh --disable` (refuses while a job runs; or
   `--wait-terminal 600` on the operator's explicit request).
2. **Terminal check** (first-hand, not a guess): every guest runner
   `busy=false` in the GitHub API, no `Runner.Worker` of the guest user for
   `ci/rN`, and no queued run that names them. The host-side removal refuses
   otherwise.
3. **Forge side** (bureau, admin token), from a workstation:
   ```
   gh api "repos/manaty/ameesh/actions/runners?per_page=100" --paginate \
     -q '.runners[] | select(.name|startswith("ameesh-nxl-<host>-")) | "\(.id) \(.name)"'
   gh api -X DELETE "repos/manaty/ameesh/actions/runners/<id>"    # once per line
   ```
   Verify: the ameesh runner list is back to its 24 pre-loan runners, all
   online; **no Nexlink runner was unregistered** (that repository's list is
   untouched).
4. **Host side**: `ci-runners.sh --remove` (deregisters the host side,
   stops and removes the units, the shim, the gate wrapper and the runner
   directories it recorded in `/etc/ameesh/ci-runners.state`;
   `--keep-workspaces` keeps the logs). It never touches `actions.runner.*`,
   `/home/runner`, the application's Docker daemon, or the other ameesh
   guest files.
5. **Canon**: the pilot merges the Host fiche PR that removes the loan (or
   records its end); the lot notes the final API state.

Never, at any point: cancel or relaunch a Nexlink run; `docker system
prune`; delete a shared cache; unregister a Nexlink runner; move
`/home/runner`; edit a Nexlink workflow or `runs-on`.

## 8. What this lot does *not* do

No runner is registered, no host is modified, no token is minted, no CI run
is relaunched. The script and its witness are ready for review; the
activation waits for the gates of §5, and the whole loan stays reversible at
every step through the state file written by `ci-runners.sh --prepare`.
