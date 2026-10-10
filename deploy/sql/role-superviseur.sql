-- SPDX-License-Identifier: AGPL-3.0-only
-- Rôle Postgres en LECTURE SEULE pour un superviseur externe (profil
-- « cluster », docs/profils/cluster.md) : l'ÉTAT du mesh, pas les contenus.
--
--   PGOPTIONS='-c search_path=<schéma ameesh>' \
--   psql "<DSN d'un rôle qui peut CREATE ROLE>" -v ON_ERROR_STOP=1 \
--        [-v role=ameesh_superviseur] -f deploy/sql/role-superviseur.sql
--
-- Le schéma visé est le premier du search_path (`public` par défaut, comme
-- AMEESH_SCHEMA).
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
--
-- Le rôle est NOLOGIN : on ne se connecte jamais sous son nom. Chaque
-- superviseur (humain ou outil) a son propre rôle de connexion, membre de
-- celui-ci, de préférence en lecture seule par défaut :
--
--   CREATE ROLE supervision_outil LOGIN IN ROLE ameesh_superviseur;
--   ALTER ROLE supervision_outil SET default_transaction_read_only = on;
--   -- mot de passe : \password supervision_outil (jamais dans un fichier versionné)
--
-- Ce qui est EXCLU, et pourquoi (le test tests/test_cluster.py vérifie que
-- toute colonne du schéma est soit au contrat ci-dessous, soit dans cette
-- liste : une migration future qui ajoute une colonne oblige à trancher ;
-- d'ici là, elle n'est pas lisible) :
--
--   * matériel de signature et de reçu — rejouable ou utile à un faux :
--       agent_mailbox.signature, .signed_payload, .nonce
--       mesh_approvals.signature, .signed_payload, .nonce
--       (et les mêmes colonnes de la vue mesh_approvals_status)
--       mesh_consumed_nonces.nonce, .challenge
--       actions.auth_receipt, .auth_nonce, .auth_challenge,
--              .replace_receipt, .replace_nonce
--       action_attempts.receipt, .nonce
--       standing_approvals.receipt, .nonce, .challenge
--   * clés publiques et identifiants d'authentificateurs — pas secrets, mais
--     inutiles à la supervision ; l'empreinte suffit :
--       agent_registry.public_key (et la vue agent_mesh_overview)
--       authenticators.public_key, .credential_id
--
--   * CONTENUS — texte libre écrit par les agents ou les humains : corps et
--     charges des messages, consignes, extraits de fil, arguments et notes
--     d'actions, descriptions et notes de lots. Le superviseur peut être
--     d'une autre organisation : il voit l'ÉTAT, pas le travail. Ces
--     colonnes sont l'objet d'un rôle DISTINCT, accordé explicitement en
--     plus (deploy/sql/role-superviseur-contenus.sql) :
--       agent_mailbox.body, .payload, .meta
--       agent_registry.pending_prompt, .current_prompt (et agent_mesh_overview),
--              .restart_brief (brief de `ameesh restart` en attente, L26)
--       thread_index.last_excerpt
--       actions.args, .last_note
--       action_events.note
--       work_items.body
--       commitments.note (L96)
--       work_item_events.note
--       work_item_milestones.note
--       mesh_approvals.meta (et mesh_approvals_status)
--
-- Restent lisibles, comme ÉTAT : titres de lots (work_items.title), cibles
-- d'actions (actions.target), diagnostics courts (status_text, last_error,
-- action_attempts.error, placement_diagnostic, canon_state.diagnostic,
-- authenticator_syncs.summary). Ils peuvent citer un extrait d'erreur d'un
-- harnais ; les retirer ici si ce n'est pas acceptable.
--
-- La base ne contient pas de secret au sens strict (spec §3 : les secrets
-- vivent dans le trousseau ou le gestionnaire de l'hôte, ici des Secret
-- Kubernetes).
--
-- Aucune fonction du schéma n'est SECURITY DEFINER (l'audit le vérifie) ;
-- aucune séquence n'est accordée.
--
-- Contrat : les colonnes lisibles, relation par relation (rien d'autre).

\if :{?role}
\else
  \set role ameesh_superviseur
\endif

BEGIN;

SELECT set_config('ameesh.contrat_role', :'role', true) \g /dev/null

CREATE TEMP TABLE ameesh_contrat (rel text PRIMARY KEY, cols text[] NOT NULL) ON COMMIT DROP;
INSERT INTO pg_temp.ameesh_contrat (rel, cols) VALUES
    ('action_attempts', ARRAY[
        'action_id', 'attempt_no', 'auth_kind', 'receipt_id', 'approver',
        'grant_id', 'reservation_id', 'state', 'external_ref', 'error',
        'launched_by', 'launched_at', 'deadline_at', 'finished_at',
        'settled_by'
    ]),
    ('action_events', ARRAY[
        'id', 'action_id', 'attempt_no', 'event', 'from_state', 'to_state',
        'actor', 'created_at'
    ]),
    ('actions', ARRAY[
        'action_id', 'project', 'work_item', 'proposed_by', 'connector',
        'operation', 'target', 'class', 'amount', 'currency',
        'policy_version', 'digest', 'dedupe', 'requires_receipt',
        'approvers', 'state', 'replaces', 'replaced_by', 'replace_approver',
        'attempts', 'auth_kind', 'auth_approver', 'auth_authenticator_id',
        'auth_expires_at', 'auth_grant_id', 'auth_by', 'launch_deadline',
        'external_ref', 'last_error', 'last_actor', 'created_at',
        'updated_at', 'approved_at', 'launched_at', 'finished_at',
        -- L44 (0035) : canon de l'action (identifiant de fédération, pas de contenu)
        'canon'
    ]),
    ('agent_mailbox', ARRAY[
        'id', 'sender', 'recipient', 'kind', 'work_item_id', 'status',
        'created_at', 'delivered_at', 'signature_key', 'slack_ts', 'host',
        'signature_expires_at'
    ]),
    ('agent_mesh_overview', ARRAY[
        'name', 'chantier', 'harness', 'host', 'cwd', 'session_id',
        'status', 'status_text', 'model', 'budget_usd', 'spent_usd',
        'turns', 'last_error', 'lease_owner', 'lease_expires_at',
        'lease_epoch', 'last_turn_at', 'last_seen', 'created_at',
        'updated_at', 'public_key_fingerprint', 'key_updated_at',
        'key_revoked_at', 'key_role', 'responsible', 'team', 'provider',
        'credential_mode', 'ephemeral', 'ephemeral_expires_at',
        'created_by', 'canon_ref', 'capabilities', 'last_event_at',
        'placement_ok', 'placement_diagnostic', 'placement_ref',
        'placement_profile', 'lease_expires_ts', 'last_seen_ts',
        'last_turn_ts', 'key_ready', 'has_owner_key',
        'ephemeral_expires_ts', 'responsible_ok', 'unread',
        -- L26 (0027), désormais exposés par la vue : réglages sans contenu
        'session_policy', 'effort', 'tier', 'session_work_item',
        'status_since', 'restart_requested_at', 'session_reset_at',
        -- L31 (0029) : priorité, admission déclarative, dépôt de mémoire et
        -- verdict de visibilité (pas de contenu)
        'priority', 'admitted_hosts', 'admitted_tags', 'memory_repository',
        'visibility_ok', 'visibility_diagnostic',
        -- L37 (0030) : mode d'agent et raison d'arrêt structurée (pas de contenu)
        'mode', 'stop_reason',
        -- L39 (0033) : NOM du compte d'origine de la session (jamais un profil)
        'session_account',
        -- L42 (0032) : canon déclarant (identifiant de fédération, pas de contenu)
        'canon'
    ]),
    ('agent_registry', ARRAY[
        'name', 'chantier', 'harness', 'host', 'cwd', 'session_id',
        'status', 'status_text', 'model', 'budget_usd', 'spent_usd',
        'turns', 'last_error', 'lease_owner', 'lease_expires_at',
        'lease_epoch', 'last_turn_at', 'last_seen', 'created_at',
        'updated_at', 'public_key_fingerprint', 'key_updated_at',
        'key_revoked_at', 'key_role', 'responsible', 'team', 'provider',
        'credential_mode', 'ephemeral', 'ephemeral_expires_at',
        'created_by', 'canon_ref', 'capabilities', 'last_event_at',
        'placement_ok', 'placement_diagnostic', 'placement_ref',
        'placement_profile',
        -- L26 (0027) : réglages et état de session, sans contenu
        'session_policy', 'effort', 'tier', 'session_work_item',
        'status_since', 'restart_requested_at', 'session_reset_at',
        -- L31 (0029) : priorité, admission déclarative, dépôt de mémoire et
        -- verdict de visibilité (pas de contenu)
        'priority', 'admitted_hosts', 'admitted_tags', 'memory_repository',
        'visibility_ok', 'visibility_diagnostic',
        -- L37 (0030) : mode d'agent et raison d'arrêt structurée (pas de contenu)
        'mode', 'stop_reason',
        -- L39 (0033) : NOM du compte d'origine de la session (jamais un profil)
        'session_account',
        -- L60 (0041) : plafond de contexte (un nombre, pas de contenu)
        'context_max_tokens',
        -- L105 (0046) : plafonds du tour (des nombres, pas de contenu)
        'turn_max_seconds', 'turn_mail_max',
        -- L42 (0032) : canon déclarant (identifiant de fédération, pas de contenu)
        'canon'
    ]),
    ('authenticator_syncs', ARRAY[
        'id', 'root_member', 'root_commit', 'commits', 'branch', 'trust',
        'host', 'applied_by', 'applied_at', 'summary',
        -- L44 (0035) : journal par canon
        'canon'
    ]),
    ('authenticators', ARRAY[
        'id', 'approver', 'facade', 'key_fingerprint', 'aaguid', 'level',
        'canon_ref', 'enrolled_at', 'updated_at', 'revoked_at',
        'revoked_reason',
        -- L44 (0035) : canon déclarant
        'canon'
    ]),
    ('canon_state', ARRAY[
        'host', 'status', 'root', 'source', 'last_good_commit',
        'last_good_at', 'diagnostic', 'checked_at', 'auth_status',
        'auth_diagnostic', 'auth_checked_at',
        -- L42 (0032) : état par (hôte, canon)
        'canon', 'canon_id'
    ]),
    ('mesh_approvals', ARRAY[
        'id', 'approver', 'action', 'artifact_kind', 'artifact_hash',
        'decision', 'signature_key', 'created_at', 'expires_at',
        'consumed_at', 'consumed_by'
    ]),
    ('mesh_approvals_status', ARRAY[
        'id', 'approver', 'action', 'artifact_kind', 'artifact_hash',
        'decision', 'signature_key', 'created_at', 'expires_at',
        'consumed_at', 'consumed_by', 'unconsumed', 'unexpired',
        'key_known', 'key_live', 'key_matches', 'expires_ts', 'key_is_owner'
    ]),
    ('mesh_consumed_nonces', ARRAY[
        'approver', 'approval_id', 'consumed_by', 'consumed_at'
    ]),
    ('schema_migrations', ARRAY[
        'version', 'name', 'checksum', 'applied_at'
    ]),
    ('spend_pending', ARRAY[
        'agent', 'start_index', 'turn', 'model', 'created_at'
    ]),
    ('standing_approvals', ARRAY[
        'id', 'approver', 'authenticator_id', 'connector', 'operations',
        'action_class', 'max_amount', 'currency', 'consumed_amount', 'uses',
        'valid_until', 'registered_by', 'created_at', 'last_used_at',
        'revoked_at', 'revoked_by'
    ]),
    ('standing_reservations', ARRAY[
        'id', 'grant_id', 'action_id', 'amount', 'connector', 'operation',
        'action_class', 'currency', 'reserved_by', 'reserved_at',
        'released_at', 'released_reason'
    ]),
    ('thread_index', ARRAY[
        'project', 'lot', 'transport', 'host', 'location', 'last_entry_id',
        'last_mailbox_id', 'last_author', 'last_at', 'entries',
        'created_at', 'updated_at'
    ]),
    ('model_catalog', ARRAY[
        'provider', 'model_id', 'context_window', 'price_input', 'price_cached',
        'price_output', 'first_seen', 'last_seen', 'retired_at', 'source', 'raw_digest'
    ]),
    ('model_harness', ARRAY[
        'provider', 'model_id', 'harness', 'efforts', 'first_seen', 'last_seen',
        'retired_at', 'source'
    ]),
    ('quota_gauge_readings', ARRAY[
        'id', 'harness', 'gauge_key', 'used', 'resets_at', 'window_s', 'observed_at',
        'account'
    ]),
    ('provider_balances', ARRAY[
        'id', 'provider', 'currency', 'total', 'granted', 'topped_up', 'available',
        'observed_at', 'account'
    ]),
    -- ressources des hôtes (L31, 0029) : des mesures et l'état des ressources
    -- d'un tour, jamais un contenu
    ('host_resources', ARRAY[
        'id', 'host', 'sampled_at', 'mem_available_bytes', 'swap_used_bytes',
        'load1', 'cpu_count', 'disk_free_bytes', 'disk_path', 'turns_in_progress',
        -- L73 : occupation du /tmp du système
        'tmp_path', 'tmp_fstype', 'tmp_size_bytes', 'tmp_used_bytes',
        -- L106 (0047) : alimentation de l'hôte
        'on_ac', 'battery_percent'
    ]),
    ('turn_resources', ARRAY[
        'id', 'turn_id', 'agent', 'host', 'pgid', 'label', 'containers',
        'started_at', 'ended_at', 'status'
    ]),
    -- ménage (L73, 0045) : journal (chemins, tailles, commandes proposées)
    -- et worktrees suivis ; de l'état d'exécution, jamais un contenu
    ('housekeeping_log', ARRAY[
        'id', 'host', 'at', 'actor', 'kind', 'action', 'path', 'bytes', 'agent',
        'lot', 'detail', 'data'
    ]),
    ('managed_worktrees', ARRAY[
        'id', 'host', 'path', 'repo', 'agent', 'lot', 'turn_id', 'branch', 'head',
        'created_at', 'status', 'detail', 'checked_at', 'ended_at'
    ]),
    -- verdict de la règle de visibilité (L31, 0029) : état, jamais de secret
    ('visibility_checks', ARRAY[
        'persona', 'host', 'ok', 'diagnostic', 'repository', 'context',
        'checked_at', 'expires_at'
    ]),
    ('turn_costs', ARRAY[
        'id', 'agent', 'harness', 'turn', 'model', 'session', 'usd',
        'input_tokens', 'cached_input_tokens', 'output_tokens', 'cum_usd',
        'cum_input_tokens', 'cum_cached_input_tokens', 'cum_output_tokens',
        'recorded_at', 'account',
        -- L60 (0041) : clé du marqueur comptable (agent, index, instant)
        'spend_key',
        -- L95 (0043) : raison d'une ligne écartée des sommes
        'void_reason'
    ]),
    -- corrections du grand livre (L95, 0043) : copie d'une ligne de
    -- turn_costs et valeurs écrites, de l'ÉTAT comptable, aucun contenu
    ('turn_cost_corrections', ARRAY[
        'id', 'run_id', 'turn_cost_id', 'kind', 'reason', 'old_row', 'new_values',
        'actor', 'at'
    ]),
    -- comptes multiples (L30, 0028) : des NOMS de comptes et l'état de bascule,
    -- jamais un profil (dossier, clé) : ceux-là restent sur l'hôte
    ('account_active', ARRAY[
        'host', 'harness', 'account', 'forced', 'updated_at'
    ]),
    ('account_holds', ARRAY[
        'host', 'harness', 'account', 'until_ts', 'reason', 'created_at'
    ]),
    ('account_switches', ARRAY[
        'id', 'host', 'harness', 'from_account', 'to_account', 'kind', 'reason',
        'agent', 'at'
    ]),
    -- plafonds de budget du mesh (L70, 0042) et leur journal : de l'ÉTAT
    -- (montants, acteur), aucun contenu
    -- feuille de route (L96, 0044) : engagements datés, de l'ÉTAT (quoi, pour
    -- quand, porteur, source) ; la note libre est un CONTENU
    ('commitments', ARRAY[
        'id', 'what', 'due_on', 'kind', 'status', 'owner', 'project', 'work_item_id',
        'package_id', 'source_kind', 'source_ref', 'depends_on', 'created_by',
        'created_at', 'updated_at', 'closed_at', 'proposal_key'
    ]),
    ('budget_limits', ARRAY[
        'scope', 'window_s', 'usd', 'set_by', 'updated_at'
    ]),
    ('budget_events', ARRAY[
        'id', 'scope', 'window_s', 'old_usd', 'new_usd', 'actor', 'at'
    ]),
    -- liaisons de session (L41, 0030) : quelle session externe parle au nom
    -- de quel agent ; même nature que agent_registry.session_id, aucun contenu
    ('session_bindings', ARRAY[
        'id', 'session_id', 'harness', 'host', 'agent', 'pid', 'pid_start', 'created_by',
        'created_at', 'revoked_at',
        -- L63 (0048) : heure de démarrage du PID lié, secondes epoch
        'pid_started_at'
    ]),
    ('work_item_events', ARRAY[
        'id', 'work_item_id', 'state', 'actor', 'created_at'
    ]),
    ('work_item_milestones', ARRAY[
        'id', 'work_item_id', 'kind', 'at', 'sha', 'actor', 'verdict'
    ]),
    ('work_items', ARRAY[
        'id', 'type', 'source', 'app', 'title', 'issue_ref', 'workstream',
        'state', 'assignee', 'loops', 'budget_usd', 'spent_usd',
        'created_at', 'updated_at', 'closed_at',
        'package_id', 'package_parent', 'pr_ref', 'close_reason', 'superseded_by',
        -- L40 (0031) : délégation à échéance en cours (pas de contenu)
        'delegated_by', 'delegated_at', 'due_at',
        -- L96 (0044) : dates prévues
        'planned_start', 'planned_end', 'planned_delivery', 'planned_source',
        'planned_by', 'planned_at',
        -- L118 (0050) : branche du lot, sa cible, dernier commit vu en avance
        'branch', 'branch_target', 'branch_head'
    ]),
    -- L40 (0031) : registre des délégations et de leur issue (de l'ÉTAT)
    ('work_item_delegations', ARRAY[
        'id', 'work_item_id', 'delegate', 'delegated_by', 'delegated_at', 'due_at',
        'first_turn_at', 'outcome', 'resolved_at', 'resolved_by'
    ]),
    -- plan de travail (L29, 0026) : copie déclarative du canon, de l'ÉTAT
    ('work_packages', ARRAY[
        'id', 'kind', 'title', 'parent', 'responsible', 'team', 'scope', 'status',
        'canon_ref', 'present', 'synced_at',
        -- L42 (0032) : canon déclarant
        'canon',
        -- L96 (0044) : dates du canon et dates posées dans ameesh
        'start_on', 'end_on', 'delivery_on', 'planned_start', 'planned_end',
        'planned_delivery', 'planned_source', 'planned_by', 'planned_at'
    ]);

\ir _contrat-superviseur.sql

COMMIT;
