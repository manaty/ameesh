# Docker storage: observation, declared-disposable volume retention (#2003) and owner cleanup (#411)

Housekeeping samples Docker storage and counts `dangling=true` volumes
(no container references, including stopped containers), and reads the
**default Docker builder's** BuildKit metadata. The observation itself deletes
nothing. Each CLI call has the runtime's deadline, capped at 10 seconds. It
avoids `docker system df` and recursive volume-size scans, which were slow
during the owner's concurrent prune. Podman and an absent Docker runtime are
not measured by this feature.

Since #2003 the same pass also **reaps the dangling volumes the mesh has
declared disposable**, and only those: conservation is the default, and the
declaration is explicit. `scripts/pg-up.sh` labels a lot bench's volume
`ameesh.bench=disposable` only when its caller declares that bench disposable
(`AMEESH_PG_DISPOSABLE=1`, the per-agent bench of the README); without that
declaration it creates no label, so the volume is kept whatever its creation
time. A shared bench (`ameesh-pg`/`agent-mesh-pg`, default volume
`${NAME}-data`, historically `ameesh-pg-data`) is deliberately persistent and is
**never labelled**, even if the declaration is passed: its data survives the
container, which is that script's contract. A labelled disposable volume is
deleted only after `docker_volume_idle` (default 24 h) of **continuous
absence**, measured by the successive housekeeping passes: each pass journals
the episode (`episode_since`, plus the volume's creation identity), a first
sighting never deletes, a volume referenced again closes the episode, and a
name recreated under the same volume opens a new one. Creation age is used as
identity and reporting information, never as the idle delay.

Nothing else is ever deleted automatically: an anonymous volume
(`com.docker.volume.anonymous`) carries no mesh provenance and is never reaped
here — a mesh container labelled `ameesh.turn` is removed with its anonymous
volumes (`docker rm -f -v`), which is what actually stops the leak; a named
volume without the label, or with a non-disposable label, is listed in the
bounded `others` section with its exact `docker volume rm` command so a human
decides. A disposable volume whose creation identity is unreadable is flagged
without a command, since the episode's identity cannot be established. This
preserves the #411 rule (existing named volumes are kept). `docker volume rm`
itself refuses a volume still mounted, so a `docker run` racing the reap wins.
Set `docker_volumes: false` in the Host card to disable reaping on one host.

`ameesh menage --json` exposes `measure.docker_storage` and `measure.volumes`;
the text report shows the orphan count, the volumes being reaped or kept, and
the undeclared ones with their command. The periodic housekeeping pass
persists the same summary. `ameesh alerts` reads these persisted samples,
without launching Docker. No migration is required.

The default thresholds and retention in the Host card's `policy.housekeeping`
are:

```yaml
docker_orphan_volumes_alert: 100
docker_build_cache_alert: 20GiB
docker_volumes: true          # reap declared-disposable dangling volumes
docker_volume_idle: 24h       # continuous absence before such a volume is reaped
```

The strictest threshold across the host's canons wins; the longest
`docker_volume_idle` wins, and one canon that sets `docker_volumes: false`
disables reaping for that host. Alerts `docker_orphan_volumes` and
`docker_build_cache` are pushed to the responsible human by the standard
notification routing. Errors and samples older than 30 minutes produce
`docker_storage_unavailable`: unknown is never reported as zero. A host needs a
housekeeping sample before these alerts are available. The cache threshold
counts private reclaimable records, excluding shared records and records in
use. Reported total cache size is logical record accounting, not a promise of
physical disk space recovered, especially with shared layers or btrfs. Other
builders require a separate operator inventory.

Sources: [Docker volume lifecycle](https://docs.docker.com/engine/storage/volumes/),
[BuildKit cache accounting](https://docs.docker.com/reference/cli/docker/buildx/du/).

## Incident evidence, 12 October 2026

The initial report recorded 5,092 Docker volumes and about 414.7 GB logically
reclaimable. The owner started `docker volume prune` before this agent's
assignment. This agent neither started nor interrupted that prune. A snapshot
during the prune (14:51–14:54 Europe/Paris) found 3,934 volumes: 3,914 carried
Docker's `com.docker.volume.anonymous` marker and 20 were named; 3,897 had no
container reference. Those counts are a moving snapshot, not a post-prune
inventory. Creation dates cluster around 3–11 October (687 on 10 October,
618 on 8 October, 468 on 9 October). No `org.testcontainers` label was found.

Existing `nexlink-sqltest-*` containers from 9–10 October and new tests at
14:51 on 12 October mount anonymous volumes at `/var/lib/postgresql/data`.
The local `postgres:17` image declares that path as a volume. Nexlink main
(`bc30675`) launches disposable Postgres without `--rm`, then runs
`docker rm -f` without `-v` in SQL, Node SQL, trust benchmark and release
rehearsal scripts. That lifecycle leaves the anonymous volume after removing
the container. The anonymous volume labels do not retain the originating
container's labels, so individual historical orphan volumes cannot be
definitively attributed to a project after container removal.

Nexlink develop already changed cleanup to `rm -f -v` in `fee06cd` on
12 October at 06:15; main does not contain that fix. The companion Nexlink
patch targets main, adds automatic removal for disposable runs and bounded
signal cleanup, preserves `KEEP=1`, and labels live containers by test suite.
Its offline tests execute the startup/EXIT/TERM boundary with a fake Docker
binary: no real container or volume is created or deleted. Existing agent
checkouts also need the repaired script; merging alone cannot fix an old
checkout or an ad hoc `docker run` invocation.

The ameesh parallel-test script already uses `--rm` and tmpfs; its explicit
cleanup now also includes `-v`. The persistent `scripts/pg-up.sh` named
database volume is deliberately retained. Shared housekeeping already uses
`rm -f -v` for authorized ended-turn containers.

A BuildKit snapshot before the no-Docker instruction contains 880 records,
about 95.94 GB logical total and 45.28 GB private reclaimable cache. On the
15:01 instruction relayed by chat-smichea, this agent stopped all further
Docker reads until the owner's prune finishes. It continues code and fixture
validation offline.

## Concrete cleanup proposal

The owner has already undertaken anonymous-volume cleanup. Do not start a
second prune while it runs. Keep all named volumes in
[the preservation manifest](operations/411-docker-volumes-keep.json), even
the five that had no container reference. In particular, preserve the four
`nexlink-page_*` data/config volumes, `tpk-dbg-bundle`, the two ameesh database
volumes, both codex review databases, all ten `supabase_db_sophia-cost-order-*`
volumes and `tpk-browser-extension_pgdata`. Preserve all referenced anonymous
volumes as well; stopped containers still count as references.

After that prune ends, the proposed next action is **cache only**:

```sh
docker builder prune --filter until=168h --keep-storage 20GB
```

This proposal targets unused default-builder cache older than one week,
retains a cache budget and does not request `--all`, volume deletion or image
pruning. It can recover less than the measured 45.28 GB because age and
retention further restrict eligibility. Request an owner decision through
`ameesh decide ask --lot 411 --needs-signature`; this document and the manifest
are the reviewable scope. The monitoring code performed no cleanup at the time
of #411; #2003 (16 October 2026) later added the declared-disposable volume
retention described at the top of this document, which only ever touches a
dangling volume carrying `ameesh.bench=disposable`.

Once the owner's volume prune has finished, refresh the read-only inventory
before executing any separately approved action. If anonymous volumes remain,
produce a new exact candidate manifest and a separate decision, preserving
every named/referenced volume. Do not use `docker volume prune --all` or
`docker system prune --volumes` as a shortcut. The first snapshot cannot
authorize deletion of newly created resources.

References: [volume prune](https://docs.docker.com/reference/cli/docker/volume/prune/),
[builder prune](https://docs.docker.com/reference/cli/docker/builder/prune/).

Post-prune read-only refresh: the Docker prune process was no longer present.
The refresh found 40 volumes, 5 unreferenced, and 20 named volumes;
all named volumes are retained in the updated preservation manifest.
No resources were deleted by this agent.
