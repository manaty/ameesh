-- SPDX-License-Identifier: AGPL-3.0-only
-- #7986 : rejeu des contrats des rôles du pourvoyeur de pods et du webhook
-- d'admission après les migrations (deploy/mise-a-jour/staging.sh, poste.sh
-- migrer), puis leur audit. Lancé par `python -m ameesh.droits_roles` sous la
-- connexion de `migrate` (propriétaire des tables et de la base, sans
-- CREATEROLE), avec le search_path du mesh.
--
-- role-pod-provisioner.sql est rejoué en mode rejeu : les CREATE/ALTER ROLE et
-- le remplacement de la fonction de garde sont sautés (ils appartiennent à
-- l'administrateur et la fonction est possédée par un autre rôle), les
-- GRANT/REVOKE, les triggers et l'audit _contrat-pod-provisioner.sql restent
-- dans la même transaction. Aucune création ni altération de rôle, aucun mot
-- de passe : si l'un des deux rôles manque, le rejeu est sauté en entier et sa
-- création reste le geste de l'administrateur (role-pod-provisioner.sql).
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role ameesh_pod_provisioner
\endif
\if :{?admission}
\else
  \set admission ameesh_pod_admission
\endif
SELECT (EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role')
        AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'admission')) AS ameesh_roles_present \gset
\if :ameesh_roles_present
  SELECT set_config('ameesh.rejeu_pod', '1', false) \g /dev/null
  \ir role-pod-provisioner.sql
  \echo 'pod-provisioner :' :role :admission 'rejoues, contrat tenu'
\else
  \echo 'pod-provisioner : roles absents (' :role ',' :admission '), rejeu saute (creation : role-pod-provisioner.sql, administrateur)'
\endif
