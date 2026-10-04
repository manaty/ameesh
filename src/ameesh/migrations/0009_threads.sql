-- 0009_threads — index des fils lisibles (spec §6, C5, R12).
--
-- Le fil est la référence lisible ; la boîte (agent_mailbox) reste la file de
-- distribution. Le contenu des fils vit chez leur transport (v1 : un fichier
-- Markdown par fil sur l'hôte qui l'écrit ; v1.x : Slack, Matrix, Nexlink) :
-- cette table n'en garde que l'index, mis à jour à chaque écriture, pour
-- `ameesh fil list`.
--
-- Un fil = (projet, lot) ; lot '' = le fil du projet. Le transport `file` est
-- local à un hôte : un même fil écrit depuis deux machines a deux emplacements,
-- d'où `host` dans la clé ('' pour un transport partagé).

create table if not exists thread_index (
    project         text not null,
    lot             text not null default '',
    transport       text not null default 'file',
    host            text not null default '',
    location        text not null,
    last_entry_id   text,
    last_mailbox_id bigint,
    last_author     text,
    last_excerpt    text not null default '',
    last_at         timestamptz,
    entries         bigint not null default 0,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    primary key (project, lot, transport, host)
);

create index if not exists thread_index_recent_idx
    on thread_index (last_at desc nulls last);
