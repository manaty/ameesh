# Offload guest on ameesh-agents-3: the bureau's commands (#4011)

The second agent host that lends capacity to `ameesh offload`, after
[ameesh-agents-1](PILOT-ameesh-agents-1.md). The pilot's decision is
**no new spend**: no VM `ameesh-tests`, no dedicated 40 GiB volume; the
guest lives on the host's root filesystem, with the offer written in the
Host card `hotes/ameesh-agents-3.md` (`policy.lend`). The host keeps its six
application seats; the guest is capped by its own slice and the capacity
guard's gate. The transport has three clients: agents-1 and agents-2 over the
public address, and the host's own `ameesh` user over the loopback. Without
the local client, a freeze launched by one of the six seats on agents-3
would stay `no-vm`.

Why agents-3: on 2026-10-27 the canon declared only `ameesh-agents-1` for
prevalidation, and that host's gate was `stopped` (`app_cpu`, level 4), so
every freeze journalised `no-vm`; `ameesh-tests` was never provisioned.
agents-3 was created the same day (GP1-S, 8 vCPU, 32 GiB, 125 GiB free on
`/`), and can carry a guest without a purchase.

## Before

* The canon PR (#4011, manaty/home) merged: `policy.lend` on the agents-3
  Host card and `prevalidation.hosts: [ameesh-agents-3, ameesh-agents-1]`
  in `processus/mecanique.yaml`.
* A checkout only root can change, at the reviewed commit of `main`:

  ```sh
  sudo git clone https://github.com/manaty/ameesh /root/ameesh   # or: git -C /root/ameesh pull
  cd /root/ameesh && git log -1 --oneline
  ```

* Read the host before changing it (the values below were read on
  2026-10-27; read them again):

  ```sh
  nproc; free -g; df -h / /srv/ameesh-guest 2>&1
  ss -ltn | awk 'NR>1 {print $4}'        # loopback ports to deny the guest
  getent passwd ameesh ameesh-guest ameesh-job
  test -e /etc/ameesh-guest && echo "guest already installed" || echo "no guest"
  ```

  If the guest is already installed, do not reinstall it blindly: read
  `/etc/ameesh-guest/authorized_keys`, keep every existing key, and add the
  three new ones (the install below writes the file from its `--key` options).

* The offer, exactly as the Host card declares it (`lend`):

  ```json
  {
    "cpu_max": 2,
    "cpu_weight": 10,
    "memory_max": "9GiB",
    "agent_memory_max": "4.5GiB",
    "disk_max": "40GiB",
    "reclaim": {
      "memory_available_min": "6GiB",
      "cpu_pressure_max": "20%",
      "memory_pressure_max": "2%",
      "io_pressure_max": "20%",
      "disk_free_min": "10GiB",
      "disk_paths": ["/"]
    },
    "drain_seconds": 120,
    "calm_seconds": 600
  }
  ```

  Write it to `/root/4011-lend.json` (root, 0600). `disk_max` is
  **indicative** on a root filesystem: no filesystem quota enforces it. The
  guard's `disk_free_min` (10 GiB on `/`) closes the gate; each job's folder
  is removed at the end.

## 1. The guard and its gate (root, on agents-3)

`--offload-only` installs the guard, the gate and the guest's slice, without
any guest runner (the host's own agents are not guests). Read the dry run
first:

```sh
cd /root/ameesh
sudo deploy/guest/install-pilot.sh --dry-run --offload-only \
  --host ameesh-agents-3 --lend /root/4011-lend.json \
  --root /srv/ameesh-guest --allow-rootfs
sudo deploy/guest/install-pilot.sh --offload-only \
  --host ameesh-agents-3 --lend /root/4011-lend.json \
  --root /srv/ameesh-guest --allow-rootfs
sudo deploy/guest/install-pilot.sh --check --offload-only --host ameesh-agents-3
```

The guard starts in observation (`observe`): it publishes the gate, it does
not move the caps. The gate file is `/run/ameesh-gate/state/state.json`
(`available`, `draining`, `stopped`). Leave `observe` for the pilot; a later
`--set-guard-mode enforce` is a separate decision.

## 2. The guest (root, on agents-3)

The three public keys come from agents-1, agents-2 and agents-3 itself
(section 3); each is a single line `ssh-ed25519 AAAA… offload@ameesh-agents-N`.
Never the owner's root key; no private key leaves its host.

```sh
cd /root/ameesh
CAPS="--slots 2 --cpu-share 0.25 --memory-max 9G --memory-high 8G \
      --job-memory-max 4.5G --max-job-seconds 7200"
sudo deploy/guest/install.sh --dry-run --gate file --allow-rootfs \
  --root /srv/ameesh-guest $CAPS \
  --key "<PUB agents-1>" --key "<PUB agents-2>" --key "<PUB agents-3>" \
  --from 78.232.56.22,78.232.60.1,127.0.0.1 \
  --deny-port 55432,55441,55442,55443,34949
sudo deploy/guest/install.sh --gate file --allow-rootfs \
  --root /srv/ameesh-guest $CAPS \
  --key "<PUB agents-1>" --key "<PUB agents-2>" --key "<PUB agents-3>" \
  --from 78.232.56.22,78.232.60.1,127.0.0.1 \
  --deny-port 55432,55441,55442,55443,34949
sudo deploy/guest/install.sh --check
```

`0.25` is 2 vCPU of 8; `--memory-max 9G` and `--job-memory-max 4.5G` are
the offer's `memory_max` and `agent_memory_max`; two slots fit two jobs. The
`--from` list binds the **three** keys to the clients' sources: the two
remote clients' public egress addresses and the loopback for the local
client on agents-3 (the `from=` granularity is the file, not the key). The
`--deny-port` list adds the services actually listening on the host's
loopback on 2026-10-27 (the default list stays); read `ss -ltn` again and add
any new port.

The install adds `git`, rootless `podman`, `uidmap`, `slirp4netns`,
`nftables` and `postgresql-client` when missing (apt, no new spend). Check
the rootless Podman line (`podman: rootless ok for ameesh-job`); a warning
means the `ameesh` profile's Postgres will fail — stop and report.

## 3. The transport (user `ameesh`, on agents-1, agents-2 and agents-3)

One dedicated ed25519 key per client, never the owner's key, outside
`~/.config/ameesh/env` (no runner restart needed on these VMs: `~/.ssh` is
not masked there). Generate the three keys first; section 2 needs their
public lines.

**Remote clients, agents-1 and agents-2** (public address):

```sh
# as ameesh, on agents-1 (same on agents-2, with -C offload@ameesh-agents-2)
install -d -m 0700 ~/.ssh
[ -f ~/.ssh/offload-agents-3_ed25519 ] \
  || ssh-keygen -t ed25519 -N '' -C "offload@ameesh-agents-1" \
       -f ~/.ssh/offload-agents-3_ed25519
cat ~/.ssh/offload-agents-3_ed25519.pub       # give this line to agents-3
```

Add the alias, once, to `~/.ssh/config` (0600):

```
Host ameesh-agents-3
    HostName 51.15.137.245
    User ameesh-guest
    IdentityFile ~/.ssh/offload-agents-3_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

Add the host key to `~/.ssh/known_hosts` only after comparing its
fingerprint with the host's own file (`ssh-keygen -lf
/etc/ssh/ssh_host_ed25519_key.pub`, root on agents-3):

```sh
HOSTKEY=$(mktemp "${TMPDIR:-/tmp}/agents-3.hostkey.XXXXXX")
ssh-keyscan -t ed25519 51.15.137.245 > "$HOSTKEY"
ssh-keygen -lf "$HOSTKEY"                    # must equal the host's fingerprint
cat "$HOSTKEY" >> ~/.ssh/known_hosts
rm -f "$HOSTKEY"
```

**Local client, agents-3 itself** (loopback; six seats freeze from there):

```sh
# as ameesh, on agents-3
install -d -m 0700 ~/.ssh
[ -f ~/.ssh/offload-agents-3_ed25519 ] \
  || ssh-keygen -t ed25519 -N '' -C "offload@ameesh-agents-3" \
       -f ~/.ssh/offload-agents-3_ed25519
cat ~/.ssh/offload-agents-3_ed25519.pub       # the third --key of section 2
```

Add the same block to `~/.ssh/config` (0600), with the loopback as the host:

```
Host ameesh-agents-3
    HostName 127.0.0.1
    User ameesh-guest
    IdentityFile ~/.ssh/offload-agents-3_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

and the `127.0.0.1` host key (same fingerprint as above):

```sh
HOSTKEY=$(mktemp "${TMPDIR:-/tmp}/agents-3.hostkey.XXXXXX")
ssh-keyscan -t ed25519 127.0.0.1 > "$HOSTKEY"
ssh-keygen -lf "$HOSTKEY"                    # must equal the host's fingerprint
cat "$HOSTKEY" >> ~/.ssh/known_hosts
rm -f "$HOSTKEY"
```

Test from each of the three clients as `ameesh`:

```sh
ameesh offload status --host ameesh-agents-3
ameesh offload run --host ameesh-agents-3 --profile default --fallback none -- uname -n
```

The first must show `"gate": "available"` (if `stopped`, read
`/run/ameesh-gate/state/state.json` on agents-3: the guard is holding the
gate for a named reason); the second must print `ameesh-agents-3`. The
`from=127.0.0.1` lets the local client connect over the loopback; the private
halves of the three keys never leave their host, and no key is copied from
one host to another.

Then the real profile from agents-1 (first run: venv + Postgres image pull,
a few minutes). `scripts/test.sh` is exactly what the prevalidation runs,
with `AMEESH_TEST_VENV` and `AMEESH_TEST_DSN` from the profile:

```sh
ameesh offload run --host ameesh-agents-3 --profile ameesh --fallback none -- \
  scripts/test.sh tests/test_1770_prevalidation.py
```

The command after `--` is a list of words, not one quoted string: the offload
launcher execs the first word with the others as arguments. A single quoted
`'scripts/test.sh tests/…'` is one argv and fails with `cannot exec` (ENOENT).

## 4. The witness

Freeze a light lot that has code changes (not a docs-only lot) from an
agent on agents-1 or agents-2, then a second one from an agent on agents-3
(the six seats there): **the witness must include a freeze launched from
agents-3**, otherwise its local transport is not proven. The freeze hook
calls `ameesh work prevalidate`; with the canon above it tries
`ameesh-agents-3` first, then `ameesh-agents-1` on `no-vm` or
`gate-preempted`. Each journal must show `green` or `run-red` and
`host: ameesh-agents-3` — never `no-vm`. A `run-red` is a valid witness
(the tests ran); read the failing modules before calling it a regression.
If the gate is `stopped` (`app_cpu`), wait for it to reopen: the guard's
`calm_seconds` is 600 s.

## Rollback

On agents-3, root, from `/root/ameesh`:

```sh
sudo deploy/guest/install.sh --uninstall --purge
sudo deploy/guest/install-pilot.sh --uninstall --purge
```

On agents-1, agents-2 and agents-3, as `ameesh`: remove the
`ameesh-agents-3` block from `~/.ssh/config`, the `offload-agents-3_ed25519*`
pair and the known_hosts line. The owner's key and the host's agents are
untouched. Revert the canon PR separately.

## Risks, stated

* **Root filesystem, no hard disk quota.** `disk_max` 40 GiB is not
  enforced; the guard closes the gate under 10 GiB free and the job folder
  is removed at the end, but one job can write a lot before that. The
  alternative (a 40 GiB volume) is a new spend, refused by the pilot on
  2026-10-27.
* **A production agent host.** The guest's slice is capped (2 vCPU,
  9 GiB), but the host's six seats share the same 8 vCPU / 32 GiB; the
  guard's `app_cpu` / memory thresholds can close the gate at any time.
  The offload never falls back locally for prevalidation (`--fallback
  none`), so a closed gate is `no-vm`/`gate-preempted`, not a false red.
* **DHCP source addresses.** `--from` pins the two remote clients' public
  egress addresses (78.232.56.22, 78.232.60.1 on 2026-10-27) and the local
  client's loopback (127.0.0.1); if one changes, the key is refused.
  Re-read the source address and update the line. The three keys stay
  dedicated to their host: no private key is copied between them.
* **AppArmor / rootless Podman.** Ubuntu 24.04 restricts unprivileged user
  namespaces; the `podman` package's profile made it work on agents-1 with
  the same setting. If `--check` warns, the `ameesh` profile's Postgres
  will fail; fix that before the witness.
