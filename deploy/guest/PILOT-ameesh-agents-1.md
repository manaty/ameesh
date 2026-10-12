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
* the host's own agents keep `max_agents: 6`, to be reviewed with the report
  of 20/10.

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

* **The guest's volume.** `/srv/ameesh-guest` is not a mount point today
  (one 135 GB root disk, 109 GB free). Either:
  - (study §4.1) attach a Scaleway volume, about 100 GB, and mount it on
    `/srv/ameesh-guest` (`/etc/fstab`); a guest that fills it never reaches
    the system; or
  - add `--allow-rootfs` to the two install commands below. The guest then
    shares `/` with the host's agents: `disk_max` is not a filesystem quota,
    and only the guard's reclaim (under 10 GiB free on `/`) holds it back.

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

Only once the canon PR is merged: the Host fiche gets `policy.lend` (the
offer above, the one in `/etc/ameesh/guard.json`) and keeps `max_agents: 6`
for its own agents; the placements of deepseek5 and deepseek6 move from
manaty-mesh-1 to ameesh-agents-1 (work under `/srv/ameesh-guest/work`). Their runners on manaty-mesh-1 are
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
| The guard's caps get in the way of the host | `sudo deploy/guest/install-pilot.sh --set-guard-mode observe` |
| A guest misbehaves | `sudo deploy/guest/install-pilot.sh --disable deepseek5` (its turn drains first, 180 s at most) |
| The pilot stops (§4.4 target missed, natives delayed) | `sudo deploy/guest/install-pilot.sh --uninstall` |
| Nothing of the pilot must stay | `sudo deploy/guest/install-pilot.sh --uninstall --purge` |

`--uninstall` follows the study's order (§4.3): `ameesh guard drain`, the
guest runners then the guard stopped and disabled, units, drop-ins and gate
directories removed; the host's own agents (`ameesh-agents.slice`) are not
touched. `--purge` also removes the secrets, the offer, and the user, the
venv and the volume's content, only those the script created
(`/etc/ameesh/pilot-install.state`). Then, outside the host: a canon PR puts
deepseek5 and deepseek6 back on manaty-mesh-1 and removes `policy.lend`, and
the host's login role (L56) is dropped. A mounted volume is unmounted and
detached by hand.
