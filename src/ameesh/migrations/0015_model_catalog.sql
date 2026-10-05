-- 0015_model_catalog — le catalogue des modèles et la découverte (L14, R21, décision 0020).
--
-- Deux tables, parce que la même question se pose à deux niveaux : ce qu'un
-- **fournisseur** publie (prix, fenêtre de contexte) et ce qu'un **harnais**
-- accepte (modèles et efforts). Un modèle vu par deux harnais donne donc une
-- ligne de `model_catalog` et deux de `model_harness`.
--
-- Aucun historique de prix dans ce lot (décision de mesh-design) : un changement
-- de prix est un **événement** (migration 0012), pas une ligne de plus.
--
-- `source` dit d'où vient l'information (`anthropic`, `openai`, `deepseek`,
-- `harness`, `prices`) et `raw_digest` l'empreinte de la réponse brute : c'est ce
-- qui permet de dire plus tard ce qui a été vu, sans garder la réponse elle-même
-- (elle peut contenir des noms de clients, jamais des secrets).
--
-- **Règle de retrait, écrite ici parce qu'elle se perd vite** : un modèle n'est
-- retiré (`retired_at`) qu'après son ABSENCE dans une liste COMPLÈTE et RÉUSSIE
-- de sa source. Une réponse partielle (pagination non suivie), en erreur, ou une
-- source muette ne retire jamais rien : elle ne fait que ne pas rafraîchir.
create table if not exists model_catalog (
    provider        text        not null,
    model_id        text        not null,
    context_window  bigint,
    price_input     numeric(12,6),
    price_cached    numeric(12,6),
    price_output    numeric(12,6),
    first_seen      timestamptz not null default now(),
    last_seen       timestamptz not null default now(),
    retired_at      timestamptz,
    source          text        not null,
    raw_digest      text,
    primary key (provider, model_id)
);

create table if not exists model_harness (
    provider        text        not null,
    model_id        text        not null,
    harness         text        not null,
    efforts         text[]      not null default '{}',
    first_seen      timestamptz not null default now(),
    last_seen       timestamptz not null default now(),
    retired_at      timestamptz,
    source          text        not null,
    primary key (provider, model_id, harness)
);

create index if not exists model_catalog_last_seen_idx on model_catalog (last_seen desc);
create index if not exists model_harness_harness_idx on model_harness (harness, model_id);
