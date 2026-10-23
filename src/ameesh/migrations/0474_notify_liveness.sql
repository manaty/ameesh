-- #3007 : preuve partagée de vie du notificateur, et réclamation des briefs.
--
-- Le seul notificateur à canaux peut tomber avec son poste ; les hôtes sains
-- n'en savent rien, et une décision en attente reste sans poussée. Le
-- notificateur bat donc en base à chaque passage (`notify_liveness`), par
-- hôte et par humain qu'il joint (`*` : `notify.default`, qui couvre tout
-- humain). `exploitation.alerts` et `ameesh decisions` lisent ces battements
-- partagés : une alerte ne se lève que si AUCUN notificateur vivant ne couvre
-- l'humain, jamais sur la seule absence de route locale (faux positif
-- permanent des VM).
--
-- `notify_briefs` réclame le courrier d'alerte aux agents (orchestrateurs,
-- auditeur) : la passe qui le porte tourne sur les VM (ménage du runner), pas
-- sur le poste du notificateur ; la réclamation (recipient, human) évite les
-- doublons entre hôtes et règle la relance après six heures. Aucune donnée
-- secrète : un hôte, un `human:<id>`, un instant.
create table if not exists notify_liveness (
    host    text not null,
    human   text not null,
    seen_at timestamptz not null default clock_timestamp(),
    primary key (host, human)
);
create index if not exists notify_liveness_seen_idx
    on notify_liveness (seen_at);

create table if not exists notify_briefs (
    recipient text not null,
    human     text not null,
    sent_at   timestamptz not null default clock_timestamp(),
    primary key (recipient, human)
);
create index if not exists notify_briefs_sent_idx
    on notify_briefs (sent_at);

comment on table notify_liveness is
    '#3007 : battement de chaque notificateur, par hôte et humain joint (* = défaut)';
comment on table notify_briefs is
    '#3007 : dernier brief notify_undeliverable par destinataire et humain (relance 6 h)';
