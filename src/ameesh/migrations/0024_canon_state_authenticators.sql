-- SPDX-License-Identifier: AGPL-3.0-only
-- 0024_canon_state_authenticators — partie « authentificateurs » de l'état du
-- canon par hôte (spec §4.1, §8.2 ; lot L9b, revue codex2).
--
-- `canon sync` (CLI comme exécuteur) inscrit l'issue de la synchronisation
-- du registre des authentificateurs (`canon_sync.record_authenticators`) :
--
--   auth_status      ok | skipped (rien écrit, sans erreur : canon non
--                    approuvé) | error (refus — branche de confiance, retard,
--                    monotonie, amorçage — ou erreurs : membre gelé, entrée
--                    illisible) ; NULL tant qu'aucun sync ne l'a écrit ;
--   auth_diagnostic  le diagnostic lisible de cette issue ;
--   auth_checked_at  horodatage de la dernière inscription.
--
-- Une erreur n'est donc jamais silencieuse (elle est aussi écrite dans le fil
-- du projet). Elle ne change pas `status` : la réclamation des agents
-- (`registry.canon_claim_predicate_sql`) ne dépend que de lui.
alter table canon_state
    add column if not exists auth_status text
        check (auth_status in ('ok', 'skipped', 'error')),
    add column if not exists auth_diagnostic text not null default '',
    add column if not exists auth_checked_at timestamptz;
