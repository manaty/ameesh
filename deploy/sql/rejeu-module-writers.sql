-- SPDX-License-Identifier: AGPL-3.0-only
-- #7986 : rejeu des contrats des groupes d'écriture isolés après les
-- migrations (deploy/mise-a-jour/staging.sh, poste.sh migrer), puis leur
-- audit. Lancé par `python -m ameesh.droits_roles` sous la connexion de
-- `migrate` (propriétaire des tables et de la base, sans CREATEROLE), avec le
-- search_path du mesh.
--
-- role-module-writers.sql est rejoué en mode rejeu : les CREATE ROLE sont
-- sautés, les GRANT/REVOKE et l'audit du script restent dans la même
-- transaction. Aucune création ni altération de rôle, aucun mot de passe :
-- si l'un des trois groupes manque, le rejeu est sauté en entier (un rôle
-- absent ne doit pas recevoir un contrat partiel) et sa création reste le
-- geste de l'administrateur (role-module-writers.sql).
\set ON_ERROR_STOP on
SELECT bool_and(EXISTS (SELECT 1 FROM pg_roles WHERE pg_roles.rolname = n)) AS ameesh_roles_present,
       string_agg(n, ', ' ORDER BY n)
         FILTER (WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE pg_roles.rolname = n))
         AS ameesh_roles_absents
  FROM unnest(ARRAY['ameesh_canon_sync', 'ameesh_it_proof', 'ameesh_it_qa']) AS n \gset
\if :ameesh_roles_present
  SELECT set_config('ameesh.rejeu_module_writers', '1', false) \g /dev/null
  \ir role-module-writers.sql
  \echo 'module-writers : ameesh_canon_sync, ameesh_it_proof, ameesh_it_qa rejoues, contrat tenu'
\else
  \echo 'module-writers : roles absents (' :'ameesh_roles_absents' '), rejeu saute (creation : role-module-writers.sql, administrateur)'
\endif
