-- 0012_events — les événements (C9) et l'état de regroupement par agent.
--
-- Un événement est un message `kind = 'event'` : il réveille l'agent comme un
-- message, mais plusieurs événements proches sont regroupés en un seul réveil
-- (au plus un par `AMEESH_EVENT_COALESCE` secondes, sauf `payload.urgent`).
-- `last_event_at` porte l'instant du dernier réveil d'événements de l'agent :
-- il survit au redémarrage de l'exécuteur, contrairement à un état en mémoire.

alter table agent_mailbox drop constraint if exists agent_mailbox_kind_check;
alter table agent_mailbox add constraint agent_mailbox_kind_check
    check (kind in ('request','reply','notify','event'));

alter table agent_registry add column if not exists last_event_at timestamptz;

-- La requête du battement : « y a-t-il des événements non remis pour X ? »
create index if not exists agent_mailbox_events_idx
    on agent_mailbox (recipient, id)
    where kind = 'event' and delivered_at is null;
