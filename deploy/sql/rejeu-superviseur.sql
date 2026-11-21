-- SPDX-License-Identifier: AGPL-3.0-only
-- #7982 : rejeu du contrat du rôle superviseur après les migrations du staging
-- (deploy/mise-a-jour/staging.sh, poste.sh migrer), puis son audit.
-- Lancé par `python -m ameesh.droits_superviseur` sous la connexion de
-- `migrate` (propriétaire des tables et de la base, sans CREATEROLE), avec le
-- search_path du mesh. Le script rejoue `role-superviseur.sql` en mode rejeu :
-- REVOKE/GRANT de son contrat de colonnes et audit `_contrat-superviseur.sql`
-- dans la même transaction. Aucune création ni altération de rôle, aucun mot
-- de passe : un rôle absent est sauté et sa création reste le geste de
-- l'administrateur (role-superviseur.sql).
--
-- Un droit manquant ou en trop, une colonne du contrat absente, une ACL par
-- défaut ou une appartenance étrangère font échouer l'audit : la transaction
-- est annulée et la commande sort en erreur en nommant l'écart. Le staging
-- s'arrête alors avant toute installation (code 5 du chemin VM, retour 1 de
-- poste.sh migrer) : la version précédente reste en place.
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role ameesh_superviseur
\endif
SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = :'role') AS ameesh_role_present \gset
\if :ameesh_role_present
  -- Le rejeu ne touche pas au rôle (propriétaire des tables, sans CREATEROLE) :
  -- le contrat commun saute l'étape d'administration et exige qu'il existe.
  SELECT set_config('ameesh.rejeu_superviseur', '1', false) \g /dev/null
  \ir role-superviseur.sql
  \echo 'superviseur :' :'role' 'rejoue, contrat tenu'
\else
  \echo 'superviseur :' :'role' 'absent, rejeu saute (creation : role-superviseur.sql, administrateur)'
\endif
