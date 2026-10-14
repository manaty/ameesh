# Lending a Nexlink CI VM to the ameesh CI queue — pilot (lot #1256)

**Status: prepared, not activated.** No runner is registered, no host is
touched. Source of the request: bureau, 2026-10-14 05:45Z (mail #31258, lot
ameesh #1256), on the measured saturation of the ameesh queue (#824: 24
`ameesh-ci` runners, 3 hosts × 8, all busy; job wait p80 22 min for an SLO of
15). The loan is a **guest** of an existing Nexlink CI VM that no autoscaler
drives (example: `ci-3`, 4 `scaleway-ci` runners, most idle), never a new VM,
never a paid runner, and never a change to Nexlink's own runners, jobs,
workflows or registrations.

## 1. What is lent

| | |
|---|---|
| Runners | `ameesh-nxl-<host>-1…N`, `N` = 2 by default (1 to 4), registered to `manaty/ameesh`, label `ameesh-ci` (the fleet's `runs-on`) |
| Guest | user `ameesh-guest`, slice `ameesh-guest.slice`, root `/srv/ameesh-guest`, containers in **rootless Podman** |
| Host | one static CI VM of the Nexlink fleet (`scaleway-ci`), chosen and confirmed by its pilot; nothing else on it changes |
| Duration | temporary: from the activation go to the removal below; the offer can be closed at any time by the guard or by the host's pilot |
| Cost | none: the VM is already paid for and its runners stay idle most of the day |

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
      disk_max: 30GiB
      agent_memory_max: 3GiB
      reclaim:
        memory_available_min: 6GiB
        cpu_pressure_max: 20%
        memory_pressure_max: 2%
        io_pressure_max: 20%
        busy_units: ["actions.runner.*"]   # les unités CI de Nexlink
        busy_tasks: ["Runner.Worker"]      # un job en cours, pas l'écouteur idle
        busy_max: 2        # busy >= 2 : avertissement ; busy > 2 : l'invité s'arrête
        disk_free_min: 10GiB
        disk_paths: ["/", "/srv/ameesh-guest"]
      drain_seconds: 90
      calm_seconds: 600
      restart_after_seconds: 600
  ---
  ```

  The sizes are a starting offer: the preflight (§6 step 0) measures the
  host and the pilot revises them; `busy_max: 2` on four runners stops the
  guest as soon as the application runs three jobs and warns at two, so the
  loan never takes the last runner; `disk_max` is held by the guest
  directory only (no dedicated volume on this VM), so the guard's
  `disk_free_min`/`disk_paths` watch is the real protection.
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
| 10 | The disk is watched | own directory on the root filesystem (no volume on this VM), `disk_free_min`/`disk_paths` in the offer, `df` floor at prepare (15 GiB) | `df -Pk /srv/ameesh-guest`; guard state |

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
4. **Host pilot's written consent** (`orchestrateur`): the VM, that no
   autoscaler drives it, and the loan window.
5. **Activation go** (`mesh-design`), distinct from the merge fire, after
   the canon Host fiche PR is merged.
6. **Enforce before the first guest job** (R1 #1256): the guard is switched
   to `enforce` while the guest runners are still stopped; observation runs
   with the guests off, and `ci-runners.sh --enable` refuses in `observe`.
7. **Qualification first**: one real ameesh job on one guest runner, its
   container proven inside the slice, `/home/runner` untouched; only then
   the others are enabled and left in service.

## 6. Activation order (operator, on the VM; each step is reversible)

```
# 0. read-only preflight: the application's units and resources, the runner
#    version/digest, the free disk; nothing is changed
systemctl list-units --all 'actions.runner.*'
# idle listeners are not jobs: the guard's busy_tasks counts real workers, so
# the preflight must show both (four listeners online, zero or more workers)
systemctl list-units --all --plain --no-legend 'actions.runner.*' | wc -l
ps -eo user=,args= | grep '[R]unner.Worker' | wc -l
nproc; free -h; df -h /srv
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
#    guard must already cover them or --prepare refuses
sudo deploy/guest/ci-runners.sh --prepare --count 2
sudo deploy/guest/ci-runners.sh --register --token-file /root/reg.token
sudo deploy/guest/ci-runners.sh --check

# 5. enforce, guests still off: --enable refuses while the guard observes
#    (R1 #1256). The observation day of step 3 is done by now.
sudo deploy/guest/install-pilot.sh --set-guard-mode enforce

# 6. qualification: one real ameesh job on the guest runner (dispatch or a
#    small PR); during the job, prove the container is in the slice:
sudo deploy/guest/ci-runners.sh --enable 1
systemd-cgls ameesh-guest.slice | grep -E 'Runner.Worker|postgres'
docker ps                       # no guest container here (host daemon)

# 7. service
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
