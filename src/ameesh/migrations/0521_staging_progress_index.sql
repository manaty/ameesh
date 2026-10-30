-- SPDX-License-Identifier: AGPL-3.0-only
-- #4880: the independent watcher reads the newest progress per run on every
-- alerts pass. Keep those reads off the full mesh activity history.
create index if not exists ameesh_events_staging_progress_idx
    on ameesh_events (type, object, id desc)
    where type in ('staging.sequence', 'staging.request');
