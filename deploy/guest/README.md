# Lending VM for heavy suites (L159b, mode b1)

The agents stay on the workstation; their heavy commands (full suite, e2e,
Postgres suites, builds) run on a VM through `ameesh offload run`. Design:
[L159 study](../../docs/design/etudes/l159-capacite-des-hotes.md), sections
2.2 (mode b1), 3 (security) and 4 (rollout). Operations:
[EXPLOITATION.md](../../docs/EXPLOITATION.md#heavy-suites-on-a-lending-vm--ameesh-offload-l159b).

| File | Role |
|---|---|
| `install.sh` | prepares the VM; run by its owner, as root (`--dry-run` shows everything first) |
| `install-pilot.sh` | pilot (a1, L159g): guest runners, slice, guard and gate on the host; run by its owner, as root (`--dry-run`, `--check`, `--uninstall`) |
| `PILOT-ameesh-agents-1.md` | the owner's exact commands and rollback for the first lending host |
| `PILOT-ameesh-agents-3.md` | the bureau's commands and rollback for the second host (#4011, no new spend): guest on the root filesystem, transport from agents-1, agents-2 and the host's own `ameesh` user (loopback) |
| `PILOT-ci-vm.md` | the CI VM lent in mode b1 only (L159h, decision #47757): offer, `install-pilot.sh --offload-only`, then `install.sh --gate file` |
| `ameesh-guard.service`, `ameesh-gate.tmpfiles.conf` | the capacity guard (L159e) and its gate directories, installed by `install-pilot.sh` |
| `../../src/ameesh/platform/offload_shell.py` | the VM side, installed as `/usr/local/lib/ameesh-guest/ameesh-offload-shell`; standard library only |
| `ameesh-job-launcher`, `ameesh-job-launcher.py` | the root-owned entry point of a job (L159b2), installed as `/usr/local/lib/ameesh-guest/ameesh-job-launcher` and admitted to the job user by a sudoers rule |
| `job-postgres.sh` | a throwaway Postgres per job, rootless Podman (`/usr/local/lib/ameesh-guest/job-postgres`) |
| `profiles/*.json` | what a job gets before its command: dependency cache, variables, hooks (`/etc/ameesh-guest/profiles/`) |
| `ci-runners.sh`, `ameesh-guest-ci@.service`, `ci-runner-gate.py` | lot #1256: GitHub Actions runners of the guest, for a temporary loan of CI capacity from another project's VM; `--prepare` installs `ci-runner-gate.py` as the unit's `ExecStart`, so the runner obeys the guard's gate; plan and gates in [`PILOT-ci-nexlink.md`](PILOT-ci-nexlink.md) |

## Owner's steps

1. Attach a dedicated volume and mount it on `/srv/ameesh-guest` (study
   4.1: 100 GB; a guest that fills it never reaches the system).
2. From a checkout of ameesh on the VM:

   ```sh
   sudo deploy/guest/install.sh --dry-run --key "$(cat poste.pub)" --from <workstation IP>,<mesh VM IP>
   sudo deploy/guest/install.sh --key "$(cat poste.pub)" --from <workstation IP>,<mesh VM IP>
   sudo deploy/guest/install.sh --check      # what the guest can read: expect only "ok"
   ```

   The defaults fit a VM whose application is our test suites: 3 slots,
   guest CPU quota 75 % of the host, `MemoryHigh` 50 % and `MemoryMax`
   60 % of RAM, one job capped at `MemoryMax / slots`, 1 h per job. Add
   the loopback ports of the VM's own services with `--deny-port`.

   By default the jobs run under their own account (`--job-user`,
   `ameesh-job`): another UID than the guest's, through the root-owned
   launcher. `--no-job-user` keeps the legacy layout, where the jobs share
   the guest's UID.
3. On the workstation, a `~/.ssh/config` entry (the script prints it), then
   `AMEESH_OFFLOAD_HOST=<alias>` in the runner's environment, and check:

   ```sh
   ameesh offload status            # {"host": {"gate": "available", ...}}
   ameesh offload run -- uname -n   # the VM's name
   ```

## What a job gets

* A throwaway checkout of the exact working tree (tracked and untracked
  files, ignored ones excluded), in the same subdirectory as on the
  workstation. Output and exit code come back unchanged.
* With the job user (the default): a UID of its own. The kernel then denies
  the job the guest's `/proc` (its nonce and its streams), its HOME, its
  caches, and any write into the mirrors the guest's own git reads. The two
  accounts share the `ameesh-jobs` group for what the job must read or
  write; each phase goes through the root-owned launcher, and the job's own
  user manager (linger) still owns its slice.
* `TMPDIR` inside its job folder, on the volume, removed at the end.
* A transient slice `ameesh-job-<id>.slice` under the job's user manager
  (the guest's without `--job-user`), capped by `agent_memory_max`.
  Everything of the job runs in it: dependency
  setup, hooks, the command (scope `ameesh-job-<id>.scope`) and its Podman
  containers (`--cgroup-parent $OFFLOAD_SLICE`). The slice is killed when
  the job ends. The whole guest is capped by its `user-<uid>.slice`.
* No stream of the SSH session: every phase writes into pipes that the VM
  shell relays, so no process of the job, even one that outlives it, can
  read the session back (`/proc/self/fd/2`) nor write into it once the job
  is over. The `result` line carries an HMAC keyed by the session's nonce.
* No `XDG_RUNTIME_DIR` nor `DBUS_SESSION_BUS_ADDRESS` for the command and the
  dependency setup (the repository's code), and no `socket(AF_UNIX)` either:
  the launcher's `exec` installs a seccomp filter (libseccomp) before the
  payload's first instruction, so the job cannot reach its own user manager
  by rebuilding `/run/user/<uid>/bus` and start a unit outside its slice
  (R1-B3 of #354). The rule compares the domain masked to the kernel's 32-bit
  `int` (R2-B3: an equality test on the 64-bit register let
  `socket(0x100000001)` start an AF_UNIX socket). The filter also refuses
  `io_uring_setup/enter/register`, which build an AF_UNIX socket without
  `socket(2)` (R2-B2), and `ptrace`, `process_vm_readv`, `process_vm_writev`
  and `pidfd_getfd`, which would inject into a same-UID process that still
  holds the bus (R2-B4). `socketpair` stays allowed (asyncio, multiprocessing).
  The client's `--env` never reaches `systemd-run` nor the `exec` wrapper --
  both load ld.so before any filter (`LD_AUDIT` was the R2-B1 witness): it
  travels as inert JSON chunks and `exec` applies it once the filter is
  installed. The profile's hooks are root-owned install-side code and keep
  the manager (rootless Podman); they never receive the client's `--env` and
  the launcher strips the loader variables from theirs. An uninstallable
  filter refuses the job.
* `kernel.yama.ptrace_scope` at 1 at least (`/etc/sysctl.d/50-ameesh-guest.conf`,
  never lowered; Debian ships 0): the job shares its UID with its own user
  manager and the hooks, which keep the bus; without Yama the payload could
  trace them (`ptrace`, `/proc/<pid>/mem`) and act through them (B4 of the
  #354 R3 reading). `--check` verifies it; a kernel without Yama is refused.
* One supervision for every phase: a cancel, the loss of the SSH session,
  the gate or the job's deadline (counted from its slot) end setup, hooks
  and command alike. The `after` hook still runs (teardown, 2 min at most),
  after the job's slice is killed: nothing of the payload outlives into it
  (R2-B4).
* A job whose shell died is cleaned at the next run: its slice is killed and
  its profile's `after` hook run (containers removed) before any new slot.
* A slot (`flock`): with all slots busy, the client says it waits.
* Its profile (`--profile`, default `default`):
  * `default`: the command as is;
  * `postgres`: a throwaway Postgres on a free loopback port, with
    `PGPASSWORD`, `AMEESH_TEST_DSN` and `DATABASE_URL`;
  * `ameesh`: the same, plus the psycopg venv cached by the hash of
    `pyproject.toml`, for `scripts/test.sh` (which then skips `pg-up.sh`).

  Postgres image volumes use tmpfs: PGDATA disappears when the container
  stops, including after a killed job, and counts against the job's memory
  cap. Both the profile's `down` hook and the launcher's `close` remove the
  container with its volumes. At job open/close, the dedicated job user also
  reads Podman's volume inventory and removes anonymous unused volumes
  created more than an hour ago, using `volume rm` without force;
  volumes referenced by any container and named volumes are preserved.
  Successful removals are logged by name on the launcher's stderr.

  Updating the Python package alone does **not** update these root-owned
  scripts. `deploy/mise-a-jour/vm.sh` now refreshes the existing guest runtime
  during `installer` and `retour`, and checks its SHA256 against the installed
  source tree during `controler`. This copies only the forced command,
  Postgres hook and (when already provisioned) the job launcher pair. It does
  not provision a guest or change its keys, profiles, configuration, users or
  firewall. A partial or unsafe installation fails explicitly. To refresh an
  existing guest separately, run as administrator from a trusted checkout:

  ```sh
  python3 deploy/mise-a-jour/guest-runtime.py /path/to/exact/staging/tree
  python3 deploy/mise-a-jour/guest-runtime.py /path/to/exact/staging/tree --check
  ```
  Cleanup failures are reported and do not prevent releasing runtime caps.
  `ameesh doctor` warns from 80% inode use on the host's watched filesystems.

  A profile is JSON: `lockfiles` and `setup` (a cache built once per hash
  of those files, in `$OFFLOAD_CACHE`), `env` (`${OFFLOAD_CACHE}`,
  `${OFFLOAD_WORK}`, `${OFFLOAD_JOB}`, `${HOME}`, `${PATH}` expanded),
  `before` (its `K=V` stdout lines join the job's environment) and `after`
  (teardown, even after a kill). Profiles are root-owned: the client
  chooses one by name, never writes one.

## Security model (study, section 3)

* `ameesh-guest`: no password, no sudo, no `docker` group, home on the
  volume (0710, group `ameesh-jobs`). `install.sh` refuses a guest in sudo,
  wheel, docker, adm, lxd or libvirt.
* `ameesh-job` (L159b2, the default): the jobs' account, another UID, no
  password, no sudo, its own home (`$ROOT/home`, 0700, unreadable by the
  guest) and its own user manager. The guest starts every phase through the
  root-owned launcher, by a sudoers rule that names that file and that user
  only; the launcher refuses to run as root. The group `ameesh-jobs` is the
  one path between the two: the guest is a member of it (so it can give the
  job a folder), it keeps the volume and its mirrors (setgid, so the folders
  it creates keep the group), the job traverses the volume, reads the
  mirrors and writes only the folders it was granted. A job's own folder is
  2750: the job enters it and writes `work`/`tmp` (2770) below, but it can
  neither write nor replace `meta.json` and `cancel`, and the shell never
  opens those through a link (`O_NOFOLLOW`, R1-B2 of #354). Every grant walks
  the tree by descriptor (`O_NOFOLLOW`, `fchmod`/`fchown` on the opened fd,
  R2-B3), so a same-UID job cannot swap an entry for a link while the shell
  is granting it; a ready cache is never granted again, it is rebuilt when it
  does not already carry the shared group (R2-B3).
* SSH: the keys live in a root-owned file (`/etc/ameesh-guest/authorized_keys`,
  `restrict`, `from=`), and an sshd `Match User` block forces
  `ameesh-offload-shell`: four verbs (`put`, `run`, `status`, `cancel`),
  no shell, no forwarding, no tty, no `~/.ssh/rc`. A key the guest might
  add to its own home is never read.
* The client's `--env` cannot set `PATH`, `HOME`, `LD_PRELOAD`, `TMPDIR` or
  the job variables; repository, commit, profile and job names are
  validated; `--cwd` stays inside the checkout.
* Outgoing traffic of both uids (nftables, `ameesh-guest-firewall.service`):
  no listed application port on the loopback, no private network, no
  metadata service (169.254.0.0/16); DNS to the configured resolvers and
  HTTPS out stay open (packages, images). `--allow-net` opens a network.
* Caps on `user-<uid>.slice` of both accounts: `CPUWeight=10`, `IOWeight=10`,
  `CPUQuota`, `MemoryHigh`, `MemoryMax`, no swap, `TasksMax`. The guest
  cannot raise them. Its processes run with `oom_score_adj` 500 (`choom` in
  the forced command): the global OOM picks them first.
* Gate: with `--gate file`, no new job unless
  `/run/ameesh-gate/state/state.json` says `available`; a running job is
  killed at the drain deadline, or at once on `stopped`; an unreadable or
  expired state counts as `stopped`. The client then reruns the command on
  the workstation. Until the guard (L159e) exists, keep `--gate none`.

**Limits.** The guest shares the kernel of the VM: isolation is Linux's
between users. The systemd sandboxing of the study (`ProtectSystem`,
`ProtectProc=invisible`, `PrivateTmp`…) applies to units (mode a); an SSH
session is not one, so in mode b1 the guest can still see other users'
processes and write to the shared `/tmp` (its jobs use `TMPDIR` on the
volume). With the job user (the default) the kernel denies the job the
guest's `/proc`, HOME and caches; with `--no-job-user` the guest's
`_no_dump` and its `foreign_holders` scan still guard them, as in L159b.
`install.sh --check` lists what it can read; mount `/proc` with
`hidepid=invisible` if the VM's command lines carry secrets.

## Rollback (study 4.3)

On the workstation, remove `AMEESH_OFFLOAD_HOST`: the agents test locally
again. On the VM:

```sh
sudo deploy/guest/install.sh --uninstall            # keeps /srv/ameesh-guest
sudo deploy/guest/install.sh --uninstall --purge    # and removes it
```

## Usage measure

Each job adds one line to `/srv/ameesh-guest/jobs.jsonl` (repository,
profile, command, outcome, exit code, seconds): the "usage" line of the
study's success measures (4.4).
