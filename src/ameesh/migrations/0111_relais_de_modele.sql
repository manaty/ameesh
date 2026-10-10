-- SPDX-License-Identifier: AGPL-3.0-only
-- 0111_relais_de_modele — le grand livre distingue la mesure du relais de
-- modèle de la déclaration d'un appareil (lot L111, étude de l'exécuteur
-- médié §5).
--
-- `turn_costs.source` : d'où vient la ligne.
--   * 'harness' (défaut, toutes les lignes d'avant L111) : l'exécuteur d'un
--     hôte du mesh a lu le flux de son harnais et appliqué le barème ; c'est
--     la comptabilité d'aujourd'hui, elle compte au plafond ;
--   * 'relay'  : le relais de modèle du serveur du mesh a MESURÉ l'usage que
--     le fournisseur a renvoyé (jetons d'entrée, de cache et de sortie) pour
--     une requête qu'il a lui-même transmise avec la clé du serveur ; elle
--     fait foi et compte au plafond ;
--   * 'device' : un exécuteur médié (appareil prêté) a déclaré le coût de son
--     tour ; la ligne reste lisible (comparaison, repère de cumul du tour
--     suivant) mais NE COMPTE PAS au plafond : le relais l'a déjà mesuré.
--
-- `executor`, `lease_owner`, `lease_epoch` : le bail sous lequel la requête
-- relayée a été faite (tirés du jeton de session) ; NULL hors relais.
--
-- Numéro 0111 : celui du lot, pour ne pas croiser les migrations des lots
-- parallèles (0043–0110) ; un trou n'est pas une erreur.

alter table turn_costs add column if not exists source text not null default 'harness';

do $$
begin
    if not exists (select 1 from pg_constraint
                   where conname = 'turn_costs_source_check'
                     and conrelid = 'turn_costs'::regclass) then
        alter table turn_costs add constraint turn_costs_source_check
            check (source in ('harness', 'relay', 'device'));
    end if;
end $$;

alter table turn_costs add column if not exists executor text;
alter table turn_costs add column if not exists lease_owner text;
alter table turn_costs add column if not exists lease_epoch bigint;

-- Le relais relit la dépense avant chaque requête : la fenêtre glissante
-- s'appuie déjà sur `turn_costs_time_idx` ; cet index sert le rapprochement
-- par bail (relais contre déclaration de l'appareil).
create index if not exists turn_costs_lease_idx
    on turn_costs (agent, lease_epoch) where lease_epoch is not null;
