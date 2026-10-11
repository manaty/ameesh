# Lending VM for heavy suites (L159b, mode b1)

The agents stay on the workstation; their heavy commands (full suite, e2e,
Postgres suites, builds) run on a VM through `ameesh offload run`. Design:
[L159 study](../../docs/design/etudes/l159-capacite-des-hotes.md), sections
2.2 (mode b1), 3 (security) and 4 (rollout). Operations:
[EXPLOITATION.md](../../docs/EXPLOITATION.md#heavy-suites-on-a-lending-vm--ameesh-offload-l159b).

| File | Role |
|---|---|
| `install.sh` | prepares the VM; run by its owner, as root (`--dry-run` shows everything first) |
| `../../src/ameesh/offload_shell.py` | the VM side, installed as `/usr/local/lib/ameesh-guest/ameesh-offload-shell`; standard library only |
| `job-postgres.sh` | a throwaway Postgres per job, rootless Podman (`/usr/local/lib/ameesh-guest/job-postgres`) |
| `profiles/*.json` | what a job gets before its command: dependency cache, variables, hooks (`/etc/ameesh-guest/profiles/`) |

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
* `TMPDIR` inside its job folder, on the volume, removed at the end.
* A transient scope `ameesh-job-<id>.scope` under the guest's user manager,
  capped by `agent_memory_max`; the whole guest is capped by its
  `user-<uid>.slice`.
* A slot (`flock`): with all slots busy, the client says it waits.
* Its profile (`--profile`, default `default`):
  * `default`: the command as is;
  * `postgres`: a throwaway Postgres on a free loopback port, with
    `PGPASSWORD`, `AMEESH_TEST_DSN` and `DATABASE_URL`;
  * `ameesh`: the same, plus the psycopg venv cached by the hash of
    `pyproject.toml`, for `scripts/test.sh` (which then skips `pg-up.sh`).

  A profile is JSON: `lockfiles` and `setup` (a cache built once per hash
  of those files, in `$OFFLOAD_CACHE`), `env` (`${OFFLOAD_CACHE}`,
  `${OFFLOAD_WORK}`, `${OFFLOAD_JOB}`, `${HOME}`, `${PATH}` expanded),
  `before` (its `K=V` stdout lines join the job's environment) and `after`
  (teardown, even after a kill). Profiles are root-owned: the client
  chooses one by name, never writes one.

## Security model (study, section 3)

* `ameesh-guest`: no password, no sudo, no `docker` group, home on the
  volume (0700). `install.sh` refuses a guest in sudo, wheel, docker, adm,
  lxd or libvirt.
* SSH: the keys live in a root-owned file (`/etc/ameesh-guest/authorized_keys`,
  `restrict`, `from=`), and an sshd `Match User` block forces
  `ameesh-offload-shell`: four verbs (`put`, `run`, `status`, `cancel`),
  no shell, no forwarding, no tty, no `~/.ssh/rc`. A key the guest might
  add to its own home is never read.
* The client's `--env` cannot set `PATH`, `HOME`, `LD_PRELOAD`, `TMPDIR` or
  the job variables; repository, commit, profile and job names are
  validated; `--cwd` stays inside the checkout.
* Outgoing traffic of the guest's uid (nftables, `ameesh-guest-firewall.service`):
  no listed application port on the loopback, no private network, no
  metadata service (169.254.0.0/16); DNS to the configured resolvers and
  HTTPS out stay open (packages, images). `--allow-net` opens a network.
* Caps on `user-<uid>.slice`: `CPUWeight=10`, `IOWeight=10`, `CPUQuota`,
  `MemoryHigh`, `MemoryMax`, no swap, `TasksMax`. The guest cannot raise
  them. Its processes run with `oom_score_adj` 500 (`choom` in the forced command): the global OOM picks
  them first.
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
volume). `install.sh --check` lists what it can read; mount `/proc` with
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
