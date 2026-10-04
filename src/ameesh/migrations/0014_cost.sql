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
-- Chaque ligne porte aussi le **cumul brut** que le harnais a publié pour ce
-- tour (`cum_*`). C'est ce qui sert de repère au tour suivant : le repère vit donc
-- dans le grand livre, et une insertion qui échoue ne fait pas avancer le repère —
-- un tour rejoué après panne retrouve le même delta au lieu d'être perdu (sonde
-- codex2 : repère écrit avant l'insertion = coût perdu).
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
    -- Cumul brut publié par le harnais (Claude : total_cost_usd ; Codex : usage du
    -- fil), tel quel : le repère du tour suivant, lu sur la dernière ligne.
    cum_usd             numeric(14, 6),
    cum_input_tokens    bigint,
    cum_cached_input_tokens bigint,
    cum_output_tokens   bigint,
    recorded_at         timestamptz not null default now()
);

-- `spent` interroge toujours une fenêtre glissante : l'index suit (agent, temps).
create index if not exists turn_costs_agent_time_idx
    on turn_costs (agent, harness, recorded_at desc, id desc);
create index if not exists turn_costs_time_idx
    on turn_costs (recorded_at desc);
