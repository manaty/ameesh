# Docker storage: observation, attributed-volume retention (#2003) and owner cleanup (#411)

Housekeeping samples Docker storage and counts `dangling=true` volumes
(no container references, including stopped containers), and reads the
**default Docker builder's** BuildKit metadata. The observation itself deletes
nothing. Each CLI call has the runtime's deadline, capped at 10 seconds. It
avoids `docker system df` and recursive volume-size scans, which were slow
during the owner's concurrent prune. Podman and an absent Docker runtime are
not measured by this feature.

Since #2003 the same pass also **reaps attributed Docker volumes**: a dangling
volume is deleted only when the mesh can say where it came from, and only
after `docker_volume_idle` (default 24 h):

* a named volume labelled `ameesh.bench` — `scripts/pg-up.sh` labels the
  database volume it creates; a shared bench keeps its volume while its
  container exists, stopped containers included;
* an anonymous volume (`com.docker.volume.anonymous`) whose creation time
  falls inside a recorded turn of this host (`turn_resources`); it follows the
  container rule and is reaped once that turn is no longer `running`.

Everything else is **never deleted automatically**: a named volume without the
label, an anonymous volume created outside any recorded turn, or an anonymous
volume whose creation time is unreadable is listed in the bounded `others`
section with its exact `docker volume rm` command so a human decides; a
labelled bench volume whose creation time is unreadable is flagged without a
command, since the age guard cannot be checked. This preserves the #411 rule
(existing named volumes are kept) while stopping the silent accumulation of
anonymous volumes leaked by test benches that remove their container without
`-v`. `docker volume rm` itself refuses a volume still mounted, so a
`docker run` racing the reap wins. Set
`docker_volumes: false` in the Host card to disable reaping on one host.

`ameesh menage --json` exposes `measure.docker_storage` and `measure.volumes`;
the text report shows the orphan count, the volumes being reaped or kept, and
the unattributed ones with their command. The periodic housekeeping pass
persists the same summary. `ameesh alerts` reads these persisted samples,
without launching Docker. No migration is required.

The default thresholds and retention in the Host card's `policy.housekeeping`
are:

```yaml
docker_orphan_volumes_alert: 100
docker_build_cache_alert: 20GiB
docker_volumes: true          # reap attributed dangling volumes
docker_volume_idle: 24h       # delay before an attributed volume is reaped
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
of #411; #2003 (16 October 2026) later added the attributed-volume retention
described at the top of this document, which never touches a named volume
without the `ameesh.bench` label.

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
