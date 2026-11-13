# Docker storage: observation, declared-disposable volume retention (#2003, #4560) and owner cleanup (#411)

Housekeeping samples Docker storage and counts `dangling=true` volumes
(no container references, including stopped containers), and reads the
**default Docker builder's** BuildKit metadata. The observation itself deletes
nothing. Each CLI call has the runtime's deadline, capped at 10 seconds. It
avoids `docker system df` and recursive volume-size scans, which were slow
during the owner's concurrent prune. Podman and an absent Docker runtime are
not measured by this feature.

The inventory records the measuring host's pass period (`pass_period_s`: the
housekeeping interval plus the pass budget, #5861). It is reported stale
(`docker_storage_unavailable`) once older than two such periods, so a single
lost pass raises nothing. A host without an executor (reading published by
`ameesh hosts --publish`) has no periodic pass: its old inventory raises no
staleness alert.

Since #2003 the same pass also **reaps the dangling volumes the mesh has
declared disposable**, and only those: conservation is the default, and the
declaration is explicit. Two labels declare it: the generic lifetime label
`ameesh.lifetime=test` (#4560), and the older `ameesh.bench=disposable`
(#2003). `scripts/pg-up.sh` puts both on a lot bench's volume only when its
caller declares that bench disposable (`AMEESH_PG_DISPOSABLE=1`, the per-agent
bench of the README); without that declaration it creates no label, so the
volume is kept whatever its creation time. A shared bench
(`ameesh-pg`/`agent-mesh-pg`, default volume `${NAME}-data`, historically
`ameesh-pg-data`) is deliberately persistent and is **never labelled**, even if
the declaration is passed: its data survives the container, which is that
script's contract. A labelled disposable volume is deleted only after
`docker_volume_idle` (default 24 h) of **continuous absence**, measured by the
successive housekeeping passes: each pass journals the episode
(`episode_since`, plus the volume's creation identity), a first sighting never
deletes, a volume referenced again closes the episode, and a name recreated
under the same volume opens a new one. Creation age is used as identity and
reporting information, never as the idle delay.

A **named base volume** (`ameesh-pg-data`, `agent-mesh-pg-data`, any
`*-pg-data`) is never reaped, even when it carries a disposable label by
mistake: it is reported as kept, without a removal command.

Since #4560 the turn's Docker shim also forces `rm -v` on every `docker rm` /
`docker container rm`: a test container removed by its own script no longer
leaves its anonymous volume behind (`-v` removes only anonymous volumes, never
a named one), and an explicit `--volumes=false` is respected. This covers
third-party suites and old checkouts that still call `docker rm -f` without
`-v` — the mechanism behind the 77 orphan volumes / 5.4 GB measured on
agents-2 on 29 October 2026. The shim labels an **untagged** image built during a turn
`ameesh.lifetime=test` (a tagged build is a deliberate artifact and gets no
disposable label); an image without any tag carrying that label is reaped
after the same `docker_volume_idle` of continuous absence. A tagged image is
never deleted, even labelled: a tag is a reference. The removal first puts a
temporary tag of ours on the image (`docker tag`) and removes only that tag
with `--no-prune`: a tag added concurrently survives, and untagged parent
images are never pruned. An untagged image without the label is only counted,
except in the mesh's rootless Docker (#7081): there it follows the same idle
delay and removal path; a container descending from it — even a stopped one —
keeps it.

Nothing else is ever deleted automatically: an anonymous volume
(`com.docker.volume.anonymous`) carries no mesh provenance and is never reaped
by housekeeping — it leaves with the container that owns it (`rm -v`, forced by
the shim and already used by the mesh's own container reaper); a named volume
without the label, or with a non-disposable label, is listed in the bounded
`others` section with its exact `docker volume rm` command so a human decides;
an untagged image without the label is only counted, except in the mesh's
rootless Docker (#7081) where it is reaped after the same idle delay; a tagged
image is never deleted, even labelled. A disposable volume or
image whose creation identity is unreadable is flagged without a command, since
the episode's identity cannot be established. This preserves the #411 rule
(existing named volumes are kept). `docker volume rm` and `docker image rm`
themselves refuse a resource still in use, so a `docker run` racing the reap
wins. Set `docker_volumes: false` / `docker_images: false` in the Host card to
disable each reaping on one host.

`ameesh menage --json` exposes `measure.docker_storage`, `measure.volumes` and
`measure.images`; the text report shows the orphan count, the volumes and
images being reaped or kept, and the undeclared ones with their command. The
periodic housekeeping pass persists the same summary. `ameesh alerts` reads
these persisted samples, without launching Docker. No migration is required.

The default thresholds and retention in the Host card's
`policy.workspaces.housekeeping` are:

```yaml
docker_orphan_volumes_alert: 100
docker_build_cache_alert: 20GiB
docker_volumes: true          # reap declared-disposable dangling volumes
docker_images: true           # declared-disposable untagged + rootless dangling
docker_volume_idle: 24h       # continuous absence before either is reaped
```

The strictest threshold across the host's canons wins; the longest
`docker_volume_idle` wins, and one canon that sets `docker_volumes: false` (or
`docker_images: false`) disables that reaping for the host. Alerts
`docker_orphan_volumes` and `docker_build_cache` are pushed to the responsible
human by the standard notification routing. Errors and samples older than 30
minutes produce `docker_storage_unavailable`: unknown is never reported as
zero. A host needs a housekeeping sample before these alerts are available. The
cache threshold counts private reclaimable records, excluding shared records
and records in use. Reported total cache size is logical record accounting, not
a promise of physical disk space recovered, especially with shared layers or
btrfs. Other builders require a separate operator inventory.

Sources: [Docker volume lifecycle](https://docs.docker.com/engine/storage/volumes/),
[BuildKit cache accounting](https://docs.docker.com/reference/cli/docker/buildx/du/).

## Agent VM BuildKit cache policy (#7245)

For agent VMs AG1–AG3, the proposed Docker rootless default-builder retention
is **10GB** (decimal bytes, about 9.31 GiB), with BuildKit GC enabled. The
reviewable fragment is
[`docker-buildkit-gc.json`](../deploy/serveur/docker-buildkit-gc.json).
It is an input to an operator merge, not a complete replacement for an existing
Docker configuration. The server installer does not apply it automatically.
Housekeeping continues to observe build cache; this proposal adds no prune
command to housekeeping and changes none of its volume/image retention rules.

The scope is the Engine's **docker driver**. Docker rootless normally reads
`~/.config/docker/daemon.json` (under `$XDG_CONFIG_HOME/docker` when configured).
A daemon started with `--config-file` must use that actual file instead.
A `docker-container`, Kubernetes or remote Buildx builder has its own BuildKit
configuration: this fragment does not configure it. Verify the builder used by
the consuming job, including its explicit `--builder`, `BUILDX_BUILDER` or
`DOCKER_BUILDKIT=0` overrides; a CLI's selected builder alone is insufficient.

`defaultKeepStorage` derives the default GC policies. GC periodically removes
eligible unused cache, first disposable/old records and then records above the
retention target. This is **not a filesystem quota**: active cache, newly
exported images, build contexts, concurrent builders and other host activity
can exceed it. Shared layer accounting also prevents interpreting logical cache
size as recoverable physical bytes. A 10GB target alone cannot guarantee that
an arbitrary build stays above the host's free-space alert threshold.

### Operator application proposal (not executed by this lot's author)

The bureau applies the reviewed configuration separately on each VM after the
required approval for an operation that enables cache deletion. Do not prune,
restart Docker, or change a live host as part of a documentation check.

1. Record the host, Engine/Buildx versions, user service command and effective
   configuration path, rootless socket and storage filesystem. Verify
   `docker info` reports rootless and `docker buildx inspect` reports the
   consuming builder's driver as `docker`. Record the host's configured
   free-space alert threshold and its separate hard pause floor; do not
   substitute the historical 2 GiB pause floor for the alert threshold.
2. Read the existing configuration as the daemon user. Back it up, preserving
   its permissions, outside the product checkout. Prepare a candidate by
   merging only `builder.gc.enabled=true` and
   `builder.gc.defaultKeepStorage="10GB"`; preserve all other keys. If an
   explicit `builder.gc.policy` exists, stop this application and have the
   operator reconcile it: the default target does not override custom rules.
   Likewise, an unexpected type in `builder` or `gc` is an error, not grounds
   to replace that section. Treat a missing configuration as an empty object.
3. Validate the candidate with the **installed Engine binary**:
   `dockerd --validate --config-file <candidate>`. JSON parsing alone does not
   validate Engine support. Keep the original configuration in place if this
   fails. Stage the validated candidate beside the real file, owned by the
   daemon user with mode 0600, and atomically rename it into place.
4. Choose an idle Docker window with the consuming jobs' operator before
   activating it. Restart the **user** `docker.service`, never the system
   daemon, through the bureau's approved operation. Restarting can interrupt
   running containers; do not infer live-restore support. Verify the actual
   service, rootless socket, daemon health, active jobs and effective file
   again after restart. A daemon failure calls for restoring the backup and
   restarting the same user service through that operator window.
5. Run the acceptance below on that VM before expanding to the others. Record
   the approved application and its measurements in the lot, not in this
   document. Restoring the previous file reverses the configuration; it cannot
   recover cache already reclaimed by GC (which must be rebuilt).

### Acceptance on each VM

Use an operator-approved, bounded representative build that generates more
than 10GB of default-builder cache cumulatively, with adequate initial headroom.
Do not allocate dummy data until a live host reaches its alert threshold.
Record the exact job, builder, Engine version, configuration and initial cache
inventory. Sample physical free bytes on the Docker data filesystem throughout
all build steps and exports, including GC activity, and keep the minimum. Use
an explicit observation interval (for example 5 seconds), not just before/after
`df` readings. This is sampled evidence, not proof of every intervening instant.
Observe GC using the installed version's builder inventory and daemon logs;
do not invoke a manual prune to make the test pass. Account for shared/in-use
cache separately, and allow the periodic GC to settle before the final reading.

Acceptance requires a successful build, observed automatic reclamation of
eligible excess cache, and a sampled minimum **above the configured alert
threshold**, with the operator's headroom margin documented. Record the
maximum cache, minimum free bytes, elapsed observation period, and final
reclaimable/in-use cache. Repeat for AG1, AG2 and AG3; a VM not measured remains
unqualified. If the driver differs, GC is not observed, cache remains in use,
or physical free space crosses the threshold, report the unmet condition;
changing the target alone is not a proven fix for an oversized active build.
The next mitigation needs workload/storage isolation or admission/headroom
control, separately scoped and reviewed.

Sources: [Docker BuildKit GC and driver-specific configuration](https://docs.docker.com/build/cache/garbage-collection/),
[rootless daemon paths and user service](https://docs.docker.com/engine/security/rootless/tips/).

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
