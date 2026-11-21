-- SPDX-License-Identifier: AGPL-3.0-only
-- #7986 : rejeu du contrat du rôle superviseur-contenus après les migrations
-- (deploy/mise-a-jour/staging.sh, poste.sh migrer), puis son audit.
-- Lancé par `python -m ameesh.droits_roles` sous la connexion de `migrate`
-- (propriétaire des tables et de la base, sans CREATEROLE), avec le
-- search_path du mesh. Le script rejoue role-superviseur-contenus.sql en mode
-- rejeu : REVOKE/GRANT de son contrat de colonnes et audit
-- _contrat-superviseur.sql dans la même transaction. Aucune création ni
-- altération de rôle, aucun mot de passe, aucune appartenance : un rôle absent
-- est sauté et sa création reste le geste de l'administrateur
-- (role-superviseur-contenus.sql).
--
-- La pose de ce rôle est une décision de vie privée (en-tête de
-- role-superviseur-contenus.sql, docs/BASCULE.md) : le rejeu ne fait que
-- tenir les colonnes DÉJÀ relues du script, sur un rôle déjà posé. Il ne
-- crée pas le rôle et n'accorde aucune appartenance ; une colonne ajoutée au
-- contrat reste une décision relue dans la PR du script, et l'accès effectif
-- d'un superviseur reste le GRANT d'appartenance de l'administrateur.
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role ameesh_superviseur_contenus
\endif
SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = :'role') AS ameesh_role_present \gset
\if :ameesh_role_present
  -- Le rejeu ne touche pas au rôle (propriétaire des tables, sans CREATEROLE) :
  -- le contrat commun saute l'étape d'administration et exige qu'il existe.
  SELECT set_config('ameesh.rejeu_superviseur', '1', false) \g /dev/null
  \ir role-superviseur-contenus.sql
  \echo 'superviseur-contenus :' :role 'rejoue, contrat tenu'
\else
  \echo 'superviseur-contenus :' :role 'absent, rejeu saute (creation : role-superviseur-contenus.sql, administrateur)'
\endif
