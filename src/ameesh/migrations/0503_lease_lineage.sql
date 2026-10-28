-- SPDX-License-Identifier: AGPL-3.0-only
-- #4245 : la lignée d'un bail au même hôte. Le 28/10 (h-07, #4243), le
-- redémarrage de l'exécuteur mesh-design d'agents-1 (réservation #4086) a
-- fait passer le bail de l'époque 291 à 293 sans quitter l'hôte ; la garde
-- EP4 (`pilot_gesture._role_gate`) a refusé le `packet record` de la
-- composition lancée à l'époque 291, et deux tentatives sur trois ont été
-- perdues sans échec de composition.
--
-- `lease_lineage_host` et `lease_lineage_epoch` disent depuis quelle époque
-- tous les détenteurs du bail ont été sur cet hôte. Ils sont tenus par un
-- déclencheur, pour TOUS les écrivains du bail (claim, attach, relève
-- planifiée, synchronisation du canon) : un détenteur nouveau (époque,
-- propriétaire ou hôte changé, propriétaire non nul) prolonge la lignée
-- seulement si l'hôte de la ligne ne change pas et reste celui de la lignée ;
-- sinon la lignée repart de son époque. Un retour du bail (propriétaire nul)
-- ne la coupe pas : personne ne tient le rôle. La garde accepte une session
-- d'époque E à l'époque E' > E seulement si `lease_lineage_epoch <= E` et que
-- l'hôte de la lignée, de la ligne et de la session sont le même.
--
-- Migration additive : deux colonnes nullables, une fonction, un
-- déclencheur. Les lignes tenues reçoivent une lignée qui part de leur
-- époque courante (aucune époque antérieure n'est présumée sur l'hôte).
ALTER TABLE agent_registry ADD COLUMN lease_lineage_host text;
ALTER TABLE agent_registry ADD COLUMN lease_lineage_epoch bigint;

UPDATE agent_registry
   SET lease_lineage_host = host,
       lease_lineage_epoch = lease_epoch
 WHERE lease_owner IS NOT NULL AND host IS NOT NULL;

CREATE FUNCTION ameesh_lease_lineage() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.lease_owner IS NULL THEN
        -- retour du bail ou clôture : personne ne le tient, la lignée reste
        RETURN NEW;
    END IF;
    IF NEW.lease_epoch IS NOT DISTINCT FROM OLD.lease_epoch
       AND NEW.lease_owner IS NOT DISTINCT FROM OLD.lease_owner
       AND NEW.host IS NOT DISTINCT FROM OLD.host THEN
        RETURN NEW;
    END IF;
    IF NEW.host IS NULL
       OR NEW.host IS DISTINCT FROM OLD.host
       OR OLD.lease_lineage_host IS DISTINCT FROM NEW.host
       OR OLD.lease_lineage_epoch IS NULL THEN
        NEW.lease_lineage_host := NEW.host;
        NEW.lease_lineage_epoch := NEW.lease_epoch;
    ELSE
        NEW.lease_lineage_host := OLD.lease_lineage_host;
        NEW.lease_lineage_epoch := OLD.lease_lineage_epoch;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER agent_registry_lease_lineage
    BEFORE UPDATE OF lease_epoch, lease_owner, host ON agent_registry
    FOR EACH ROW EXECUTE FUNCTION ameesh_lease_lineage();
