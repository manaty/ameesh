-- SPDX-License-Identifier: AGPL-3.0-only
-- Rôle Postgres d'ameesh-approve (lot L27) : LECTURE SEULE de ce que le
-- service lit dans la base de SON équipe, et rien d'autre.
--
--   PGOPTIONS='-c search_path=<schéma ameesh de l équipe>' \
--   psql "<DSN d'un rôle qui peut CREATE ROLE>" -v ON_ERROR_STOP=1 \
--        [-v role=ameesh_approve] -f deploy/sql/role-approve.sql
--
-- Le schéma visé est le premier du search_path (`public` par défaut, comme
-- AMEESH_SCHEMA). Une base ou un schéma PAR ÉQUIPE (décision 0026) : ce rôle
-- ne reçoit des droits que dans ce schéma ; une autre équipe a son propre
-- rôle, sur son propre schéma, et aucun des deux n'est membre de l'autre.
--
-- TRANSACTIONNEL ET AUDITÉ — même partie commune que les rôles superviseur
-- de L25 (_contrat-superviseur.sql) : le script ouvre sa transaction, crée le
-- rôle s'il manque (NOLOGIN, sans attribut), retire ses propres droits sur le
-- schéma, accorde la lecture des colonnes du contrat ci-dessous, puis vérifie
-- les droits EFFECTIFS du rôle — directs, via PUBLIC, via un rôle parent ou
-- prédéfini — et les ACL par défaut. Toute appartenance du rôle à un autre
-- rôle est refusée (SET ROLE contournerait l'audit). L'audit couvre aussi
-- TOUS les autres schémas (celui d'une autre équipe compris), les bases et
-- les grands objets : un droit utile hors du schéma de l'équipe fait refuser. Si le contrat n'est pas
-- tenu, il échoue avec la liste des écarts et la transaction est ANNULÉE :
-- rien n'est créé ni changé. Idempotent : le relancer après chaque mise à
-- jour d'ameesh ; une colonne du contrat absente (base en retard de
-- migrations) le fait échouer.
--
-- Le rôle est NOLOGIN. Le service se connecte sous un rôle de connexion
-- dédié, membre de celui-ci, en lecture seule par défaut :
--
--   CREATE ROLE approve_service LOGIN IN ROLE ameesh_approve;
--   ALTER ROLE approve_service SET default_transaction_read_only = on;
--   ALTER ROLE approve_service SET search_path = <schéma de l équipe>;
--   -- mot de passe : \password approve_service (jamais dans un fichier versionné)
--
-- Ce que le service LIT, et pourquoi (miroir du code : la lecture de
-- l'action nomme ses colonnes une à une, `DbActionSource.READ` ; le registre
-- est lu par `storage.postgres.authenticators.AUTH_COLUMNS` ; le test
-- tests/test_approve_equipes.py vérifie que ce contrat et le code concordent) :
--
--   * actions : de quoi RECALCULER l'empreinte (spec §7.1), rendre le résumé
--     (y compris args, que voit l'humain) et vérifier l'état — jamais le
--     matériel de reçu (auth_receipt, auth_nonce, auth_challenge,
--     replace_receipt, replace_nonce) ni les notes ;
--   * authenticators : le registre de confiance — credential_id et clé
--     publique servent à vérifier l'assertion ; révocation et niveau ; canon
--     déclarant (L44 : seul le canon de l'action, `actions.canon`, compte) ;
--   * mesh_consumed_nonces : refuser à la signature un nonce déjà consommé
--     (sans jamais en consommer : c'est l'exécuteur qui consomme, sous la
--     porte) — pas le challenge.
--
-- Rien d'autre : ni boîte aux lettres, ni registre des agents, ni lots, ni
-- coûts, ni approbations permanentes, ni journal du canon. Aucune écriture :
-- les demandes, liens, reçus et propositions d'enrôlement du service vivent
-- dans son état privé, hors base (spec §9). Aucune séquence, aucune fonction
-- SECURITY DEFINER exécutable.

\if :{?role}
\else
  \set role ameesh_approve
\endif

BEGIN;

SELECT set_config('ameesh.contrat_role', :'role', true) \g /dev/null

CREATE TEMP TABLE ameesh_contrat (rel text PRIMARY KEY, cols text[] NOT NULL) ON COMMIT DROP;
INSERT INTO pg_temp.ameesh_contrat (rel, cols) VALUES
    ('actions', ARRAY[
        'action_id', 'project', 'connector', 'operation', 'target', 'args',
        'amount', 'currency', 'policy_version', 'state', 'class',
        'proposed_by', 'work_item', 'digest', 'dedupe', 'replaces',
        'replaced_by',
        -- L44 (0035) : canon de l'action, qui borne les authentificateurs admis
        'canon'
    ]),
    ('authenticators', ARRAY[
        'id', 'approver', 'facade', 'credential_id', 'public_key',
        'key_fingerprint', 'aaguid', 'level', 'canon_ref', 'enrolled_at',
        'updated_at', 'revoked_at', 'revoked_reason',
        -- L44 (0035) : canon déclarant
        'canon'
    ]),
    ('mesh_consumed_nonces', ARRAY[
        'approver', 'nonce', 'consumed_by', 'consumed_at'
    ]);

\ir _contrat-superviseur.sql

COMMIT;
