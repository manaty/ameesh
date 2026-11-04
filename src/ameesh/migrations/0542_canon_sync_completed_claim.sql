-- SPDX-License-Identifier: AGPL-3.0-only
-- #5688 : la réclamation qui a produit le dernier résultat canon terminé.
--
-- Un runner encore sur le code d'avant 0541 termine la sync partagée sans
-- publier `sync_completed_at` ; seule `sync_claimed_at` date cette fin, et la
-- réclamation suivante l'écrase — y compris quand elle est prise par un ancien
-- runner. `shared_sync` perdait alors la preuve d'une fin récente et refusait
-- le résultat comme « trop ancien » pendant tout le temps d'une sync en cours.
--
-- Le déclencheur copie la réclamation dans `sync_completed_claim_at` à chaque
-- changement du jeton terminé, POUR TOUS les écrivains (ancien ou nouveau
-- code). Une réclamation en attente ne touche pas cette colonne : elle ne date
-- jamais le résultat précédent. Migration additive ; l'âge de #5653 (900 s)
-- n'est pas desserré : la ligne garde la date de la dernière fin, pas celle du
-- claim en cours.
ALTER TABLE canon_state
    ADD COLUMN IF NOT EXISTS sync_completed_claim_at timestamptz;

-- Lignes déjà terminées : la réclamation courante est la seule preuve
-- conservatrice quand elle est aussi la réclamation terminée. Une réclamation
-- en attente au moment de la migration ne date pas le résultat précédent
-- (NULL) : `sync_completed_at` reste le repli, et la prochaine fin remplira la
-- colonne par le déclencheur.
UPDATE canon_state
   SET sync_completed_claim_at = sync_claimed_at
 WHERE sync_completed_token IS NOT NULL
   AND sync_token = sync_completed_token
   AND sync_completed_claim_at IS NULL;

CREATE OR REPLACE FUNCTION ameesh_canon_sync_completed_claim() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.sync_completed_token IS NOT NULL
       AND NEW.sync_completed_token IS DISTINCT FROM OLD.sync_completed_token THEN
        NEW.sync_completed_claim_at := NEW.sync_claimed_at;
    END IF;
    RETURN NEW;
END;
$$;

-- Le déclencheur ne change aucune écriture lue par le code d'avant : il ne
-- remplit que `sync_completed_claim_at`, colonne neuve que le nouveau code
-- seul lit, et laisse `sync_token`, `sync_claimed_at`, `sync_completed_token`
-- et `sync_signature` inchangés. Un runner d'avant 0542 réclame et termine
-- donc exactement comme avant.
-- ameesh: trigger compatible with previous code
DROP TRIGGER IF EXISTS canon_state_sync_completed_claim ON canon_state;
CREATE TRIGGER canon_state_sync_completed_claim
    BEFORE UPDATE OF sync_completed_token ON canon_state
    FOR EACH ROW EXECUTE FUNCTION ameesh_canon_sync_completed_claim();
