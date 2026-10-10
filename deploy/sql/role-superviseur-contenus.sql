-- SPDX-License-Identifier: AGPL-3.0-only
-- Rôle Postgres DISTINCT, en lecture seule, qui donne accès aux CONTENUS
-- (profil « cluster », docs/profils/cluster.md) : corps et charges des
-- messages, consignes des agents, extraits de fil, arguments et notes
-- d'actions, descriptions et notes de lots, métadonnées d'approbations.
--
-- Le rôle de base (role-superviseur.sql) ne lit que l'ÉTAT : un superviseur
-- peut appartenir à une autre organisation. Ce rôle-ci ne s'accorde qu'EN
-- PLUS, explicitement, à un superviseur habilité à lire le travail lui-même ;
-- seul, il ne donne que ces colonnes (sans clés de jointure) :
--
--   PGOPTIONS='-c search_path=<schéma ameesh>' \
--   psql "<DSN d'un rôle qui peut CREATE ROLE>" -v ON_ERROR_STOP=1 \
--        [-v role=ameesh_superviseur_contenus] -f deploy/sql/role-superviseur-contenus.sql
--
--   GRANT ameesh_superviseur_contenus TO supervision_habilitee;  -- déjà membre de ameesh_superviseur
--
-- TRANSACTIONNEL ET AUDITÉ (partie commune : _contrat-superviseur.sql) : le
-- script ouvre sa propre transaction ; il crée le rôle s'il manque, retire
-- ses propres droits sur le schéma, accorde la lecture des colonnes du
-- contrat ci-dessous, puis vérifie les droits EFFECTIFS du rôle — y compris
-- ceux reçus de PUBLIC, des rôles dont il hérite, des rôles prédéfinis — et
-- les ACL par défaut du schéma. Si le contrat n'est pas tenu, il échoue avec
-- la liste des écarts et la transaction est ANNULÉE : rien n'est créé ni
-- changé. Il ne répare jamais des droits accordés par d'autres ; il refuse.
-- Idempotent : le relancer après chaque mise à jour d'ameesh. Une colonne du
-- contrat absente (base en retard de migrations) le fait aussi échouer.
-- Jamais de matériel de signature, de reçu ni de nonce : le test
-- tests/test_cluster.py vérifie que ce rôle lit exactement ces colonnes, et
-- le rôle de base aucune.
--
-- Contrat : les colonnes lisibles, relation par relation (rien d'autre).

\if :{?role}
\else
  \set role ameesh_superviseur_contenus
\endif

BEGIN;

SELECT set_config('ameesh.contrat_role', :'role', true) \g /dev/null

CREATE TEMP TABLE ameesh_contrat (rel text PRIMARY KEY, cols text[] NOT NULL) ON COMMIT DROP;
INSERT INTO pg_temp.ameesh_contrat (rel, cols) VALUES
    ('action_events', ARRAY[
        'note'
    ]),
    ('actions', ARRAY[
        'args', 'last_note'
    ]),
    ('agent_mailbox', ARRAY[
        'body', 'payload', 'meta'
    ]),
    ('agent_mesh_overview', ARRAY[
        'pending_prompt', 'current_prompt', 'restart_brief'
    ]),
    ('agent_registry', ARRAY[
        'pending_prompt', 'current_prompt', 'restart_brief'
    ]),
    ('mesh_approvals', ARRAY[
        'meta'
    ]),
    ('mesh_approvals_status', ARRAY[
        'meta'
    ]),
    ('thread_index', ARRAY[
        'last_excerpt'
    ]),
    ('work_item_events', ARRAY[
        'note'
    ]),
    ('work_item_milestones', ARRAY[
        'note'
    ]),
    ('commitments', ARRAY[
        'note'
    ]),
    ('work_items', ARRAY[
        'body'
    ]);

\ir _contrat-superviseur.sql

COMMIT;
