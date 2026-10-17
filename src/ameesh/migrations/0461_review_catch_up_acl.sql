-- SPDX-License-Identifier: AGPL-3.0-only
-- #1448 (N1 de la R2 #1104) : la borne d'activation du rattrapage après
-- fusion (`review_catch_up`, migration 0438) ne doit pas être déplaçable par
-- un rôle exécuteur. Les exécuteurs la lisent ; seul le propriétaire de la
-- table (le pilote qui décide d'un rattrapage historique) peut la déplacer.
--
-- Même mécanisme que 0417 : on lit l'ACL réelle de la table pour révoquer
-- tout privilège non-SELECT accordé à un rôle autre que le propriétaire, y
-- compris PUBLIC et les droits par défaut présents à la création. Une liste
-- de rôles nommés raterait ceux créés après coup par role-executor.sql ;
-- `deploy/sql/role-executor.sql` porte la même exception, avec l'audit du
-- contrat de rôle qui la vérifie.
DO $review_acl$
DECLARE item record;
BEGIN
    IF to_regclass(format('%I.review_catch_up', current_schema())) IS NULL THEN
        RAISE EXCEPTION '#1448: review_catch_up absente — la migration 0438 (#1104) doit être appliquée avant';
    END IF;
    FOR item IN SELECT DISTINCT c.relname, acl.grantee, acl.privilege_type
                  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                  CROSS JOIN LATERAL aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) acl
                 WHERE n.nspname = current_schema()
                   AND c.relname = 'review_catch_up'
                   AND acl.grantee <> c.relowner
                   AND acl.privilege_type <> 'SELECT' LOOP
        EXECUTE format('REVOKE %s ON %I.%I FROM %s',
                       item.privilege_type,
                       current_schema(), item.relname,
                       CASE WHEN item.grantee = 0 THEN 'PUBLIC'
                            ELSE quote_ident(pg_get_userbyid(item.grantee)) END);
    END LOOP;
END
$review_acl$;
