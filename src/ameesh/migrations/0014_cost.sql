-- 0014_cost — la comptabilité par tour (L12, R20, décision 0019 §3).
--
-- Un tour terminé laisse une ligne : qui, sur quel harnais, avec quel modèle,
-- pour combien de dollars et de jetons. C'est la matière de `ameesh cost spent`
-- (dépense sur une fenêtre glissante) et de `ameesh cost report`.
--
-- Les coûts sont des ESTIMATIONS : Claude donne un coût cumulé par session dont
-- on prend la différence, Codex un usage cumulé par fil dont on prend la
-- différence, DeepSeek un usage par étape multiplié par le barème. Le barème vit
-- hors base (fichier du poste de travail, `$AMEESH_PRICES`), pour être modifiable
-- sans migration.
--
-- La table est alimentée par le module `ameesh.cost` (`record`), pas par
-- l'exécuteur : L13 y branchera la garde de budget avant chaque tour.
create table if not exists turn_costs (
    id                  bigserial primary key,
    agent               text not null,
    harness             text not null,
    turn                text,
    model               text,
    session             text,
    usd                 numeric(12, 6) not null default 0,
    input_tokens        bigint not null default 0,
    cached_input_tokens bigint not null default 0,
    output_tokens       bigint not null default 0,
    recorded_at         timestamptz not null default now()
);

-- `spent` interroge toujours une fenêtre glissante : l'index suit (agent, temps).
create index if not exists turn_costs_agent_time_idx
    on turn_costs (agent, recorded_at desc);
create index if not exists turn_costs_time_idx
    on turn_costs (recorded_at desc);
