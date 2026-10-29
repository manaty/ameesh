-- SPDX-License-Identifier: AGPL-3.0-only
-- 0513_lease_since — #4468 : depuis quand le détenteur courant tient le bail.
--
-- La délégation de la passe `alerts` a besoin de l'âge de la tenure courante
-- du détenteur. Un relevé frais et une route publiée
-- (`role_standings.notify_ok`) prouvent que l'hôte POURRAIT pousser, pas que
-- sa minuterie d'alertes tourne ; le battement partagé `notify_liveness`
-- prouve que la passe a tourné, mais son absence ne distingue pas un
-- détenteur qui vient de prendre le bail (lui laisser une grâce bornée) d'un
-- détenteur en place depuis des heures sans passe (déléguer tout de suite).
-- `agent_registry` porte l'époque du bail et son échéance, pas l'instant où
-- le détenteur courant l'a pris : `renew` déplace `updated_at` et
-- `last_seen` à chaque renouvellement, aucun des deux n'est une tenure.
-- Cette colonne est cet instant, tenue par un déclencheur pour tous les
-- écrivains du bail (claim, attach, release).
--
-- Additive : une colonne nullable, une fonction, un déclencheur. Les
-- détenteurs existants n'ont pas d'instant de prise connu ; la reprise leur
-- ouvre UNE fenêtre de grâce, elle ne prétend jamais une tenure plus vieille.
-- Jamais de secret.
set local lock_timeout = '5s';

ALTER TABLE agent_registry ADD COLUMN IF NOT EXISTS lease_since timestamptz;

COMMENT ON COLUMN agent_registry.lease_since IS
    '#4468: database instant the current lease was taken (set on a claim, NULL when the lease is free); renewals keep it, so it measures the holder tenure.';

UPDATE agent_registry
   SET lease_since = clock_timestamp()
 WHERE lease_owner IS NOT NULL AND lease_since IS NULL;

CREATE FUNCTION ameesh_lease_since() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.lease_owner IS NULL THEN
        NEW.lease_since := NULL;
    ELSIF TG_OP = 'INSERT' THEN
        NEW.lease_since := clock_timestamp();
    ELSIF OLD.lease_owner IS DISTINCT FROM NEW.lease_owner
          OR OLD.lease_epoch IS DISTINCT FROM NEW.lease_epoch THEN
        NEW.lease_since := clock_timestamp();
    ELSE
        NEW.lease_since := OLD.lease_since;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS agent_registry_lease_since ON agent_registry;
CREATE TRIGGER agent_registry_lease_since
    BEFORE INSERT OR UPDATE OF lease_owner, lease_epoch ON agent_registry
    FOR EACH ROW EXECUTE FUNCTION ameesh_lease_since();
