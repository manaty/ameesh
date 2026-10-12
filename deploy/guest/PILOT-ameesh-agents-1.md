# Pilot (a1) on ameesh-agents-1: the owner's commands (L159g)

The exact commands of the first lending host, for its owner, as root. The
general steps and the report are in
[EXPLOITATION.md](../../docs/EXPLOITATION.md#lending-host-pilot-mode-a1--ameesh-hosts-host---report-l159g);
the design in the [L159 study](../../docs/design/etudes/l159-capacite-des-hotes.md),
sections 2.1, 3 and 4. Validated by mesh-design on 2026-10-12 (lot #192):

* guests: `deepseek5` and `deepseek6` (today on manaty-mesh-1), DeepSeek
  through `dsh`, no subscription account; installed in observation only;
* the script's default offer: `cpu_max 2`, `cpu_weight 10`, `memory_max
  9GiB`, `agent_memory_max 4.5GiB`, `disk_max 40GiB`; reclaim at 6 GiB
  available, CPU pressure 20 %, memory 2 %, I/O 20 %, 10 GiB free on `/`;
  drain 120 s, calm 600 s;
* the host's own agents stay six, to be reviewed with the report of 20/10;
  the canon's `max_agents` goes from 6 to 8 because it counts every
  placement of the host, the guests' included (6 would make the fiche
  invalid).

## Before

* On `main`: the gate (L159f, #191), `--once` (L159f4, #378) and this
  script (L159g, #192).
* **A checkout that only root can change.** The script installs
  `/opt/ameesh/venv` from it, as root: never run it from an agent's clone
  (`/home/ameesh/work/…` is writable by the agents).

  ```sh
  sudo git clone https://github.com/manaty/ameesh /root/ameesh   # or: git -C /root/ameesh pull
  cd /root/ameesh && git log -1 --oneline                        # the reviewed commit of main
  ```

* **The guest's volume: the owner's decision** (it costs money and changes
  the infrastructure). `/srv/ameesh-guest` is not a mount point today: one
  local 150 GB disk (`l_ssd`), 109 GB free; the VM is a GP1-S in `fr-par-1`.
  - **Recommended** (study §4.1): a Block Storage volume, 5K IOPS, mounted
    on `/srv/ameesh-guest`. A guest that fills it never reaches the system
    nor the host's own agents. Only the volume's size holds the offer's
    `disk_max` (no filesystem quota; the guard's reclaim watches `/`), so
    the owner picks one of two sizes. Cost: €0.000130/GB/hour excluding VAT
    (scaleway.com/en/pricing/storage, read on 2026-10-12), 730 h a month,
    snapshots not included:

    | Size | Cost | `disk_max` |
    |---|---|---|
    | 40 GB | about **€3.80 per month** | `40GiB`, the offer as validated: held by the volume |
    | 100 GB | about **€9.50 per month** | raised to `100GiB`, in the offer (`--lend`) and the canon (manaty/home#24), else the script warns that it is not held |

    From a workstation with the `scw` CLI configured for the ameesh project,
    `<size>` being `40GB` or `100GB`:

    ```sh
    scw block volume create name=ameesh-guest perf-iops=5000 from-empty.size=<size> zone=fr-par-1
    scw block volume wait <volume-id> zone=fr-par-1          # until "available"
    scw instance server list name=ameesh-agents-1 zone=fr-par-1
    scw instance server attach-volume server-id=<server-id> volume-id=<volume-id> \
      volume-type=sbs_volume zone=fr-par-1
    ```

    Then on the host (the new disk is the one `lsblk` did not list before,
    without partitions; check its size):

    ```sh
    lsblk -o NAME,SIZE,TYPE,MOUNTPOINT
    sudo mkfs.ext4 -L ameesh-guest /dev/<new disk>
    sudo mkdir -p /srv/ameesh-guest
    echo 'LABEL=ameesh-guest /srv/ameesh-guest ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2' \
      | sudo tee -a /etc/fstab
    sudo systemctl daemon-reload && sudo mount /srv/ameesh-guest
    findmnt /srv/ameesh-guest                                # the volume, of the chosen size
    ```

    `nofail`: the host boots even without the volume. The guest's runners
    do not start then (`RequiresMountsFor=/srv/ameesh-guest`): they never
    write on the root disk instead; the host's agents are not affected.
  - **Without a volume**: `--allow-rootfs` on the two install commands below.
    The guest then shares `/` with the host's agents: `disk_max` is
    indicative only (the script says so), and only the guard's reclaim (under
    10 GiB free on `/`) holds the guest back. Not to be used while the owner
    has not chosen.

## Install (guard in observation, no guest runner started)

`dsh` is in `/opt/node/bin`, which `sudo`'s `secure_path` does not contain:
give it with `--path`.

```sh
cd /root/ameesh
sudo deploy/guest/install-pilot.sh --dry-run --host ameesh-agents-1 \
  --agent deepseek5 --agent deepseek6 --path /opt/node/bin    # read it all
sudo deploy/guest/install-pilot.sh --host ameesh-agents-1 \
  --agent deepseek5 --agent deepseek6 --path /opt/node/bin
```

Then the secrets (files created empty, `0640 root:ameesh-guest`, never
overwritten by the script):

```sh
sudoedit /etc/ameesh/guest.env             # AMEESH_DSN=… the host's login role (L56)
sudoedit /etc/ameesh/guest-deepseek5.env   # DEEPSEEK_API_KEY=…, GH_TOKEN=…
sudoedit /etc/ameesh/guest-deepseek6.env
sudo deploy/guest/install-pilot.sh --check # every line "ok"
```

## Enable the two guests

Only once the canon PR (manaty/home#24, a draft until that day) is merged: the Host fiche gets `policy.lend` (the
offer above, the one in `/etc/ameesh/guard.json`) and `max_agents: 8` (6
own agents + 2 guests), without `volatile`; `policy.work_dirs` puts the guests'
work under `/srv/ameesh-guest/work/{agent}`; the placements of deepseek5 and
deepseek6 move from manaty-mesh-1 to ameesh-agents-1. Their runners on manaty-mesh-1 are
stopped first: an agent runs on one host at a time.

```sh
sudo deploy/guest/install-pilot.sh --enable deepseek5 deepseek6
sudo deploy/guest/install-pilot.sh --check
systemctl status 'ameesh-guest-runner@*'
```

After a day of observation without a missed target
(`ameesh hosts ameesh-agents-1 --report --days 1`):

```sh
sudo deploy/guest/install-pilot.sh --set-guard-mode enforce
```

## Rollback

From the lightest to the full one; each step is enough on its own.

| When | Command |
|---|---|
| The guard's caps get in the way of the host | `sudo deploy/guest/install-pilot.sh --set-guard-mode observe` (stops the guard, takes its caps off the slice and the runners, starts it again observing) |
| A guest misbehaves | `sudo deploy/guest/install-pilot.sh --disable deepseek5` (its turn drains first, 180 s at most) |
| The pilot stops (§4.4 target missed, natives delayed) | `sudo deploy/guest/install-pilot.sh --uninstall` |
| Nothing of the pilot must stay | `sudo deploy/guest/install-pilot.sh --uninstall --purge` |

An observing guard starts no runner again: one the enforcing guard had
stopped (level 4) shows `inactive` in `--check`, and `--enable` starts it.

`--uninstall` follows the study's order (§4.3): `ameesh guard drain`, the
guest runners then the guard stopped and disabled, units, drop-ins and gate
directories removed; the host's own agents (`ameesh-agents.slice`) are not
touched. `--purge` also removes the secrets, the offer, and the user, the
venv and the guest's directories, only those the script created: it
records each one, by its absolute path, before creating it
(`/etc/ameesh/pilot-install.state`); a directory that was there before, a
mount point, or anything outside them stays. Then, outside the host: a canon PR puts
deepseek5 and deepseek6 back on manaty-mesh-1 and removes `policy.lend`, and
the host's login role (L56) is dropped. A mounted volume is unmounted and
detached by hand.
