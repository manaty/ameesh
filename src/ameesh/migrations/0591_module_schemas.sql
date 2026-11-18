-- P1a des schémas de module (étude #7616, décision owner #222989) : les
-- registres du runner. Additif : aucune table n'est déplacée, aucun droit
-- retiré, aucune ligne métier touchée.
--
-- `module_schemas` est écrit par le runner dans la même transaction que la
-- première migration du module ; `schema_sha256` est l'empreinte de l'identité
-- (module, nom logique), pas des migrations (étude §4.1). Le registre des
-- migrations de module est séparé de `schema_migrations` : une version est
-- locale au module (`dev/0001` n'entre pas en collision avec `0001` noyau).
create table if not exists module_schemas (
    module            text primary key,
    schema_name       text not null unique,
    schema_sha256     text not null,
    created_at        timestamptz not null default now(),
    constraint module_schemas_module_check
        check (module ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    constraint module_schemas_schema_name_check
        check (schema_name ~ '^[a-z_][a-z0-9_]{0,62}$')
);

create table if not exists module_schema_migrations (
    module     text not null references module_schemas(module) on delete restrict,
    version    integer not null,
    name       text not null,
    checksum   text not null,
    applied_at timestamptz not null default now(),
    primary key (module, version)
);

create table if not exists module_schema_non_additive_migrations (
    module   text not null,
    version  integer not null,
    name     text not null,
    checksum text not null,
    primary key (module, version)
);
