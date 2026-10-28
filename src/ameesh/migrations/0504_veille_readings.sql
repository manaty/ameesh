-- 0504 — le relevé précédent de la veille, par harnais (#4291).
--
-- La présence au catalogue partagé n'est pas un relevé de la veille :
-- `models discover` y écrit avant elle (`catalog.record`), et le modèle
-- nouveau passerait pour déjà connu : aucun lot, définitivement (B de
-- l'auditeur, #4291). Cette table garde ce que la veille a réellement vu,
-- par harnais et fournisseur, et rien d'autre n'y écrit.
--
-- Le relevé est un ensemble cumulé : un modèle déjà vu par la veille n'est
-- pas « nouveau » à sa réapparition. C'est la même règle que le catalogue,
-- mais la propriété de la ligne est ici celle de la veille.
create table if not exists veille_readings (
    harness     text        not null,
    provider    text        not null,
    model_id    text        not null,
    first_seen  timestamptz not null default now(),
    last_seen   timestamptz not null default now(),
    primary key (harness, provider, model_id)
);

create index if not exists veille_readings_harness_idx
    on veille_readings (harness, last_seen desc);
