-- SPDX-License-Identifier: AGPL-3.0-only
-- #7873 : rejeu des droits des connexions limitées après les migrations du
-- staging (deploy/mise-a-jour/staging.sh, poste.sh migrer), puis leur audit.
-- Lancé par `python -m ameesh.droits_limites` sous la connexion de `migrate`
-- (propriétaire des tables, sans superutilisateur), search_path du mesh.
-- GRANT et REVOKE des scripts relus seulement : aucune création de rôle,
-- aucun mot de passe, aucune inscription. Rôle absent : rejeu sauté (sa
-- création reste un geste d'administrateur, role-bureau.sql). L'audit
-- (_contrat-bureau.sql) est dans la même transaction : un droit manquant ou
-- en trop annule le rejeu et fait échouer la commande en le nommant.
\set ON_ERROR_STOP on
\if :{?role}
\else
  DO $$ BEGIN RAISE EXCEPTION 'droits limites : -v role=<connexion limitee> requis'; END $$;
\endif
SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = :'role') AS bureau_present \gset
\if :bureau_present
  -- l'audit vérifie l'inscription telle qu'elle est (agent, hôte) : le rejeu
  -- ne la réécrit pas
  SELECT coalesce((SELECT agent FROM limited_logins WHERE rolname = :'role'), '') AS agent,
         coalesce((SELECT host FROM limited_logins WHERE rolname = :'role'), '') AS host \gset
  BEGIN;
  SELECT set_config('ameesh.bureau_role', :'role', true) \g /dev/null
  \ir _bureau-droits.sql
  \ir _bureau-poser-droits.sql
  \ir _contrat-bureau.sql
  COMMIT;
  \echo 'droits limites :' :role 'rejoues, contrat bureau conforme'
\else
  \echo 'droits limites :' :role 'absent, rejeu saute (creation : role-bureau.sql, administrateur)'
\endif
