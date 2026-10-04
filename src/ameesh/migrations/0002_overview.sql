-- 0002_overview — la vue d'observabilité (spec §5 point 5 : `mesh list`).
--
-- Une seule ligne par agent avec son bail, ses non-lus et son budget. La vue
-- sert la CLI (`agent-mail list`) et, plus tard, `mesh list` côté orchestrateur ou Nexlink.

create or replace view agent_mesh_overview as
select
    r.*,
    extract(epoch from r.lease_expires_at)::float8 as lease_expires_ts,
    extract(epoch from r.last_seen)::float8        as last_seen_ts,
    extract(epoch from r.last_turn_at)::float8      as last_turn_ts,
    (select count(*) from agent_mailbox m
      where m.recipient = r.name and m.delivered_at is null) as unread
from agent_registry r;
