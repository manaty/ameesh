-- SPDX-License-Identifier: AGPL-3.0-only
-- L160e2 r9 (#517): each admission has a durable identity, even if the same
-- freeze/SHA/target is requeued or the row is removed and then recreated.
create sequence if not exists merge_queue_generation_seq;
alter table merge_queue
    add column if not exists generation bigint not null
        default nextval('merge_queue_generation_seq'),
    add column if not exists frozen_id bigint;
-- Backfill the freeze in force at admission, not a later refreeze.
update merge_queue q set frozen_id = (
    select m.id from work_item_milestones m
    where m.work_item_id = q.work_item_id and m.kind = 'frozen'
      and m.at <= q.queued_at
    order by m.at desc, m.id desc limit 1
) where frozen_id is null;
