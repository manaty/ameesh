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

**Option A of the R2 B1 arbitration** (pilot `mesh-design`, 2026-10-15 01:57)
is implemented here, in this lot: the guest runners are **single-use (JIT)**,
they carry a label **outside the required checks** (`ameesh-lent`, never the
fleet's `ameesh-ci`), and the only jobs that may be routed to them are
**admissible** ones — never the required check nor anything it needs or reads
(§3, "Routing"). Its benefit is deliberately limited and stated in §3; no
risk acceptance is asked of the owner.

## 1. What is lent

| | |
|---|---|
| Runners | `ameesh-nxl-<host>-1…N`: **1 to qualify**, a 2nd only if the post-job disk check of §6 passes (1 to 4). **Single-use**: each one is created by one just-in-time configuration, runs **one** job, and is removed by GitHub (option A, §3). Label `ameesh-lent`, **never** the fleet's `ameesh-ci`, so no job of the required check can land here |
| Guest | user `ameesh-guest`, slice `ameesh-guest.slice`, root `/srv/ameesh-guest`, containers in **rootless Podman**; the privileged tools root installs (`docker` shim, gate wrapper) live **outside** that root, in `/usr/local/lib/ameesh-guest-ci` (§4, row 11) |
| Host | one static CI VM of the Nexlink fleet (`scaleway-ci`), chosen and confirmed by its pilot; nothing else on it changes |
| Duration | temporary: from the activation go to the removal below; the offer can be closed at any time by the guard or by the host's pilot. The loan is **armed job by job**: a slot runs only once a fresh single-use configuration has been deposited (§3, §6) |
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
| ameesh pilot | `mesh-design` | classes the lot, qualifies each gel, gives the merge and activation fires; owns the canon Host fiche PR; arbitrated option A (15/10 01:57) |
| Bureau | `bureau` | **mints the single-use JIT configurations** off the host (`POST /repos/manaty/ameesh/actions/runners/generate-jitconfig`, admin token) and deposits them (§3, §6); opens/closes the removal; also runs the forge-side deletion. No registration token is ever minted for this host |
| Auditor | `auditeur` | verifies the rules are applied and the evidence is first-hand |
| Author / operator | `mty-amh-impl-07` | prepares this plan, the script and its witness; runs the read-only preflight; the R1-delta fix keeping the privileged tools out of the guest's root comes from `ameesh-cx3` (binôme canon §2, 2026-10-14) |

An agent never changes a host's units on its own: the VM-side commands are
run by the owner or by the host's pilot, with the script under review. The
GitHub-side deletion is run by the bureau (admin token), not on the VM. The
JIT configurations are minted by the bureau **off** the host: nothing that
can create a registration is ever stored on a machine where an application
job is root-equivalent.

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
* **New**: `deploy/guest/ameesh-guest-ci@.service` (the runner unit,
  single-use: `Restart=no`, one start = at most one job),
  `deploy/guest/ci-runner-gate.py` (installed as
  `/usr/local/lib/ameesh-guest-ci/ci-runner-gate`: the unit's `ExecStart`,
  it waits for the guard to publish a fresh `available` state out of
  `enforce` **without consuming anything**, then consumes exactly one
  deposited configuration, starts `run.sh --jitconfig <configuration>`,
  stops it if the state closes, disappears or goes stale, and removes the
  local `.runner`/`.credentials`/`.credentials_rsaparams` before and after
  the job),
  `deploy/guest/ci-runners.sh` (prepare/deposit/enable/disable/check/remove;
  no registration token any more: `--deposit` validates one single-use
  configuration and hands it to the guest's spool; root writes only the
  tools outside the guest's root, and every mutation under it runs as the
  guest — R1 delta B4, binôme) and `tests/test_1256_ci_runners.py`.
  `deploy/guest/install-pilot.sh` gains `--agent-units PATTERN` (default
  unchanged; the pattern must stay in the guest namespace, R1 #1256). The
  guard's job signal gains `reclaim.busy_tasks` (`src/ameesh/guard.py`,
  `src/ameesh/capacity_guard.py`, contract `docs/GARDE-DE-CAPACITE.md`):
  without it, `busy_units` counts four idle persistent listeners as four
  jobs (R1 #1256).

### Single-use registration (option A)

No registration token reaches the lending host. For each job slot the
**bureau**, from a workstation with an admin token, creates one just-in-time
runner:

```
gh api -X POST repos/manaty/ameesh/actions/runners/generate-jitconfig \
  -f name="ameesh-nxl-<host>-1" -F runner_group_id=1 \
  -f 'labels[]=self-hosted' -f 'labels[]=linux' -f 'labels[]=ameesh-lent' \
  -f work_folder=_work -q .encoded_jit_config > /root/jit-ci-3-1.b64
```

The returned `encoded_jit_config` is the base64 of a JSON object mapping
runner file names to base64 contents (actions/runner v2.338.0, `Runner.cs`:
`run.sh --jitconfig` writes them into the runner's root before the single
job). It is deposited **on** the host, never minted there:

```
sudo deploy/guest/ci-runners.sh --disk-floor-mib 6144 --deposit /root/jit-ci-3-1.b64
```

`--deposit` reads the source as root, validates it (strict base64 of a JSON
object; safe file names — never a path; base64 values; `.runner.gitHubUrl`
normalized equal to `https://github.com/manaty/ameesh`; **`.runner` mandatory,
and `ephemeral: true` inside it**) and publishes it atomically and
exclusively in `/srv/ameesh-guest/ci/jit/rN/` **as the guest** (root writes
nothing under `$ROOT`, R1 delta B4). The guest copy goes to a private
temporary of the same directory; its digest is compared with the validated
source **before** the exclusive link publishes it (the link fails if the
target already exists, so a concurrent or repeated deposit is refused, and no
partial file is ever visible to the wrapper). A source that changed between
the root read and the guest copy is therefore refused **before** anything is
published, never consumed then rejected (R1/R2 Codex B2, #1256).

`ephemeral: true` is not decoration. The JIT blob only writes the files
(`Runner.cs`, `RunJitConfig`); the one-shot path reads `settings.Ephemeral`,
and a configuration without it leaves a **persistent** runner registered on
the lending host — precisely what option A removes (R1 Codex B1, #1256). Both
sides refuse it: the deposit, and the wrapper that consumes the configuration
(`--spool`, `validate_config`). The slot follows the runner name in `.runner`
(`ameesh-nxl-<host>-<slot>`, `--runner N` when the configuration carries no
name), and the deposit is named by the sha256 of the canonical text (the same
configuration cannot be deposited twice). One file = one job: GitHub removes
the runner once the job ends, and the wrapper deletes the local configuration
files the runner wrote from the blob.

Consequences, accepted and bounded:

* a compromised application job (root-equivalent through Docker, §4) can
  read a deposited configuration and take **that one job** — a job of the
  borrowed label only, which is never a required check and never a job the
  required path needs or reads. It cannot mint another one: no token, no
  durable credential, and the registration it stole is gone after the job;
* `ci-runners.sh --check` refuses a `.runner`/`.credentials` left on disk
  while no unit runs (a durable credential of an older, non-JIT install, or
  a killed run): the wrapper removes them before and after each job;
* **capacity is not continuous**: nothing re-arms behind the operator
  (`Restart=no`, `systemctl start`, and `--enable N` refuses without a
  deposited configuration). A job routed to `ameesh-lent` waits until the
  bureau deposits a fresh configuration and the operator arms the slot.
  The loan is therefore armed in batches, and the routing variable is set
  only while it is armed.

### Routing: which jobs may target the borrowed label

The label separation is what protects the merge gate; it is not a
convenience:

* `ameesh-ci` is the fleet's label, named by the 13 self-hosted jobs of
  `tests.yml` (9), `carte.yml` (3) and `ui.yml` (1). The borrowed runners
  never carry it, so **none of those jobs can be scheduled on a lending
  host**.
* The only **required status check** is `tests` (branch protection of
  `main`: `contexts: ["tests"]`, GitHub Actions). No job of its path may
  ever target `ameesh-lent`: not `tests` itself, and none of its `needs`
  (`plan`, `version`, `psycopg`, `psql`, `platform-macos`, `doc-readers`).
  A required part that ran on a host of another project could be forged
  green without running its tests.
* A job routed to the borrowed label must also: hold **no write
  permission** (in particular no `checks: write`, which can publish a check
  run in the `tests` context), and feed **nothing the required path reads**.
  That excludes `plan` (it decides which parts run) and `carte` (its map is
  cached and read by `plan`: a poisoned map would shrink the required
  suite). It is witnessed, not trusted: `WorkflowRoutingTest` in
  `tests/test_1256_ci_runners.py` recomputes the required closure from the
  workflow files and fails if any routed job enters it, if a routed job or
  workflow holds a write permission, if anything needs a routed job, or if
  a `runs-on` names the borrowed label as a literal.
* **The Actions cache is a channel too, and `permissions` does not close
  it** (R1 Codex B3, confirmed by the Claude read). Every job receives the
  run's runtime token, and with it the cache service; `plan` restores the
  measured map (`carte-*`, most recent wins) and the psycopg parts restore
  `.venv-ci`, so a cache entry written from the borrowed host would reach
  the required checks — a forged map shrinks the required suite, a forged
  venv can turn a part green. The remedy is `cache-mode`, which the **cache
  service** enforces on the run's token (an environment variable set by the
  job itself would prove nothing against a root-equivalent host):
  `cache-mode: none` on `bilan` (it reads no cache) and `cache-mode: read`
  on `ui` (setup-node keeps its npm restore, the save is denied). A routed
  job in any write mode is a defect; the witness fails on one. **At
  qualification, a real lent run must show the effective scope** (the cache
  save refused, `§5`), not just the declaration.
* **The run's artifacts are a second channel of the same token** (R1 Claude
  B4, #1256). `bilan` runs after `plan`, `psycopg` and `psql`, and the
  runtime token serves the artifact API too: a rooted application job could
  delete the `plan` artifact and publish a forged one, then a *re-run of
  failed jobs* of the parts would execute the forged plan — a green without
  tests. The remedy does not depend on the artifact service refusing
  deletion: the `plan` job publishes **the sha256 of the file it uploads as
  a job output** (`plan_sha256`), which no other job can rewrite, and
  `psycopg` and `psql` compare it (`sha256sum -c`) before their part runs —
  a mismatch fails the required job. The witness
  (`test_the_required_path_verifies_the_plan_artifact`) recomputes the
  required closure and fails if a required consumer downloads `plan`
  without that check. Qualification must also record whether the runtime
  token may delete an artifact at all (`§5.7`); if it may not, this is
  defence in depth rather than the only barrier.
* Today's admissible list is therefore: **`bilan`** (tests.yml,
  `continue-on-error: true`, not in `tests.needs`, consumes the `flux-*`
  artifacts, feeds only the two-week tally of the map) and **`ui`**
  (ui.yml, not a required check, read-only, feeds nothing). Anything else
  needs this section amended, and a fresh review of what it can reach.
* The routing switch is a repository variable,
  `vars.AMEESH_LENT_RUNS_ON`, read by those two jobs only:

  ```yaml
  runs-on: ${{ (…) && fromJSON(vars.AMEESH_LENT_RUNS_ON || '["self-hosted", "linux", "ameesh-ci"]') || 'ubuntu-latest' }}
  ```

  Set it to `["self-hosted","linux","ameesh-lent"]` while the loan is armed
  (configurations deposited, slots armed) and **clear it** before the
  removal. Unset or empty, both jobs stay on the fleet. GitHub has no label
  fallback: a job sent to an empty loan waits for a runner, so the variable
  must not be left set over a closed loan.

**The benefit is limited, and this is the trade the pilot chose.** Under
option A the loan cannot absorb any part of the required suite: it takes
`bilan` (5 min) and `ui` (10 min) per run at most, so the 24 fleet runners
keep the whole required load and the queue wait of #824 is only partly
relieved. Option B (persistent runners on the fleet's label, the same load
as the fleet) was not chosen: it would have needed the owner's written
acceptance that an application job on the VM — root-equivalent through
Docker — can pass itself off as an ameesh runner and publish a required
check.

* **Host fiche** to merge in `manaty/home` (`hotes/ci-3.md`) — draft:

  ```yaml
  ---
  type: Host
  title: ci-3
  description: "VM CI Nexlink (flotte scaleway-ci), prêteuse : quelques runners ameesh en invités, à usage unique, temporairement (lot #1256)."
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
  * prepare floor: `--disk-floor-mib 6144` = the 5 GiB guard threshold + 1
    GiB install margin (the pinned asset is 227660598 B ≈ 217 MiB
    compressed; the extraction is budgeted at ≤ 1 GiB per runner and is
    measured at qualification; the digest is verified on a root-private
    snapshot of the archive — ≈ one extra asset copy under `/var/tmp`,
    removed on exit, R1 delta B4). The 15 GiB default would refuse here.
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
  workflow and no `runs-on`; the routing variable and the two routed jobs
  live in `manaty/ameesh` only. The host's pilot records the consent.

## 4. Isolation: what is proven, and how

| # | Property | Mechanism | Evidence (`ci-runners.sh --check`, API) |
|---|---|---|---|
| 1 | The guest is not root-equivalent | user `ameesh-guest`, locked, no sudo, no `docker` group (`install.sh` refuses it) | `id ameesh-guest`, `id -nG` |
| 2 | The containers are the guest's own | `docker` shim → **rootless Podman** (`--cgroup-manager=cgroupfs`), never `/run/docker.sock` | `podman info` `rootless=true`; host `docker ps` never shows a guest container |
| 3 | The containers are capped with the guest | `Delegate=yes` on the unit; containers are cgroups **under** the runner, inside `ameesh-guest.slice` | `systemd-cgls ameesh-guest.slice` during a job |
| 4 | The guest cannot starve the application | slice `CPUWeight=10`/`IOWeight=10`, `CPUQuota`, `MemoryHigh`, `MemoryMax`; `OOMScoreAdjust=500` | `systemctl show ameesh-guest.slice -p …` |
| 5 | The application's jobs win | guard `reclaim.busy_units: actions.runner.*` **with `busy_tasks: ["Runner.Worker"]`** (a job counts, an idle listener does not), `busy_max`, PSI and memory thresholds; levels 3/4 stop the guest runners; the unit's `ci-runner-gate` waits for the gate, consumes nothing while it is closed, and stops `run.sh` when the state closes, disappears or goes stale | `ameesh guard check --json`, `guard.stopped` in the state; witnesses `tests/test_l159e_guard.py` (idle listener vs running job, unreadable cgroup **and unreadable task cmdline** count) and `tests/test_1256_ci_runners.py` (a closed/observed/absent gate consumes nothing and starts nothing; a gate that closes or expires mid-job stops it; `--check` reads an active wrapper waiting with its child stopped as correct, and refuses a runner process alive after the wrapper's stop delay) |
| 6 | The application's runners are never touched | the unit matches `ameesh-guest-ci@*` only; no command of the script ever names `actions.runner.*`; the check compares the application's unit count before/after | witness `tests/test_1256_ci_runners.py`; `systemctl list-units 'actions.runner.*'` unchanged |
| 7 | The workspaces are distinct | `/srv/ameesh-guest/ci/rN` (guest) vs `/home/runner/rN/_work/nexlink` (application); `RequiresMountsFor` keeps the guest on its own root | `ci-runners.sh --check`; `ls -ld` |
| 8 | The credentials are distinct **and single-use** | one JIT configuration per job, minted for `manaty/ameesh` only and deposited by an operator; no registration token ever reaches the host; the runner is removed by GitHub after its one job, and the wrapper deletes `.runner`/`.credentials`/`.credentials_rsaparams` before and after it | the deposit refuses a configuration whose `.runner` names another repository, an unsafe file name or a duplicate; `--check` reports the deposited configurations and refuses a local configuration left while no unit runs; `gh api repos/manaty/ameesh/actions/runners` shows no persistent `ameesh-nxl-*` entry |
| 9 | The workspaces are separate | each runner has its own `_work`; the application's sweep is nominative (its `RUNNER_NAME`) and never looks at `ci/rN` | its `self-hosted-sweep.sh` (Nexlink repo) |
| 10 | The disk is watched | own directory on the root filesystem (no volume on this VM), `disk_free_min`/`disk_paths` in the offer, `df` floor at prepare (`--disk-floor-mib`; 15 GiB default, 6 GiB qualified on ci-3, §3) | `df -Pk /srv/ameesh-guest`; `--check`; guard state |
| 11 | The privileged tools are out of the guest's reach | `docker` shim and gate wrapper installed in `/usr/local/lib/ameesh-guest-ci` (root:root); the whole chain is checked (no symlink, root-owned, not group/other-writable) before every `--prepare` and `--remove`; every creation, publication and removal under `/srv/ameesh-guest` runs as `ameesh-guest` | `ls -ld /usr/local/lib/ameesh-guest-ci /usr/local/lib/ameesh-guest-ci/docker`; witness `tests/test_1256_ci_runners.py` (a symlinked tools path, ancestor or tool file is refused; no root mutation names a path under the guest tree; removal goes through `runuser`) |
| 12 | The application's runner secrets stay out of the guest's read | probed as `ameesh-guest`, never as root: `/home/runner`, then every `.runner`, `.credentials`, `.credentials_rsaparams` and `_work` under it (depth 5) must fail `test -r` | `ci-runners.sh --check` (`runuser -u ameesh-guest -- test -r …`; any readable path is a KO, with the path named); witness `tests/test_1256_ci_runners.py` (unreadable tree accepted, readable `.credentials` refused); qualification §5.7 (R2 #1256 B2) |
| 13 | A required check can never run here | the guest's label is `ameesh-lent`, distinct from the fleet's `ameesh-ci`; the two routed jobs (`bilan`, `ui`) are outside `tests` and its `needs`, hold no write permission, **declare a non-writing `cache-mode`** and feed nothing the required path reads | witness `WorkflowRoutingTest` (`tests/test_1256_ci_runners.py`): the required closure is recomputed from `tests.yml`, and any routed job inside it, any write permission on a routed job, any routed job without `cache-mode: read`/`none`, or any literal `ameesh-lent` in a `runs-on` fails the test; `--check` prints the label and the deposit count; qualification §5.7 shows the effective cache scope (R1 #1256 B3) |
| 14 | The borrowed job is bounded to one non-required job | the JIT configuration is consumed (unlinked) by the wrapper before `run.sh` starts, is never reused, and the runner it created is removed by GitHub after that job; the window in which an application job could steal it is one non-required job, with no reusable credential | witness `tests/test_1256_ci_runners.py` (a configuration is handed to `run.sh --jitconfig` exactly once, a second start with an empty spool starts nothing, the local files do not outlive the job); `--check` refuses leftovers |

Residual limits, stated rather than hidden: the guest shares the kernel (a
rootless container is not a VM); **the isolation is not symmetric**: on this
VM an application job is root-equivalent (the fleet's jobs run Docker and
`docker run`), so it can read a deposited single-use configuration — option
A bounds what that theft is worth (one job of the borrowed label, never a
required check, no durable credential), it does not make the guest
unreadable; the JIT configuration travels in `run.sh`'s argument vector for
the length of the job (`/proc` is readable by the same UID; the runner
offers no file or stdin variant) — it is single-use and non-required, and it
is unlinked from the spool before the runner starts; the guest's label is
fixed in `ci-runners.sh` (`LABEL=ameesh-lent`) and the deposit refuses a
configuration naming another repository, but the labels of a JIT
configuration are set server-side at generation: the bureau sets them
(`self-hosted`, `linux`, `ameesh-lent`) and the *plan* is the reference —
a mis-generated label would show as a runner that never receives a job, not
as a required job on the loan, since the fleet's jobs ask for `ameesh-ci`; a
job killed at level 4 is lost, not retried (GitHub marks it failed), and a
configuration consumed for a job that then loses the gate is spent;
`disk_max` has no volume to hold it here (the guard's free-space threshold
is the protection); the job signal is the `Runner.Worker` marker read in
each matched unit's cgroup (R1 #1256) — it follows the runner's
implementation, and a unit whose cgroup cannot be read counts as busy (the
application wins); the actions-runner tarball is copied to a root-private
snapshot (0700/0600), its pinned digest is verified there, and the **guest**
extracts that snapshot — the cache and the destination, both under the
guest's `$ROOT`, are never unpacked by root (R1 delta B4, #1256); the two
files root installs (the `docker` shim and the gate wrapper) are written
outside `$ROOT`, in `/usr/local/lib/ameesh-guest-ci`, after the chain's
ownership, mode and symlink-free state have been checked (`root_tools_safe`,
before `--prepare` and `--remove`), so a guest rename after that check
cannot redirect a privileged write or removal — the guest owns `$ROOT`, not
this chain (R1 delta B4, binôme impl-07/cx3, 2026-10-14); the gate wrapper
follows the guard's own state TTL (90 s, rewritten every 30 s) and a state
it cannot read is closed, so a guard that dies or hangs stops the guest
runners instead of leaving them running.

## 5. Gates (nothing starts before all of them)

1. **Two independent readings** of the PR (families other than the author's
   DeepSeek, and other than each other), one of them covering the
   root-script safety and one the label separation of §3 (the routing rule
   and the workflows are part of the merge gate: the canon classes anything
   that can turn a required check green without its tests as security).
2. **CI green on the exact head** (required `tests`), all expected parts
   executed, none cancelled — per canon §3, and after the #1211 wave has
   relieved the queue.
3. **Pilot's fire** (`mesh-design`) for the merge, on the frozen head.
4. **Host pilot's written consent** (`orchestrateur`): the VM (identity of
   §3), that no autoscaler drives it (§3, "Autoscaler": the provider-side
   attestation is the pilot's), and the loan window. The reverse-direction
   decision of §4 is **taken**: option A (single-use JIT, label outside the
   required checks, admissible routing) is what is activated; no owner risk
   acceptance is asked for. The consent must name that the host runs
   single-use guests under `ameesh-lent`.
5. **Activation go** (`mesh-design`), distinct from the merge fire, after
   the canon Host fiche PR is merged.
6. **Enforce before the first guest job** (R1 #1256): the guard is switched
   to `enforce` while the guest runners are still stopped; observation runs
   with the guests off, and `ci-runners.sh --enable` refuses in `observe`.
7. **Qualification first**: one real ameesh job **of the routed kind** on one
   guest runner (a `bilan` of a dispatched run, `AMEESH_LENT_RUNS_ON` set
   for the window), its container proven inside the slice, `/home/runner`
   untouched, the guest proven unable to read `/home/runner`, the
   application runners' `.runner`/`.credentials`/`.credentials_rsaparams`
   and their `_work` (`ci-runners.sh --check`; a readable path is a KO, R2
   #1256 B2), the runner gone from `gh api
   repos/manaty/ameesh/actions/runners` after the job, **the effective cache
   scope of the lent job shown read-only or closed in the job's own log (a
   refused save, `cache-mode`; R1 #1256 B3)**, **the plan artifact digest
   verified by the required parts and whether the runtime token may delete
   an artifact (R1 #1256 B4)**, and the post-job disk
   check of §6 (free ≥ 6 GiB, no `disk_low` episode); only then the others
   are enabled and left in service.

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

# 4. the runners (this lot), prepared but NOT started and NOT registered: the
#    guard must already cover them or --prepare refuses. ci-3 holds 8.95 GiB
#    free: qualify ONE slot with the floor §3 justifies (5 GiB guard
#    threshold + 1 GiB install margin), not the 15 GiB default. Nothing is
#    registered here: there is no registration token on this host.
sudo deploy/guest/ci-runners.sh --prepare --count 1 --disk-floor-mib 6144
sudo deploy/guest/ci-runners.sh --check
du -sh /srv/ameesh-guest/ci; df -Pk /    # record the real install cost

# 5. enforce, guests still off: --enable refuses while the guard observes
#    (R1 #1256). The observation day of step 3 is done by now.
sudo deploy/guest/install-pilot.sh --set-guard-mode enforce

# 6. qualification: the bureau mints ONE single-use configuration off the
#    host (generate-jitconfig, labels self-hosted/linux/ameesh-lent, name
#    ameesh-nxl-ci-3-1), copies it to /root/jit-ci-3-1.b64, and the operator
#    deposits it, arms the slot, and sets the routing variable for the
#    window — only the routed jobs (bilan, ui) may use the loan:
gh api -X POST repos/manaty/ameesh/actions/runners/generate-jitconfig \
  -f name="ameesh-nxl-ci-3-1" -F runner_group_id=1 \
  -f 'labels[]=self-hosted' -f 'labels[]=linux' -f 'labels[]=ameesh-lent' \
  -f work_folder=_work -q .encoded_jit_config > /root/jit-ci-3-1.b64   # bureau
sudo deploy/guest/ci-runners.sh --disk-floor-mib 6144 --deposit /root/jit-ci-3-1.b64
gh variable set AMEESH_LENT_RUNS_ON \
  --body '["self-hosted", "linux", "ameesh-lent"]'                    # bureau
# --prepare recorded the floor (6144); the later commands adopt it when the
# option is omitted (R1 delta B5)
sudo deploy/guest/ci-runners.sh --enable 1
sudo deploy/guest/ci-runners.sh --check
# a dispatched run on main (or a PR touching ui/**) carries `bilan`; during
# the job, prove the container is in the slice, and that the runner is gone
# afterwards:
systemd-cgls ameesh-guest.slice | grep -E 'Runner.Worker|postgres'
docker ps                       # no guest container here (host daemon)
gh api "repos/manaty/ameesh/actions/runners?per_page=100" --paginate \
  -q '.runners[] | select(.name|startswith("ameesh-nxl-ci-3-")) | .name'
# expected after the job: nothing (the JIT runner removed itself)

# 6bis. the disk decides the 2nd runner, not the plan: after the job, with the
#    guest idle, free must be back >= 6 GiB and the guard must never have
#    published `disk_low`. Otherwise the loan stays at one runner (or closes).
df -Pk /; /opt/ameesh/venv/bin/ameesh guard check --json | grep -E 'disk|level'

# 7. service: only after 6bis passes; the recorded count (1) is adopted, r1 is
#    kept, r2 is prepared. Each job of the loan needs its own deposit: keep
#    the spool armed while the loan is useful, and clear the routing variable
#    when it is not.
sudo deploy/guest/ci-runners.sh --prepare --count 2 --disk-floor-mib 6144
sudo deploy/guest/ci-runners.sh --deposit /root/jit-ci-3-2.b64   # bureau, one per job
sudo deploy/guest/ci-runners.sh --enable 2
```

## 7. Removal — only after terminal jobs

The order matters; nothing else is touched.

1. **Routing off**: `gh variable delete AMEESH_LENT_RUNS_ON` (bureau) or set
   it back to the fleet's labels: no new job is sent to the loan.
2. **Pause**: `ci-runners.sh --disable` (refuses while a job runs; or
   `--wait-terminal 600` on the operator's explicit request).
3. **Terminal check** (first-hand, not a guess): every guest runner
   `busy=false` in the GitHub API, no `Runner.Worker` of the guest user for
   `ci/rN`, and no queued run that names them. The host-side removal refuses
   otherwise.
4. **Forge side** (bureau, admin token), from a workstation:
   ```
   gh api "repos/manaty/ameesh/actions/runners?per_page=100" --paginate \
     -q '.runners[] | select(.name|startswith("ameesh-nxl-<host>-")) | "\(.id) \(.name)"'
   gh api -X DELETE "repos/manaty/ameesh/actions/runners/<id>"    # once per line
   ```
   List **offline entries too**: a configuration deposited but never run
   leaves a registration that never came online, and it is removed the same
   way. Verify: the ameesh runner list is back to its 24 pre-loan runners,
   all online; **no Nexlink runner was unregistered** (that repository's
   list is untouched).
5. **Host side**: `ci-runners.sh --remove` (stops and removes the units, the
   shim, the gate wrapper, the runner directories and the deposit directory
   it recorded in `/etc/ameesh/ci-runners.state`; the shim and the wrapper
   are the two files of `/usr/local/lib/ameesh-guest-ci`, whose directory is
   removed too when empty; only what `--prepare` recorded is removed, and a
   tools path swapped for a link makes the whole removal refuse;
   `--keep-workspaces` keeps the logs). It never touches `actions.runner.*`,
   `/home/runner`, the application's Docker daemon, or the other ameesh
   guest files.
6. **Canon**: the pilot merges the Host fiche PR that removes the loan (or
   records its end); the lot notes the final API state.

Never, at any point: cancel or relaunch a Nexlink run; `docker system
prune`; delete a shared cache; unregister a Nexlink runner; move
`/home/runner`; edit a Nexlink workflow or `runs-on`.

## 8. What this lot does *not* do

No runner is registered, no host is modified, no token is minted, no CI run
is relaunched. The script and its witness are ready for review; the
activation waits for the gates of §5, and the whole loan stays reversible at
every step through the state file written by `ci-runners.sh --prepare`. The
loan carries **no required check** (option A): relieving the required suite
is the announcement of the lot, not of the loan.
