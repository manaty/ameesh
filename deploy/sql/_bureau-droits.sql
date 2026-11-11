-- SPDX-License-Identifier: AGPL-3.0-only
-- #6881 : la liste EXACTE des droits d'une connexion limitée du bureau, lue
-- par role-bureau.sql (pose) et _contrat-bureau.sql (audit). Une ligne par
-- relation et privilège ; `cols` NULL = toute la table (SELECT seulement),
-- sinon les seules colonnes nommées ; `opt_cols` : colonnes accordées en plus
-- si elles existent (ajoutées par un lot pas encore dans la base visée).
-- Ce que chaque droit sert (chemins tracés, #6881) :
--   lecture des lots exécutés (`work show`), liaison L41 (`mail bind`), prise
--   #6839 (`work claim`), reçus de provenance (hooks), `agent-mail send|inbox`.
-- Aucune table de secrets, de clés privées, d'approbation ou de comptes.
CREATE TEMP TABLE IF NOT EXISTS bureau_spec(rel text, priv text, cols text[], opt_cols text[]);
DELETE FROM pg_temp.bureau_spec;
INSERT INTO pg_temp.bureau_spec(rel, priv, cols) VALUES
  -- admission d'écriture et sonde de schéma (db.connect)
  ('schema_migrations','SELECT',NULL),
  ('schema_non_additive_migrations','SELECT',NULL),
  -- registre et noms (résolution, orthographe, réveil)
  ('agent_registry','SELECT',NULL),
  -- verrou de ligne seulement (FOR SHARE / FOR KEY SHARE) : toute écriture
  -- est refusée par ameesh_limited_login_guard (0567)
  ('agent_registry','UPDATE',ARRAY['updated_at']),
  ('agent_name_resolutions','INSERT',ARRAY['supplied_name','primary_name','operation']),
  ('agent_candidates','SELECT',NULL),
  ('canon_state','SELECT',NULL),
  ('mesh_keys','SELECT',NULL),
  ('limited_logins','SELECT',NULL),
  -- liaison de session L41
  ('session_bindings','SELECT',NULL),
  ('session_bindings','INSERT',ARRAY['session_id','harness','host','agent','pid','pid_start','pid_started_at','created_by']),
  ('session_bindings','UPDATE',ARRAY['pid','pid_start','pid_started_at','revoked_at']),
  -- courrier
  ('agent_mailbox','SELECT',NULL),
  ('agent_mailbox','INSERT',ARRAY['sender','recipient','body','host','kind','payload','work_item_id','signature','signature_key','signed_payload','nonce','created_at','signature_expires_at']),
  -- `meta` : clé d'idempotence d'un envoi ; réservation (`meta.consigne`) et
  -- remise (`delivered_at`, `status`) du courrier reçu par le hook
  ('agent_mailbox','UPDATE',ARRAY['meta','delivered_at','status']),
  ('thread_index','SELECT',NULL),
  ('thread_index','INSERT',ARRAY['project','lot','transport','host','location','last_entry_id','last_mailbox_id','last_author','last_excerpt','last_at','entries']),
  ('thread_index','UPDATE',ARRAY['location','entries','last_entry_id','last_mailbox_id','last_author','last_excerpt','last_at','counted_until','updated_at']),
  ('ameesh_events','INSERT',ARRAY['type','object','project','data','created_at']),
  -- lots (lecture) et prise #6839 (écriture bornée par 0567)
  ('work_items','SELECT',NULL),
  ('work_items','UPDATE',ARRAY['external_claim_session','external_claimed_at','external_claim_actor','external_claim_host','external_claim_proof','updated_at']),
  ('work_item_events','SELECT',NULL),
  ('work_item_events','INSERT',ARRAY['work_item_id','state','note','actor','evidence']),
  ('work_item_milestones','SELECT',NULL),
  ('work_migration_reservations','SELECT',NULL),
  ('work_module_milestones','SELECT',NULL),
  ('work_item_estimates','SELECT',NULL),
  ('estimate_references','SELECT',NULL),
  ('work_resolutions','SELECT',NULL),
  ('work_packet_members','SELECT',NULL),
  ('work_packages','SELECT',NULL),
  ('merge_queue','SELECT',NULL),
  ('work_item_branch_heads','SELECT',NULL),
  ('work_item_measures','SELECT',NULL),
  ('work_item_dependencies','SELECT',NULL),
  ('work_item_delegations','SELECT',NULL),
  ('external_executors','SELECT',NULL),
  ('project_modules','SELECT',NULL),
  ('commitments','SELECT',NULL),
  ('work_landings','SELECT',NULL),
  -- gestes du portail des lots (résumé de progression de `work show`) : les
  -- colonnes de progress._ACTION_COLUMNS seulement ; ni args, ni empreinte,
  -- ni nonce, ni reçu. Les tables d'approbation (mesh_approvals,
  -- standing_approvals, reçus) restent fermées.
  ('actions','SELECT',ARRAY['action_id','project','work_item','proposed_by','connector','operation','target','class','state','attempts','last_actor','auth_kind','auth_approver','approvers','created_at','updated_at','approved_at','launched_at','finished_at']),
  ('turn_costs','SELECT',NULL),
  ('turn_resources','SELECT',NULL),
  ('spend_pending','SELECT',NULL),
  -- reçus de provenance (hooks)
  ('provenance_receipts','SELECT',NULL),
  ('provenance_receipts','INSERT',ARRAY['subject','sha','actor','session','turn','harness','work_item_id']),
  -- séquences des INSERT ci-dessus (USAGE seulement)
  ('session_bindings_id_seq','USAGE',NULL),
  ('agent_mailbox_id_seq','USAGE',NULL),
  ('work_item_events_id_seq','USAGE',NULL),
  ('ameesh_events_id_seq','USAGE',NULL),
  ('agent_name_resolutions_id_seq','USAGE',NULL);
-- #6877 (0566) : le harnais et la ligne de liaison L41 de la prise, écrits
-- par claim/release dès que la migration est passée ; la garde 0567 les
-- contrôle
INSERT INTO pg_temp.bureau_spec VALUES
  ('work_items','UPDATE',ARRAY[]::text[],ARRAY['external_claim_harness','external_claim_binding']);
