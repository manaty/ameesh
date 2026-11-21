-- SPDX-License-Identifier: AGPL-3.0-only
-- #7986 : rejeu du contrat du rôle ameesh-approve après les migrations
-- (deploy/mise-a-jour/staging.sh, poste.sh migrer), puis son audit.
-- Lancé par `python -m ameesh.droits_roles` sous la connexion de `migrate`
-- (propriétaire des tables et de la base, sans CREATEROLE), avec le
-- search_path du mesh. Le script rejoue role-approve.sql en mode rejeu :
-- REVOKE/GRANT de son contrat de colonnes et audit _contrat-superviseur.sql
-- dans la même transaction. Aucune création ni altération de rôle, aucun mot
-- de passe : un rôle absent est sauté et sa création reste le geste de
-- l'administrateur (role-approve.sql).
--
-- Chaque équipe a son schéma et son rôle ; ce rejeu vise le nom par défaut du
-- script (ameesh_approve). Une installation qui a choisi un autre nom garde
-- son geste d'administrateur et son contrôle de contrat.
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role ameesh_approve
\endif
SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = :'role') AS ameesh_role_present \gset
\if :ameesh_role_present
  -- Le rejeu ne touche pas au rôle (propriétaire des tables, sans CREATEROLE) :
  -- le contrat commun saute l'étape d'administration et exige qu'il existe.
  SELECT set_config('ameesh.rejeu_superviseur', '1', false) \g /dev/null
  \ir role-approve.sql
  \echo 'approve :' :role 'rejoue, contrat tenu'
\else
  \echo 'approve :' :role 'absent, rejeu saute (creation : role-approve.sql, administrateur)'
\endif
