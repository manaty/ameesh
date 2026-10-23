-- SPDX-License-Identifier: AGPL-3.0-only
-- 0473_election_pilote — L165 (EP1) : une relève élue entre plusieurs hôtes.
--
-- Une fiche Agent peut déclarer `releve: election` et ses `candidats`
-- (étude docs/design/etudes/election-pilote.md §1, §3). Le bail reste la
-- seule autorisation de tourner, et son epoch le fencing : un hôte candidat
-- peut réclamer le bail quand il est libre ou expiré, même si la ligne est
-- encore chez un autre candidat. La réclamation déplace alors la ligne sur
-- l'hôte qui a gagné.
--
-- Le verdict de placement est PAR HÔTE : la politique de l'hôte candidat
-- peut différer de celle du détenteur précédent. `canon sync` écrit donc un
-- verdict (et son profil évalué) dans `agent_candidates`, et la condition de
-- réclamation exige celui de l'hôte qui réclame. Aucune donnée d'un agent
-- n'est supprimée : `candidats` NULL et les lignes de `agent_candidates`
-- restent lisibles après un retour à `releve: hote`.

alter table agent_registry
    add column if not exists releve text not null default 'hote';
alter table agent_registry
    add column if not exists candidats text[];

do $$
begin
    if not exists (select 1 from pg_constraint
                    where conname = 'agent_registry_releve_valid'
                      and conrelid = 'agent_registry'::regclass) then
        alter table agent_registry
            add constraint agent_registry_releve_valid
            check (releve in ('hote', 'election'));
    end if;
end $$;

create table if not exists agent_candidates (
    name                 text        not null references agent_registry(name) on delete cascade,
    host                 text        not null,
    canon                text        not null default '',
    cwd                  text,
    placement_ok         boolean,
    placement_diagnostic text        not null default '',
    placement_ref        text,
    placement_profile    text,
    updated_at           timestamptz not null default clock_timestamp(),
    primary key (name, host)
);

comment on table agent_candidates is
    'L165 (EP1): placement verdict of an elected agent per candidate host, written by canon sync.';
