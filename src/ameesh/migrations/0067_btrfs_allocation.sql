-- SPDX-License-Identifier: AGPL-3.0-only
-- 0067_btrfs_allocation — btrfs allocation in host resource readings (#246).
--
-- On 2026-10-11 the host's btrfs was fully allocated (1 MiB unallocated) and
-- its metadata exhausted while `df` (statvfs, `disk_free_bytes`) still showed
-- 4.7 then 16 GB free: no disk floor fired. Two columns, null when the work
-- root is not on btrfs or sysfs is unreadable — no guessing:
--   * `btrfs_unallocated_bytes`: device sizes minus the space carved into
--     data, metadata and system block groups;
--   * `btrfs_metadata_headroom_bytes`: metadata total minus used minus
--     reserved (`bytes_may_use`), floored at 0.
-- The floors stay declarative (`policy.resources.min_btrfs_unallocated`,
-- `min_btrfs_metadata_headroom` of the Host card, defaults otherwise).

alter table host_resources add column if not exists btrfs_unallocated_bytes bigint;
alter table host_resources add column if not exists btrfs_metadata_headroom_bytes bigint;
