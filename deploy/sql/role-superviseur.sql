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
--   ALTER ROLE supervision_outil SET jit = off;  -- #3503 : pas de compilation JIT
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
--       key_holders.public_key (et la vue mesh_keys, #330)
--       authenticators.public_key, .credential_id
--   * exécuteurs médiés (voie B, 0062 et 0063) — empreintes de jetons et de
--     codes, identifiants d'assertions, clé et attestation d'appareil,
--     réponses rejouables du cache d'idempotence :
--       executor_tokens.token_sha256, executor_invitations.code_sha256,
--       executor_assertion_jti.jti, executors.public_key, .device_attestation,
--       exec_idempotency.response
--   * #1467 (0442) : plans de train et reçus rejouables ; tables entièrement
--     fermées aux deux rôles superviseurs, sans nouveau GRANT :
--       integration_trains.*, integration_train_fires.*,
--       integration_train_queue_receipts.*
--       provenance_receipts.* (#1465, 0445, préalable du train)
--   * empreinte d'un identifiant de connexion (#210) — SHA-256 tronqué d'un
--     jeton de rafraîchissement ou d'une clé : ne le révèle pas, mais
--     reconnaîtrait un jeton fuité ; inutile à la supervision :
--       credential_health.failing_fingerprint
--
--   * CONTENUS — texte libre écrit par les agents ou les humains : corps et
--     charges des messages, consignes, extraits de fil, arguments et notes
--     d'actions, descriptions et notes de lots. Le superviseur peut être
--     d'une autre organisation : il voit l'ÉTAT, pas le travail. Ces
--     colonnes sont l'objet d'un rôle DISTINCT, accordé explicitement en
--     plus (deploy/sql/role-superviseur-contenus.sql) :
--       agent_mailbox.body, .payload, .meta
--       agent_registry.pending_prompt, .current_prompt (et agent_mesh_overview),
--              .restart_brief (brief de `ameesh restart` en attente, L26),
--              .election_brief (brief de relève élue, L165/EP3)
--       thread_index.last_excerpt
--       actions.args, .last_note
--       action_events.note
--       work_items.body
--       commitments.note (L96)
--       work_item_events.note
--       work_item_milestones.note
--       work_module_milestones.payload
--       project_modules.schemas, .rules
--       mesh_approvals.meta (et mesh_approvals_status)
--       ci_test_failures.excerpt (sortie d'un test en échec, L161c)
--       host_handoffs.plan, .evidence (métadonnées de travail et chemins, L232a)
--       host_handoff_events.detail (détail du journal, L232a)
--
-- Restent lisibles, comme ÉTAT : titres de lots (work_items.title), valeur
-- attendue des éléments de la file d'amélioration (work_items.expected_value,
-- L119), cibles d'actions (actions.target), diagnostics courts (status_text, last_error,
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
    -- L243b (0418): identity resolution metadata, without keys or signed content.
    ('agent_name_resolutions', ARRAY[
        'id', 'at', 'supplied_name', 'primary_name', 'operation', 'db_user'
    ]),
    -- L243b step 3 (0516, #4299): rename journal, without the free-text reason.
    ('agent_renames', ARRAY[
        'id', 'at', 'old_name', 'new_name', 'actor', 'lease_epoch', 'rewritten',
        'db_user'
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
        -- #2689 : non-lus passifs (copies, accusés, radars), un compteur — la
        -- même règle L125 que l'exécuteur, sans contenu
        'passive_unread',
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
        'canon',
        -- #810 (0428) : la vue est recréée avec `r.*` et expose donc les
        -- colonnes de `agent_registry` apparues depuis 0033 ; accordées ici à
        -- l'identique de la table (mêmes données, mêmes refus de contenu).
        -- L60 (0041) : plafond de contexte (un nombre)
        'context_max_tokens',
        -- L105 (0046) : plafonds du tour (des nombres)
        'turn_max_seconds', 'turn_mail_max',
        -- 0051 : délai de grâce du travail de fond d'un tour (un nombre)
        'turn_grace_seconds',
        -- L232a (0371) : clôture de passage entre hôtes
        'handoff_fenced',
        -- L165b (0384) : réveil sur lot actionnable et session (mêmes réglages)
        'lot_wake', 'session_harness', 'session_cwd', 'session_origin',
        -- L243 (0388) : projets de l'agent (des noms, pas de contenu)
        'projects',
        -- 0403 : attente de bail depuis (un instant)
        'lease_wait_since',
        -- L148 (0410) : `workstream`, le nouveau nom de `chantier` (même donnée)
        'workstream',
        -- #508 (0411) : la vue est recréée avec `r.*` et expose les mêmes
        -- colonnes que `agent_registry` ; preuve de vie du runner, sans
        -- contenu ni secret, accordée à l'identique de la table
        'runner_seen_at', 'runner_seen_host', 'runner_seen_owner'
    ]),
    ('agent_registry', ARRAY[
        'name', 'aliases', 'chantier', 'harness', 'host', 'cwd', 'session_id',
        'status', 'status_text', 'model', 'budget_usd', 'spent_usd',
        'turns', 'last_error', 'lease_owner', 'lease_expires_at',
        'lease_epoch', 'lease_wait_since', 'last_turn_at', 'last_seen', 'created_at',
        'updated_at', 'public_key_fingerprint', 'key_updated_at',
        'key_revoked_at', 'key_role', 'responsible', 'team', 'provider',
        'credential_mode', 'ephemeral', 'ephemeral_expires_at',
        'created_by', 'canon_ref', 'capabilities', 'last_event_at',
        'placement_ok', 'placement_diagnostic', 'placement_ref',
        'placement_profile',
        -- L165 (EP1) : relève et hôtes candidats (des réglages et des noms,
        -- pas de contenu), comme `mode` et `admitted_hosts`
        'releve', 'candidats',
        -- #508 (0411) : preuve de vie du runner, sans contenu ni secret
        'runner_seen_at', 'runner_seen_host', 'runner_seen_owner',
        -- L232a (0371) : clôture de passage entre hôtes
        'handoff_fenced',
        -- #1521 (0446) : début du motif de statut, sans contenu
        'status_reason_since',
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
        -- 0051 : délai de grâce du travail de fond d'un tour (un nombre)
        'turn_grace_seconds',
        -- L165b (0384) : réveil sur lot actionnable, on/off (un réglage) ;
        -- harnais, dossier et id de la session dont ils notent l'ouverture
        -- (même nature que harness, cwd, session_id)
        'lot_wake', 'session_harness', 'session_cwd', 'session_origin',
        -- #4293 : binaire du harnais choisi pour cet agent (canari d'adoption),
        -- un chemin — même nature que cwd, pas de contenu ni de secret
        'harness_bin',
        -- L42 (0032) : canon déclarant (identifiant de fédération, pas de contenu)
        'canon',
        -- L243 (0388) : projets de l'agent (des noms, pas de contenu)
        'projects',
        -- #3185 (0479) : projets dont l'agent relit les lots, hors de son
        -- équipe (des noms, pas de contenu) — comme `projects` et `roles`
        'relit',
        -- L148 (0410) : `workstream`, le nouveau nom de `chantier` (même donnée)
        'workstream',
        -- L244 (0427) : rôles déclarés d'une fiche Agent/Persona (des noms,
        -- pas de contenu) — comme `projects`, le garde QA les lit
        'roles',
        -- 0484 (#3460, décision #135690 a) : retrait d'un agent — un instant,
        -- le nom de qui l'a retiré et la raison de cet état (même nature que
        -- `stop_reason` et `status_text`, pas de contenu)
        'retired_at', 'retired_by', 'retired_reason',
        -- #4245 (0503) : lignée de bail au même hôte (un nom d'hôte et une
        -- époque, même nature que `host` et `lease_epoch`, pas de contenu)
        'lease_lineage_host', 'lease_lineage_epoch',
        -- #4468 (0513) : début de la tenure du bail courant (un instant, même
        -- nature que `lease_wait_since` et `lease_expires_at`, pas de contenu)
        'lease_since'
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
        'canon', 'canon_id',
        -- #793 (0425) : repère des envois Process
        'process_commit'
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
    -- #330 (0364) : clés des humains, hors registre des agents
    ('key_holders', ARRAY[
        'name', 'human', 'public_key_fingerprint', 'key_role',
        'key_updated_at', 'key_revoked_at', 'created_at'
    ]),
    ('mesh_keys', ARRAY[
        'name', 'holder', 'public_key_fingerprint', 'key_role',
        'key_updated_at', 'key_revoked_at'
    ]),
    -- #326 r2 (0364) : journal des clés, sans la clé (l'empreinte suffit)
    ('key_events', ARRAY[
        'id', 'at', 'name', 'holder', 'event', 'key_role', 'fingerprint',
        'db_user', 'owner_gesture'
    ]),
    ('mesh_consumed_nonces', ARRAY[
        'approver', 'approval_id', 'consumed_by', 'consumed_at'
    ]),
    ('schema_migrations', ARRAY[
        'version', 'name', 'checksum', 'applied_at'
    ]),
    ('spend_pending', ARRAY[
        'agent', 'start_index', 'turn', 'model', 'created_at',
        -- #796 (0423) : lot et tour du marqueur comptable, gardés pour la
        -- réparation d'un tour interrompu (de l'état, aucun contenu)
        'work_item_id', 'turn_id',
        -- #2166 (0471) : clé exacte du marqueur, la même que
        -- `turn_costs.spend_key` (déjà lue par le rôle) ; elle ne dit que
        -- l'agent, l'index et l'instant déjà accordés.
        'spend_key'
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
    -- L165b (0384): sessions put aside per (agent, lot) and the runner's
    -- wake bookkeeping; a session id, like agent_registry.session_id
    ('agent_lot_sessions', ARRAY[
        'agent', 'work_item_id', 'session_id', 'harness', 'account', 'cwd',
        'parked_at', 'wakes', 'last_wake_at', 'wake_activity_at', 'created_at'
    ]),
    -- L385 (0373): explicit prerequisites and their audit attribution.
    ('work_item_dependencies', ARRAY[
        'work_item_id', 'blocker_id', 'created_by', 'created_at'
    ]),
    -- L78 (0368): conversation cursors and state, without message content.
    ('conversation_heads', ARRAY['agent', 'seq']),
    ('agent_peek', ARRAY['agent', 'host', 'lease_epoch', 'captured_at']),
    ('conversation_log', ARRAY[
        'agent', 'seq', 'session', 'turn', 'kind', 'author', 'truncated',
        'event_id', 'created_at', 'project', 'lot', 'thread_entry'
    ]),
    ('thread_index', ARRAY[
        'project', 'lot', 'transport', 'host', 'location', 'last_entry_id',
        'last_mailbox_id', 'last_author', 'last_at', 'entries', 'counted_until',
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
        'account', 'produced_at', 'source',
        -- #3755 (0490) : l'overage publié par l'hôte du compte, lisible depuis
        -- la passe d'alertes de n'importe quel hôte ; booléens d'état.
        'overage_capable', 'overage_in_use'
    ]),
    ('provider_balances', ARRAY[
        'id', 'provider', 'currency', 'total', 'granted', 'topped_up', 'available',
        'observed_at', 'account'
    ]),
    -- L120 (0065): harness versions per host, a version line, no content
    ('harness_versions', ARRAY[
        'host', 'harness', 'path', 'version', 'raw', 'observed_at'
    ]),
    -- L232a (0371) : état et réservations ; plans et preuves dans le rôle contenus
    -- L232e (0380): withdrawal intent and agent origins; refusals in contents role
    ('host_lifecycle', ARRAY[
        'host', 'mode', 'generation', 'actor', 'requested_at', 'return_at', 'updated_at',
        -- #4148 : states and evidence of the external release adapter; the
        -- free-text failure stays in the contents role.
        'release_state', 'hook_state', 'claims_open', 'release_epoch',
        'release_generation', 'automatic', 'released_at',
        'return_requested_at', 'return_requested_by', 'release_error_state',
        'release_error_at', 'release_error_since', 'hook_attempt_at',
        'hook_attempt_state', 'hook_attempt_generation', 'hook_attempt_epoch'
    ]),
    ('host_lifecycle_agents', ARRAY[
        'host', 'generation', 'agent', 'critical', 'operation'
    ]),
    -- #4148 : one journal line per acknowledged adapter transition; `detail`
    -- (command output) is a content.
    ('host_lifecycle_transitions', ARRAY[
        'host', 'generation', 'release_epoch', 'state', 'actor', 'created_at'
    ]),
    -- #4148 : per-host scanner lock and the reason a scan did not drain.
    ('host_release_locks', ARRAY[
        'host', 'owner', 'expires_at', 'next_scan_at', 'blocked_reason',
        'blocked_source', 'blocked_since', 'updated_at'
    ]),
    -- L232b (0417) : la fermeture de release, lue par `ameesh_host_gate_state`
    -- (`host` seulement ; périmètre d'opération et reçus exclus)
    ('host_release_fences', ARRAY['host']),
    ('host_handoffs', ARRAY[
        'id', 'agent', 'source', 'target', 'state', 'fenced',
        'memory_bytes', 'disk_bytes', 'reserved_until', 'created_at', 'updated_at'
    ]),
    ('host_handoff_events', ARRAY[
        'id', 'operation', 'state', 'created_at'
    ]),
    -- ressources des hôtes (L31, 0029) : des mesures et l'état des ressources
    -- d'un tour, jamais un contenu
    ('host_resources', ARRAY[
        'id', 'host', 'sampled_at', 'mem_available_bytes', 'swap_used_bytes',
        'load1', 'cpu_count', 'disk_free_bytes', 'disk_path', 'turns_in_progress',
        -- L73 : occupation du /tmp du système
        'tmp_path', 'tmp_fstype', 'tmp_size_bytes', 'tmp_used_bytes',
        -- L106 (0047) : alimentation de l'hôte
        'on_ac', 'battery_percent',
        -- L159d (0061): shared host measure, numbers only
        'mem_total_bytes', 'psi_cpu_some', 'psi_mem_some', 'psi_mem_full',
        'psi_io_some', 'app_cpu', 'guest_cpu', 'cpu_window_s', 'guest_mem_bytes',
        'lendable_cpu', 'lendable_memory_bytes', 'lendable_disk_bytes',
        -- #246 (0067): btrfs allocation, numbers only
        'btrfs_unallocated_bytes', 'btrfs_metadata_headroom_bytes',
        -- #253 (0359): every filesystem the host writes to, paths and numbers
        'volumes',
        -- L159e6 (0361): the guard's last restart (unit names and reasons)
        'guard_restart',
        -- L159f (0069): host gate state, seq and the guard's short reason
        'gate_state', 'gate_seq', 'gate_reason',
        -- #2022 (0455): process and zombie counts, the publishing runner
        'procs_total', 'procs_zombie', 'runner_id', 'runner_procs', 'runner_zombies',
        -- #2814 (0469) : mesures du cgroup v2 du processus (PSI, quota,
        -- compteurs, chemin d'état) — des nombres et un chemin, pas un secret
        'cgroup'
    ]),
    ('turn_resources', ARRAY[
        'id', 'turn_id', 'agent', 'host', 'pgid', 'label', 'containers',
        -- L498 (0439) : le cgroup délégué du tour (chemin d'état, pas un secret)
        'cgroup',
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
        'void_reason',
        -- L111 (0064) : provenance (harness, relay, device) et bail
        'source', 'executor', 'lease_owner', 'lease_epoch',
        -- L225 (0059) : raison d'un tour facturé au repli prudent
        'price_fallback',
        -- L225 (0059) : écritures de cache Claude du tour
        'cache_write_tokens',
        -- #1469 (0443) : compte de facturation qui porte la dépense
        'billing_account',
        -- #796 (0423) : lot servi, début du tour et lien aux ressources du tour
        'work_item_id', 'started_at', 'turn_id'
    ]),
    -- corrections du grand livre (L95, 0043) : copie d'une ligne de
    -- turn_costs et valeurs écrites, de l'ÉTAT comptable, aucun contenu
    ('turn_cost_corrections', ARRAY[
        'id', 'run_id', 'turn_cost_id', 'kind', 'reason', 'old_row', 'new_values',
        'actor', 'at'
    ]),
    -- exécuteurs médiés (voie B : 0062, 0063) : de l'ÉTAT, jamais une
    -- empreinte de jeton ou de code, ni une réponse rejouable
    ('exec_idempotency', ARRAY[
        'executor_id', 'key', 'op', 'request_sha256', 'status', 'at'
    ]),
    ('exec_host_availability', ARRAY[
        'host', 'executor_id', 'available', 'state', 'seq', 'until_at', 'caps',
        'reason', 'updated_at'
    ]),
    ('exec_audit', ARRAY[
        'id', 'at', 'executor_id', 'host', 'principal', 'agent', 'op', 'route',
        'status', 'error', 'fenced', 'replayed', 'idempotency_key'
    ]),
    ('executor_invitations', ARRAY[
        'mesh', 'host', 'agents_allowlist', 'created_by', 'created_at',
        'expires_at', 'consumed_at', 'executor_id', 'cancelled_at'
    ]),
    ('executors', ARRAY[
        'id', 'mesh', 'host', 'thumbprint', 'agents_allowlist', 'label',
        'device_key_sha256', 'enrolled_by', 'enrolled_at', 'last_seen_at',
        'revoked_at', 'revoked_by', 'revoked_why'
    ]),
    ('executor_tokens', ARRAY[
        'kind', 'executor_id', 'agent', 'epoch', 'created_at', 'expires_at'
    ]),
    ('executor_assertion_jti', ARRAY[
        'executor_id', 'expires_at'
    ]),
    ('executor_events', ARRAY[
        'id', 'at', 'kind', 'host', 'executor_id', 'actor', 'detail'
    ]),
    -- comptes multiples (L30, 0028) : des NOMS de comptes et l'état de bascule,
    -- jamais un profil (dossier, clé) : ceux-là restent sur l'hôte.
    -- #3755 (0490) : `forced_until` est l'échéance du forçage, état de
    -- supervision (le forçage rend la main à l'automate à cet instant).
    ('account_active', ARRAY[
        'host', 'harness', 'account', 'forced', 'forced_until', 'updated_at'
    ]),
    ('account_holds', ARRAY[
        'host', 'harness', 'account', 'until_ts', 'reason', 'created_at'
    ]),
    -- #254 (0392) : pause d'un hôte voulue par le propriétaire
    ('host_holds', ARRAY[
        'host', 'reason', 'author', 'created_at', 'until'
    ]),
    ('host_hold_events', ARRAY[
        'id', 'host', 'action', 'author', 'reason', 'until', 'at'
    ]),
    ('account_switches', ARRAY[
        'id', 'host', 'harness', 'from_account', 'to_account', 'kind', 'reason',
        'agent', 'at'
    ]),
    -- credential health (L226a/L226b, 0060, 0068): account NAMES and their state
    -- (short cause and fix command), never a credential
    ('credential_health', ARRAY[
        'host', 'harness', 'account', 'state', 'reason', 'source', 'checked_at',
        'failing_since', 'expires_at', 'last_failure_at', 'failures'
    ]),
    -- provider account credits (#4164, 0500 ; #4166, 0504): account NAMES,
    -- their state, the declared prepaid and the last assessed balance;
    -- `detail` (provider error text, free) stays closed
    ('provider_account_credits', ARRAY[
        'harness', 'account', 'state', 'since', 'checked_at', 'next_probe_at',
        'reason', 'source', 'agent', 'host', 'prepaid_usd', 'prepaid_at',
        'prepaid_by', 'balance_usd', 'ratio', 'runway_at'
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
    -- #1469 (0443) : enveloppe par compte de facturation et réservations des
    -- tours, de l'ÉTAT comptable (montants, jour, clé), aucun contenu
    ('account_budgets', ARRAY[
        'billing_account', 'usd_per_day', 'tz', 'set_by', 'updated_at'
    ]),
    ('account_reservations', ARRAY[
        'id', 'billing_account', 'agent', 'usd', 'period', 'state', 'spend_key',
        'created_at', 'closed_at', 'closed_by'
    ]),
    -- L85 (0396): the event log, identifiers and states only, never free text
    ('ameesh_events', ARRAY[
        'id', 'type', 'object', 'project', 'data', 'tx', 'created_at'
    ]),
    -- the retention's watermark: the greatest position (tx, id) it deleted
    ('ameesh_events_pruned', ARRAY[
        'one', 'tx', 'id', 'at'
    ]),
    -- #928 (0448): the journal's random identity, in every position; it names
    -- a log, it carries no content and no secret
    ('ameesh_events_journal', ARRAY[
        'one', 'journal'
    ]),
    -- L121 (0362): the watches ameesh manages (agent, period, host, when)
    ('agent_watches', ARRAY[
        'agent', 'every_s', 'host', 'enabled_by', 'enabled_at', 'last_sent_at'
    ]),
    -- liaisons de session (L41, 0030) : quelle session externe parle au nom
    -- de quel agent ; même nature que agent_registry.session_id, aucun contenu
    ('session_bindings', ARRAY[
        'id', 'session_id', 'harness', 'host', 'agent', 'pid', 'pid_start', 'created_by',
        'created_at', 'revoked_at',
        -- L63 (0048) : heure de démarrage du PID lié, secondes epoch
        'pid_started_at'
    ]),
    -- L161c (0055) : exécutions de CI relevées et leurs tests en échec (de
    -- l'ÉTAT : noms de tests, commandes pour rejouer ; l'extrait de sortie
    -- est un CONTENU). `run_attempt` (#901, 0429) : quel essai de l'exécution
    -- relancée (`gh run rerun`) porte la ligne.
    ('ci_runs', ARRAY[
        'repo', 'run_id', 'head_sha', 'branch', 'event', 'base', 'work_item_id',
        'status', 'superseded_by', 'url', 'workflow', 'first_seen_at', 'concluded_at',
        'urgent_message_id', 'verdict_message_id',
        -- #747 (0424) : de l'ÉTAT, l'attente et l'exécution séparées ;
        -- created_at = création GitHub du run, started_at = premier relevé
        -- `in_progress` (l'extrait de sortie reste un contenu, ailleurs)
        'created_at', 'started_at', 'run_attempt'
    ]),
    ('ci_test_failures', ARRAY[
        'repo', 'run_id', 'part', 'test', 'module', 'status', 'inherited', 'replay',
        'duration_s', 'failed_at', 'message_id', 'recorded_at', 'run_attempt'
    ]),
    -- #845 (0426) : garde de la CI par l'API — état des runners et de la file
    -- (même nature que ci_runs : identifiants, noms, horodatages, comptages,
    -- aucun contenu)
    ('ci_runner_state', ARRAY[
        'repo', 'runner_id', 'name', 'busy', 'job', 'busy_since', 'stuck_since',
        'seen_at'
    ]),
    ('ci_queue_state', ARRAY[
        'repo', 'oldest_id', 'oldest_at', 'queued', 'seen_at'
    ]),
    -- #3344 (0480) : dernière carte de mesure publiée (même nature :
    -- identifiants, tête, horodatages, aucun contenu)
    ('ci_map_state', ARRAY[
        'repo', 'branch', 'run_id', 'head_sha', 'published_at', 'seen_at'
    ]),
    -- #845 (0426, revue N/P2 2328) : la fenêtre du relevé de la garde, son
    -- coût réel et sa couverture (même nature : horodatages, comptages,
    -- codes) — l'état qui dit si la garde est aveugle, sans contenu
    ('ci_guard_scan', ARRAY[
        'repo', 'started_at', 'finished_at', 'calls', 'runs', 'covered',
        'complete', 'reason'
    ]),
    -- #845 : le relevé par exécution non conclue (cache du relevé borné)
    ('ci_guard_run', ARRAY[
        'repo', 'run_id', 'attempt', 'status', 'queued', 'oldest_id',
        'oldest_at', 'runners', 'seen_at'
    ]),
    -- Session history (L52, 0032 §2): which sessions, when and where;
    -- no content (transcripts stay in harness storage).
    ('persona_sessions', ARRAY[
        'id', 'persona', 'session_id', 'harness', 'host', 'account', 'work_item',
        'started_at', 'ended_at', 'end_reason'
    ]),
    ('work_item_events', ARRAY[
        'id', 'work_item_id', 'state', 'actor', 'created_at'
    ]),
    ('work_item_milestones', ARRAY[
        'id', 'work_item_id', 'kind', 'at', 'sha', 'actor', 'verdict'
    ]),
    ('project_modules', ARRAY['project', 'module', 'canon', 'types']),
    ('work_module_milestones', ARRAY[
        'id', 'work_item_id', 'module', 'kind', 'subject', 'binding_id', 'actor', 'at'
    ]),
    ('work_items', ARRAY[
        'id', 'type', 'source', 'app', 'title', 'issue_ref', 'workstream',
        'state', 'legacy_state', 'assignee', 'loops', 'budget_usd', 'spent_usd',
        'created_at', 'updated_at', 'closed_at',
        'package_id', 'package_parent', 'pr_ref', 'close_reason', 'superseded_by',
        -- L40 (0031) : délégation à échéance en cours (pas de contenu)
        'delegated_by', 'delegated_at', 'due_at',
        -- L96 (0044) : dates prévues
        'planned_start', 'planned_end', 'planned_delivery', 'planned_source',
        'planned_by', 'planned_at',
        -- L119 (0049) : file d'amélioration ; expected_value est une phrase
        -- courte, lisible comme le titre (ÉTAT), le reste des scalaires
        'expected_value', 'value_score', 'priority', 'team',
        'required_capabilities',
        -- L118 (0050) : branche du lot, sa cible, dernier commit vu en avance
        'branch', 'branch_target', 'branch_head',
        -- L118d (0372) : quand le relevé a vu branch_head, et la dernière
        -- fusion partielle (vue la première fois quand)
        'branch_head_at', 'partial_merge', 'partial_seen_at',
        -- L157 (0052) : durée estimée (minutes, source courte, auteur,
        -- instant) et début mesuré du travail — de l'ÉTAT, comme les dates
        -- prévues
        'estimate_minutes', 'estimate_source', 'estimate_by', 'estimate_at',
        'started_at',
        -- L122 (0053) : numéro de migration réservé par le lot, et son
        -- dépôt (#443, 0390) ; #1567 : le numéro réellement atterri, quand la
        -- renumérotation du paquet a changé celui de la réservation
        'migration', 'migration_repo', 'migration_landed', 'migration_landed_repo',
        -- L157b (0054) : taille déclarée du changement
        'change_size',
        -- #705 (0421) : le relecteur désigné d'un lot en qa — l'alerte
        -- stale_lot le nomme, le superviseur doit donc le lire
        'reviewer', 'reviewers',
        -- #3447 (0482) : exécutant externe déclaré d'un lot (un nom d'agent
        -- ou d'humain, de l'ÉTAT comme `assignee` ; NULL = lot ordinaire) —
        -- le superviseur lit à qui le lot est réservé
        'external_executor',
        -- #2466 (0466) : échéance du réveil programmé d'un lot et bail de
        -- livraison (des instants, de l'ÉTAT) — sans eux, la lecture de la
        -- ligne par `to_jsonb` échouerait pour le rôle à droits de colonne
        'wake_at', 'wake_claim_at'
    ]),
    -- L157 (0052) : historique des estimations d'un lot (de l'ÉTAT)
    ('work_item_estimates', ARRAY[
        'id', 'work_item_id', 'minutes', 'source', 'estimated_by', 'at'
    ]),
    -- L157b (0054) : tables de référence des durées publiées par l'auditeur
    -- (des nombres par type, taille, hôte, harnais et estimateur : de l'ÉTAT)
    ('estimate_references', ARRAY[
        'id', 'published_at', 'published_by', 'body'
    ]),
    -- L122 (#711, 0419): migration reservation identities, target and lifecycle
    -- metadata (STATE, no content or credentials).
    ('work_migration_reservations', ARRAY[
        'id', 'work_item_id', 'slot', 'number', 'project', 'target_repo',
        'target_branch', 'target_sha', 'status', 'updated_at'
    ]),
    -- L160d (0057): prepared conflict resolutions between two lots (commits,
    -- ref, conflicting paths: STATE, no content)
    ('work_resolutions', ARRAY[
        'work_item_id', 'other_item_id', 'commit_sha', 'lot_tip', 'other_tip', 'ref',
        'files', 'created_by', 'created_at'
    ]),
    -- L157c (0056): actual time of a delivered lot by phase and size of its
    -- merged diff (durations and line counts: STATE)
    ('work_item_measures', ARRAY[
        'work_item_id', 'measured_at', 'started_at', 'merged_at', 'total_minutes',
        'agent_minutes', 'ci_minutes', 'review_minutes', 'human_minutes', 'ci_runs',
        'merge_sha', 'diff_files', 'diff_insertions', 'diff_deletions',
        'measured_size', 'declared_size',
        -- #796 (0423) : provenance des minutes d'agent ('turns' | 'states')
        'agent_source'
    ]),
    -- L160e (0058): merge queue per target (lot, frozen commit, rank, state:
    -- STATE, no content) and the per-target merge lease
    ('merge_queue', ARRAY[
        'work_item_id', 'target', 'frozen_sha', 'state', 'rank', 'landing_sha', 'reason',
        'queued_by', 'queued_at', 'updated_at', 'source',
        -- L160e2 (0412): admission generation and frozen milestone identity
        'generation', 'frozen_id'
    ]),
    ('merge_locks', ARRAY[
        'target', 'owner', 'epoch', 'expires_at'
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
