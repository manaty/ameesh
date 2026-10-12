-- SPDX-License-Identifier: AGPL-3.0-only
-- 0359_host_volumes — every filesystem the host writes to, in resource readings (#253).
--
-- Since 2026-10-12 04:43 the work root is on its own SSD: sampling the first
-- work root only, the disk guard (#246) watched the SSD and no longer the
-- system disk that failed on 2026-10-11 (state, deps cache, docker). A
-- reading now samples each distinct filesystem among the work roots, the
-- state dir, the deps cache root, docker's root dir and `/`, deduplicated by
-- btrfs fsid (st_dev otherwise). The flat columns (`disk_free_bytes`,
-- `btrfs_*`) keep the WORST value per floor, `disk_path` the path of the
-- worst free disk; `volumes` holds each filesystem:
--   [{"path", "paths": [...], "disk_free_bytes", "btrfs_unallocated_bytes",
--     "btrfs_metadata_headroom_bytes"}]
-- Paths and numbers only. The supervisor role reads it
-- (deploy/sql/role-superviseur.sql).

alter table host_resources add column if not exists volumes jsonb;
