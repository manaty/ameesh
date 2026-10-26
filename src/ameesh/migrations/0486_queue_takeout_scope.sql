-- SPDX-License-Identifier: AGPL-3.0-only
-- #3550: a queue take-out receipt names the target it was taken out of, so
-- the fire's fallback can ignore a removal of another target. Additive and
-- nullable: receipts written before this migration carry no target and stay
-- readable (the reader treats a missing target as "any target", as before,
-- while it still scopes by freeze).
alter table integration_train_queue_receipts
    add column if not exists target text;

comment on column integration_train_queue_receipts.target is
    '#3550: target of the queue row removed by this receipt (null for legacy rows)';
