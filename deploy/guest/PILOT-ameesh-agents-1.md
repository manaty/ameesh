# Pilot (a1) on ameesh-agents-1: the owner's commands (L159g)

The exact commands of the first lending host, for its owner, as root. The
general steps and the report are in
[EXPLOITATION.md](../../docs/EXPLOITATION.md#lending-host-pilot-mode-a1--ameesh-hosts-host---report-l159g);
the design in the [L159 study](../../docs/design/etudes/l159-capacite-des-hotes.md),
sections 2.1, 3 and 4. Validated by mesh-design on 2026-10-12 (lot #192):

* guests: `deepseek6` and `deepseek1` (today the last two personas on
  manaty-mesh-1; `deepseek5` became a native agent of the host with
  manaty/home#27), DeepSeek through `dsh`, no subscription account; installed
  in observation only;
* the script's default offer: `cpu_max 2`, `cpu_weight 10`, `memory_max
  9GiB`, `agent_memory_max 4.5GiB`, `disk_max 40GiB`; reclaim at 6 GiB
  available, CPU pressure 20 %, memory 2 %, I/O 20 %, 10 GiB free on `/`;
  drain 120 s, calm 600 s;
* the host's own agents stay thirteen (manaty/home#22, #23, #26, #27), to be
  reviewed with the report of 20/10; the canon's `max_agents` goes from 13
  to 15 because it counts every placement of the host, the guests' included
  (13 would make the fiche invalid); manaty-mesh-1 goes to `max_agents: 0`
  and carries only the mesh database (validated by the owner, relayed by
  bureau on 2026-10-12 at 19:40).

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

* **The guest's volume: a 40 GB Block Storage volume** (the owner's
  decision 10408, 2026-10-12, option a). `/srv/ameesh-guest` is not a
  mount point today: one local 150 GB disk (`l_ssd`), 109 GB free; the VM
  is a GP1-S in `fr-par-1`. The volume, 5K IOPS, is mounted on
  `/srv/ameesh-guest`: a guest that fills it never reaches the system nor
  the host's own agents (study §4.1). Its size holds the offer's `disk_max
  40GiB` (no filesystem quota; the guard's reclaim watches `/`). Cost:
  €0.000130/GB/hour excluding VAT (scaleway.com/en/pricing/storage, read on
  2026-10-12), about **€3.80 per month** (730 h), snapshots not included.
  `deploy/guest/volume-pilot.sh` does the steps below, idempotent, with
  its rollback (`unmount`, then `detach [--delete]`); read its `--dry-run`
  first:

  ```sh
  deploy/guest/volume-pilot.sh create                     # workstation, scw
  sudo deploy/guest/volume-pilot.sh mount --volume-id <volume-id>   # host
  sudo deploy/guest/volume-pilot.sh check --volume-id <volume-id>
  ```

  It finds the disk by the volume's id, formats it only when blank, and
  writes the fstab line with the filesystem's UUID. By hand, from a
  workstation with the `scw` CLI configured for the ameesh project:

  ```sh
  scw block volume create name=ameesh-guest perf-iops=5000 from-empty.size=40GB zone=fr-par-1
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
  findmnt /srv/ameesh-guest                                # the volume, 40 GB
  ```

  `nofail`: the host boots even without the volume. The guest's runners
  do not start then (`RequiresMountsFor=/srv/ameesh-guest`): they never
  write on the root disk instead; the host's agents are not affected.

  `--allow-rootfs` (the guest on `/`, `disk_max` indicative only) is not
  used on this host.

## Install (guard in observation, no guest runner started)

`dsh` is in `/opt/node/bin`, which `sudo`'s `secure_path` does not contain:
give it with `--path`.

```sh
cd /root/ameesh
sudo deploy/guest/install-pilot.sh --dry-run --host ameesh-agents-1 \
  --agent deepseek6 --agent deepseek1 --path /opt/node/bin    # read it all
sudo deploy/guest/install-pilot.sh --host ameesh-agents-1 \
  --agent deepseek6 --agent deepseek1 --path /opt/node/bin
```

Then the secrets (files created empty, `0640 root:ameesh-guest`, never
overwritten by the script):

```sh
sudoedit /etc/ameesh/guest.env             # AMEESH_DSN=… the host's login role (L56)
sudoedit /etc/ameesh/guest-deepseek1.env   # DEEPSEEK_API_KEY=…, GH_TOKEN=…
sudoedit /etc/ameesh/guest-deepseek6.env
sudo deploy/guest/install-pilot.sh --check # every line "ok"
```

## Enable the two guests

Only once the canon PR (manaty/home#24, a draft until that day) is merged: the Host fiche gets `policy.lend` (the
offer above, the one in `/etc/ameesh/guard.json`) and `max_agents: 15` (13
own agents + 2 guests), without `volatile`; `policy.work_dirs` puts the guests'
work under `/srv/ameesh-guest/work/{agent}`; the placements of deepseek6 and
deepseek1 move from manaty-mesh-1 to ameesh-agents-1, which keeps
`max_agents: 0` there. Their runners on manaty-mesh-1 are
stopped first: an agent runs on one host at a time.

```sh
sudo deploy/guest/install-pilot.sh --enable deepseek6 deepseek1
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
| A guest misbehaves | `sudo deploy/guest/install-pilot.sh --disable deepseek1` (its turn drains first, 180 s at most) |
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
(`/etc/ameesh/pilot-install.state`); a directory or a secrets file that
was there before, a tree with a mount point in it, or anything outside them
stays (a mount point is read in `/proc/self/mountinfo`, binds included).
What stays keeps its record, so a later `--purge` can still remove it,
the user too when `userdel` fails (its processes still running). A `--root`
through a symbolic link is refused: give the resolved path. The install refuses a volume bigger than `disk_max` (nothing else
holds it); `--accept-larger-volume` overrides. Then, outside the host: a canon PR puts
deepseek6 and deepseek1 back on manaty-mesh-1 (its `max_agents` back to 2) and removes `policy.lend`, and
the host's login role (L56) is dropped. The volume: `sudo
deploy/guest/volume-pilot.sh unmount` on the host, then
`deploy/guest/volume-pilot.sh detach [--delete]` from the workstation
(`--delete`: its data lost, its billing stopped).
